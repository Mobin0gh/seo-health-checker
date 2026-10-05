"""Deterministic SEO health scorer.

Converts structured fetch, robots, sitemap, and link-check outcomes into a
:class:`app.contracts.HealthReport` with a 0--100 score and a deterministic,
ordered list of issues.

This module is intentionally pure: it performs no network or asynchronous
work and contains no randomness. Given identical inputs it always produces
an identical output.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from app.contracts import (
    AnalysisResult,
    FetchResult,
    HealthReport,
    Issue,
    Severity,
)


# Category ceilings (maximum penalty each category can contribute).
CEILING_FETCH = 100
CEILING_HTTP = 10
CEILING_ROBOTS = 10
CEILING_SITEMAP = 10
CEILING_LINKS = 15
CEILING_CONTENT = 50

# Thresholds / constants.
SLOW_RESPONSE_MS = 3000.0
HTTP_SLOW_PENALTY = 10
ROBOTS_UNAVAILABLE_PENALTY = 10
SITEMAP_UNAVAILABLE_PENALTY = 10
SITEMAP_INVALID_PENALTY = 10
LINKS_PER_BROKEN = 5
ANALYSIS_UNAVAILABLE_PENALTY = 10
MAX_BROKEN_SAMPLES = 5

# Analyzer severity -> content-category penalty mapping.
SEVERITY_PENALTY = {
    Severity.CRITICAL: 40,
    Severity.WARNING: 10,
    Severity.INFO: 0,
}


class ResourceState(Enum):
    """Three-state existence verdict, mirroring the link checker's model.

    Defined locally because ``app.linkchecker.results`` is intentionally not
    imported by other modules.
    """

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


@dataclass
class ScoreInput:
    """All inputs the scorer needs to produce a health report.

    The orchestrator builds this from the fetcher, analyzer, and link
    checker outputs. Everything is optional so partial results (e.g. a fetch
    failure with no analysis) can be scored without fabricating data.
    """

    requested_url: str
    # Fetch
    fetch: Optional[FetchResult] = None
    fetch_failure_code: Optional[str] = None
    # Analysis
    analysis: Optional[AnalysisResult] = None
    analysis_failed: bool = False
    analysis_reason: Optional[str] = None
    # Robots
    robots_exists: Optional[ResourceState] = None
    robots_status: Optional[int] = None
    robots_reason: Optional[str] = None
    # Sitemap
    sitemap_exists: Optional[ResourceState] = None
    sitemap_valid: Optional[bool] = None
    sitemap_status: Optional[int] = None
    sitemap_url_count: int = 0
    sitemap_reason: Optional[str] = None
    # Links
    total_unique_links: int = 0
    checked_links: int = 0
    confirmed_broken_count: int = 0
    broken_url_samples: List[str] = field(default_factory=list)
    links_deadline_interrupted: bool = False


class HealthScorer:
    """Pure, deterministic scorer that converts inputs into a HealthReport."""

    def score(self, inputs: ScoreInput) -> HealthReport:
        """Score the provided inputs and return a HealthReport.

        This method is pure and deterministic: identical inputs always yield
        identical outputs. It performs no network or asynchronous work.
        """
        issues: List[Issue] = []

        # ------------------------------------------------------------
        # 1) Fetch-level issues (fetch failure, HTTP error, slow, redirect)
        # ------------------------------------------------------------
        fetch_failed = inputs.fetch is None
        http_error = False
        http_slow = False
        redirect_http_to_https = False

        # Build fetch-level measurements
        scorer_measurements: dict[str, float | int | bool | None] = {}

        if fetch_failed:
            # No fetch result; use sanitized failure code
            code = inputs.fetch_failure_code or "unknown_fetch_failure"
            issues.append(
                Issue(
                    code="fetch_failed",
                    severity=Severity.CRITICAL,
                    message="دریافت صفحه ناموفق بود.",
                    details=f"کد: {code}",
                )
            )
            scorer_measurements["scorer.fetch_failed"] = True
            # Fabricate nothing: status=0, response_time=0, final_url=requested_url
            final_url = inputs.requested_url
            status = 0
            response_time_ms = 0.0
        else:
            # Fetch succeeded; process HTTP-level outcomes
            assert inputs.fetch is not None
            final_url = inputs.fetch.final_url
            status = inputs.fetch.status
            response_time_ms = inputs.fetch.elapsed_ms
            scorer_measurements["scorer.http_status"] = inputs.fetch.status
            scorer_measurements["scorer.response_ms"] = inputs.fetch.elapsed_ms
            scorer_measurements["scorer.redirect_count"] = inputs.fetch.redirect_count

            # HTTP error: non-2xx final status
            if not (200 <= inputs.fetch.status < 300):
                issues.append(
                    Issue(
                        code="http_error",
                        severity=Severity.CRITICAL,
                        message=f"پاسخ سرور کد {inputs.fetch.status} است.",
                        details=f"کد HTTP: {inputs.fetch.status}",
                    )
                )
                http_error = True
                scorer_measurements["scorer.http_error"] = True
            else:
                # Successful response; check for slow and redirect
                if inputs.fetch.elapsed_ms > SLOW_RESPONSE_MS:
                    issues.append(
                        Issue(
                            code="http_slow",
                            severity=Severity.WARNING,
                            message=f"زمان پاسخ‌گیری {inputs.fetch.elapsed_ms:.0f} میلی‌ثانیه است.",
                            details=None,
                        )
                    )
                    http_slow = True
                    scorer_measurements["scorer.http_slow"] = True

                # Redirect detection: HTTP -> HTTPS
                req_parsed = urllib.parse.urlparse(inputs.fetch.url)
                final_parsed = urllib.parse.urlparse(inputs.fetch.final_url)
                if (
                    req_parsed.scheme == "http"
                    and final_parsed.scheme == "https"
                    and inputs.fetch.redirect_count > 0
                ):
                    issues.append(
                        Issue(
                            code="redirect_http_to_https",
                            severity=Severity.INFO,
                            message="صفحه از HTTP به HTTPS هدایت شد.",
                            details=None,
                        )
                    )
                    redirect_http_to_https = True
                    scorer_measurements["scorer.redirect_http_to_https"] = True

        # ------------------------------------------------------------
        # 2) Robots.txt issues
        # ------------------------------------------------------------
        robots_penalty = 0
        if inputs.robots_exists is not None:
            if inputs.robots_exists is ResourceState.FALSE:
                issues.append(
                    Issue(
                        code="missing_robots",
                        severity=Severity.INFO,
                        message="فایل robots.txt یافت نشد.",
                        details=None,
                    )
                )
                scorer_measurements["scorer.robots_exists"] = False
            elif inputs.robots_exists is ResourceState.UNKNOWN:
                issues.append(
                    Issue(
                        code="robots_unavailable",
                        severity=Severity.WARNING,
                        message="فایل robots.txt قابل تأیید نیست.",
                        details=inputs.robots_reason,
                    )
                )
                robots_penalty = ROBOTS_UNAVAILABLE_PENALTY
                scorer_measurements["scorer.robots_exists"] = None
                scorer_measurements["scorer.robots_status"] = inputs.robots_status
            else:  # TRUE
                scorer_measurements["scorer.robots_exists"] = True
                scorer_measurements["scorer.robots_status"] = inputs.robots_status

        # ------------------------------------------------------------
        # 3) Sitemap.xml issues
        # ------------------------------------------------------------
        sitemap_penalty = 0
        if inputs.sitemap_exists is not None:
            if inputs.sitemap_exists is ResourceState.FALSE:
                issues.append(
                    Issue(
                        code="missing_sitemap",
                        severity=Severity.INFO,
                        message="فایل sitemap.xml یافت نشد.",
                        details=None,
                    )
                )
                scorer_measurements["scorer.sitemap_exists"] = False
            elif inputs.sitemap_exists is ResourceState.UNKNOWN:
                issues.append(
                    Issue(
                        code="sitemap_unavailable",
                        severity=Severity.WARNING,
                        message="فایل sitemap.xml قابل تأیید نیست.",
                        details=inputs.sitemap_reason,
                    )
                )
                sitemap_penalty = SITEMAP_UNAVAILABLE_PENALTY
                scorer_measurements["scorer.sitemap_exists"] = None
                scorer_measurements["scorer.sitemap_status"] = inputs.sitemap_status
            else:  # TRUE
                scorer_measurements["scorer.sitemap_exists"] = True
                scorer_measurements["scorer.sitemap_status"] = inputs.sitemap_status
                scorer_measurements["scorer.sitemap_url_count"] = inputs.sitemap_url_count
                # Validity check
                if not (inputs.sitemap_valid is True):
                    issues.append(
                        Issue(
                            code="sitemap_invalid",
                            severity=Severity.WARNING,
                            message="فایل sitemap.xml معتبر نیست.",
                            details=inputs.sitemap_reason or f"تعداد URL: {inputs.sitemap_url_count}",
                        )
                    )
                    sitemap_penalty = SITEMAP_INVALID_PENALTY
                else:
                    scorer_measurements["scorer.sitemap_valid"] = True

        # ------------------------------------------------------------
        # 4) Link-check issues
        # ------------------------------------------------------------
        links_penalty = 0
        if inputs.confirmed_broken_count > 0:
            # Cap samples to MAX_BROKEN_SAMPLES
            samples = inputs.broken_url_samples[:MAX_BROKEN_SAMPLES]
            details = f"تعداد: {inputs.confirmed_broken_count}\n" + (
                f"نمونه‌ها: {', '.join(samples)}" if samples else ""
            )
            issues.append(
                Issue(
                    code="broken_links",
                    severity=Severity.WARNING,
                    message=f"{inputs.confirmed_broken_count} لینک شکسته است.",
                    details=details,
                )
            )
            links_penalty = min(LINKS_PER_BROKEN * inputs.confirmed_broken_count, CEILING_LINKS)
            scorer_measurements["scorer.links_total"] = inputs.total_unique_links
            scorer_measurements["scorer.links_checked"] = inputs.checked_links
            scorer_measurements["scorer.links_broken"] = inputs.confirmed_broken_count

        if inputs.links_deadline_interrupted:
            issues.append(
                Issue(
                    code="link_check_incomplete",
                    severity=Severity.INFO,
                    message="بررسی لینک‌ها در اثر مهلت قطع شد.",
                    details=f"بررسی‌شده: {inputs.checked_links}/{inputs.total_unique_links}",
                )
            )
            scorer_measurements["scorer.links_deadline_interrupted"] = True

        # ------------------------------------------------------------
        # 5) Analysis issues (reuse analyzer issues verbatim)
        # ------------------------------------------------------------
        content_penalty = 0
        if inputs.analysis is not None:
            # Reuse analyzer issues verbatim
            issues.extend(inputs.analysis.issues)
            # Map analyzer severities to content penalties
            for issue in inputs.analysis.issues:
                if issue.severity == Severity.CRITICAL:
                    content_penalty += 40
                elif issue.severity == Severity.WARNING:
                    content_penalty += 10
                # INFO -> 0
            # Merge analyzer measurements with scorer measurements (no key collisions)
            for k, v in inputs.analysis.measurements.items():
                scorer_measurements[k] = v
        elif inputs.analysis_failed:
            # Analysis unavailable after successful fetch
            issues.append(
                Issue(
                    code="analysis_unavailable",
                    severity=Severity.WARNING,
                    message="تحلیل محتوای صفحه انجام نشد.",
                    details=inputs.analysis_reason,
                )
            )
            content_penalty += ANALYSIS_UNAVAILABLE_PENALTY

        # ------------------------------------------------------------
        # 6) Deduplicate issues deterministically
        # ------------------------------------------------------------
        seen = set()
        deduped: List[Issue] = []
        for issue in issues:
            key = (issue.code, issue.details or "")
            if key not in seen:
                seen.add(key)
                deduped.append(issue)
        issues = deduped

        # ------------------------------------------------------------
        # 7) Sort issues deterministically by severity then code
        # ------------------------------------------------------------
        def severity_rank(sev: Severity) -> int:
            return {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}[sev]

        issues.sort(key=lambda i: (severity_rank(i.severity), i.code, i.details or ""))

        # ------------------------------------------------------------
        # 8) Compute final score
        # ------------------------------------------------------------
        penalties = {
            "fetch": 0,
            "http": 0,
            "robots": robots_penalty,
            "sitemap": sitemap_penalty,
            "links": links_penalty,
            "content": content_penalty,
        }
        if fetch_failed:
            penalties["fetch"] += 100
        if http_error:
            penalties["fetch"] += 100
        if http_slow:
            penalties["http"] += HTTP_SLOW_PENALTY
        # redirect_http_to_https -> 0 points

        total_penalty = 0
        for cat, ceiling in {
            "fetch": CEILING_FETCH,
            "http": CEILING_HTTP,
            "robots": CEILING_ROBOTS,
            "sitemap": CEILING_SITEMAP,
            "links": CEILING_LINKS,
            "content": CEILING_CONTENT,
        }.items():
            total_penalty += min(penalties[cat], ceiling)

        score = max(0, min(100, 100 - total_penalty))

        # ------------------------------------------------------------
        # 9) Build HealthReport
        # ------------------------------------------------------------
        return HealthReport(
            requested_url=inputs.requested_url,
            final_url=final_url,
            status=status,
            response_time_ms=response_time_ms,
            score=score,
            measurements=scorer_measurements,
            issues=issues,
        )