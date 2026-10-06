"""Orchestrator that ties together fetch, analysis, resource checks, and scoring."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import httpx

from app.analyzer import HtmlAnalyzer
from app.contracts import FetchResult, HealthReport
from app.linkchecker import ResourceChecker
from app.scorer import HealthScorer, ResourceState, ScoreInput
import app.security
from app.security import (
    DNSResolutionError,
    FetchTimeout,
    RedirectLimitExceeded,
    ResponseTooLarge,
    SecurityError,
    URLValidationError,
)

logger = logging.getLogger(__name__)

# The only reasons a link result can be "deadline-interrupted".
_DEADLINE_REASONS = frozenset({"deadline_expired", "timeout"})


class OrchestratorError(Exception):
    """Base for orchestrator-raised domain errors."""


class InvalidRequestError(OrchestratorError):
    """Malformed request or URL — surface as HTTP 400."""


class SecurityPolicyRefused(OrchestratorError):
    """SSRF/security-policy refusal — surface as HTTP 403."""


class UpstreamUnavailable(OrchestratorError):
    """DNS/connect/redirect/size failure — surface as HTTP 502."""


class CheckTimeout(OrchestratorError):
    """Deadline/backstop expired — surface as HTTP 504."""


class UnexpectedError(OrchestratorError):
    """Programming error — surface as HTTP 500."""


def _classify_dns_error(exc: DNSResolutionError) -> bool:
    """Return True when the DNS error is an SSRF/security-policy refusal."""
    msg = str(exc)
    return "forbidden address" in msg or "publicly routable" in msg


def _translate_resource_exists(value) -> Optional[ResourceState]:
    if value is None:
        return None
    return ResourceState(value.value)


async def run_check(
    url: str,
    *,
    deadline_ms: Optional[float] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> HealthReport:
    """Run a full health check and return a :class:`app.contracts.HealthReport`.

    :raises InvalidRequestError: URL failed structural validation (HTTP 400).
    :raises SecurityPolicyRefused: SSRF / security-policy refusal (HTTP 403).
    :raises CheckTimeout: outer backstop or internal deadline expired (HTTP 504).
    :raises UpstreamUnavailable: DNS / connectivity / redirect / size failure (HTTP 502).
    :raises UnexpectedError: unexpected programming error (HTTP 500).
    """
    if deadline_ms is None:
        deadline_ms = time.monotonic() * 1000.0 + 10000.0

    try:
        return await asyncio.wait_for(
            _orchestrate(url, deadline_ms, transport),
            timeout=10.0,
        )
    except TimeoutError as exc:
        raise CheckTimeout("check timed out") from exc


async def _orchestrate(
    url: str,
    deadline_ms: float,
    transport: Optional[httpx.AsyncBaseTransport],
) -> HealthReport:
    # ------------------------------------------------------------------ #
    # 1) Fetch
    # ------------------------------------------------------------------ #
    try:
        fetch_result = await app.security.fetch_url(
            url,
            deadline_ms=deadline_ms,
            transport=transport,
        )
    except URLValidationError as exc:
        raise InvalidRequestError(f"Invalid request: {exc}") from exc
    except DNSResolutionError as exc:
        if _classify_dns_error(exc):
            raise SecurityPolicyRefused(f"Security policy: {exc}") from exc
        raise UpstreamUnavailable(f"Upstream failure: {exc}") from exc
    except ResponseTooLarge as exc:
        raise UpstreamUnavailable(f"Upstream failure: {exc}") from exc
    except RedirectLimitExceeded as exc:
        raise UpstreamUnavailable(f"Upstream failure: {exc}") from exc
    except FetchTimeout as exc:
        raise CheckTimeout(f"Timeout: {exc}") from exc
    except httpx.DecodingError as exc:
        raise UpstreamUnavailable(f"Upstream failure: {exc}") from exc
    except SecurityError as exc:
        raise UpstreamUnavailable(f"Upstream failure: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 2) Analyze HTML
    # ------------------------------------------------------------------ #
    analysis = None
    analysis_failed = False
    analysis_reason = None
    try:
        analyzer = HtmlAnalyzer(fetch_result.final_url)
        analysis = analyzer.analyze(fetch_result.body)
    except Exception as exc:
        logger.exception("HTML analysis failed for %s", url)
        analysis_failed = True
        analysis_reason = "analysis_error"

    # ------------------------------------------------------------------ #
    # 3) Deduplicate internal links (preserve order); retain unique count
    # ------------------------------------------------------------------ #
    seen: set = set()
    deduped: List[str] = []
    if analysis is not None:
        for link in analysis.internal_links:
            if link not in seen:
                seen.add(link)
                deduped.append(link)
    total_unique_links = len(deduped)

    # ------------------------------------------------------------------ #
    # 4) Check robots / sitemap / links with the same absolute deadline
    # ------------------------------------------------------------------ #
    robots_result, sitemap_result, link_summary = await ResourceChecker.check_all(
        fetch_result.final_url, deduped, deadline_ms
    )

    # ------------------------------------------------------------------ #
    # 5) Translate resource results into ScoreInput (no local types imported)
    # ------------------------------------------------------------------ #
    broken_deadline = _DEADLINE_REASONS
    confirmed_broken = [
        r for r in link_summary.results
        if r.status.value == "broken" and r.reason not in broken_deadline
    ]
    confirmed_broken_count = len(confirmed_broken)
    broken_url_samples = [r.url for r in confirmed_broken[:5]]
    links_deadline_interrupted = any(
        r.reason in broken_deadline for r in link_summary.results
    )

    score_input = ScoreInput(
        requested_url=fetch_result.url,
        fetch=fetch_result,
        analysis=analysis,
        analysis_failed=analysis_failed,
        analysis_reason=analysis_reason,
        robots_exists=_translate_resource_exists(robots_result.exists),
        robots_status=robots_result.status,
        robots_reason=robots_result.reason,
        sitemap_exists=_translate_resource_exists(sitemap_result.exists),
        sitemap_valid=sitemap_result.valid,
        sitemap_status=sitemap_result.status,
        sitemap_url_count=sitemap_result.url_count,
        sitemap_reason=sitemap_result.reason,
        total_unique_links=total_unique_links,
        checked_links=link_summary.checked_count,
        confirmed_broken_count=confirmed_broken_count,
        broken_url_samples=broken_url_samples,
        links_deadline_interrupted=links_deadline_interrupted,
    )

    # ------------------------------------------------------------------ #
    # 6) Score
    # ------------------------------------------------------------------ #
    scorer = HealthScorer()
    return scorer.score(score_input)
