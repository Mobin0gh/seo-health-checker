"""Internal link checking for SEO Health Checker."""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import List

from app.analyzer.analyzer import _same_effective_host
import app.security
from app.security import (
    DNSResolutionError,
    FetchTimeout,
    RedirectLimitExceeded,
    ResponseTooLarge,
    SecurityError,
    URLValidationError,
)
from app.linkchecker.results import (
    LinkCheckResult,
    LinkCheckSummary,
    LinkStatus,
)


# Bounded concurrency cap for parallel link checks.
DEFAULT_MAX_CONCURRENCY = 5

# Hard cap on the number of unique links to check (preserving input order).
MAX_LINKS_TO_CHECK = 20


async def _check_one_link(url: str, deadline_ms: float) -> LinkCheckResult:
    """Check a single internal link.

    Args:
        url: Absolute URL to check.
        deadline_ms: Absolute monotonic deadline in milliseconds. The same
            value is passed to :func:`app.security.fetch_url`.

    Returns:
        A populated :class:`LinkCheckResult`.
    """
    try:
        fetch_result = await app.security.fetch_url(
            url,
            deadline_ms=deadline_ms,
            user_agent="SEOHealthChecker/0.1",
        )
        # Determine if working or broken based on final status
        if 200 <= fetch_result.status < 400:
            status = LinkStatus.WORKING
            reason = None
            detail = None
        else:
            # 4xx and 5xx are considered broken
            status = LinkStatus.BROKEN
            reason = f"http_status_{fetch_result.status}"
            detail = f"HTTP {fetch_result.status}"
        return LinkCheckResult(
            url=url,
            status=status,
            http_status=fetch_result.status,
            reason=reason,
            detail=detail,
            elapsed_ms=fetch_result.elapsed_ms,
        )
    except URLValidationError as e:
        return LinkCheckResult(
            url=url,
            status=LinkStatus.BROKEN,
            reason="url_validation",
            detail=str(e),
        )
    except DNSResolutionError as e:
        return LinkCheckResult(
            url=url,
            status=LinkStatus.BROKEN,
            reason="dns_failure",
            detail=str(e),
        )
    except ResponseTooLarge as e:
        return LinkCheckResult(
            url=url,
            status=LinkStatus.BROKEN,
            reason="response_too_large",
            detail=str(e),
        )
    except FetchTimeout as e:
        return LinkCheckResult(
            url=url,
            status=LinkStatus.BROKEN,
            reason="timeout",
            detail=str(e),
        )
    except RedirectLimitExceeded as e:
        return LinkCheckResult(
            url=url,
            status=LinkStatus.BROKEN,
            reason="redirect_limit",
            detail=str(e),
        )
    except SecurityError as e:
        # Catch-all for other security errors (e.g. connection refused)
        return LinkCheckResult(
            url=url,
            status=LinkStatus.BROKEN,
            reason="security_error",
            detail=str(e),
        )


def _deadline_expired(deadline_ms: float) -> bool:
    """Return True if the absolute monotonic deadline has already passed."""
    return time.monotonic() * 1000.0 >= deadline_ms


async def check_links(links: List[str], deadline_ms: float) -> LinkCheckSummary:
    """Check a list of internal links with bounded concurrency.

    Args:
        links: List of absolute URLs discovered on the page.
        deadline_ms: Absolute monotonic deadline in milliseconds. The same
            value is passed to every :func:`app.security.fetch_url` call.

    Returns:
        A :class:`LinkCheckSummary` containing per-link results in the
        **input order** of the deduplicated list (after applying the 20-link cap).
    """
    # 1. Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in links:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    # 2. Apply the 20-link cap
    capped = deduped[:MAX_LINKS_TO_CHECK]

    # 3. If the deadline is already expired, return a clearly identified
    #    unverified/deadline-expired result without scheduling any work.
    if _deadline_expired(deadline_ms):
        return LinkCheckSummary(
            results=[
                LinkCheckResult(
                    url=u,
                    status=LinkStatus.BROKEN,
                    reason="deadline_expired",
                    detail="Absolute monotonic deadline expired before scheduling",
                )
                for u in capped
            ],
            checked_count=len(capped),
            broken_count=len(capped),
        )

    # 4. Run checks with bounded concurrency (max 5)
    semaphore = asyncio.Semaphore(DEFAULT_MAX_CONCURRENCY)

    async def semchecked(url: str) -> LinkCheckResult:
        async with semaphore:
            return await _check_one_link(url, deadline_ms)

    # asyncio.gather preserves order of inputs in the output
    results = await asyncio.gather(*(semchecked(u) for u in capped))

    # 5. Build summary
    broken_count = sum(1 for r in results if r.status is LinkStatus.BROKEN)
    return LinkCheckSummary(
        results=list(results),
        checked_count=len(results),
        broken_count=broken_count,
    )


async def check_all(
    base_url: str, links: List[str], deadline_ms: float
) -> tuple:
    """Convenience: run all three checks sequentially.

    Args:
        base_url: Final page URL (after redirects) whose origin defines the
            resource locations.
        links: List of absolute URLs discovered on the page.
        deadline_ms: Absolute monotonic deadline in milliseconds.

    Returns:
        Tuple of (robots_result, sitemap_result, link_summary).
    """
    from .resources import check_robots, check_sitemap

    robots_result = await check_robots(base_url, deadline_ms)
    sitemap_result = await check_sitemap(base_url, deadline_ms)
    link_summary = await check_links(links, deadline_ms)
    return robots_result, sitemap_result, link_summary