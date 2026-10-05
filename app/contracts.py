"""Shared contracts for the SEO Health Checker.

These are plain dataclasses and enums (no Pydantic, no framework coupling) so
that internal modules can exchange structured data without depending on a
specific web framework. The Pydantic models in :mod:`app.schemas` translate
these contracts to/from JSON for the HTTP API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Severity(str, Enum):
    """Severity level of a discovered issue.

    Ordered from most to least severe so that comparisons and aggregation are
    intuitive: ``CRITICAL > WARNING > INFO``.
    """

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

    def __lt__(self, other: "Severity") -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
        return order[self] < order[other]


@dataclass
class Issue:
    """A single SEO finding produced by the analyzer/scorer.

    Each issue carries a stable ``code`` so callers can react to specific
    problems (e.g. ``"missing_title"``) regardless of wording changes. The
    ``message`` is in Persian (Farsi) for end-user display.
    """

    code: str
    """Stable machine-readable identifier, e.g. ``"missing_title"``."""

    severity: Severity
    """How severe the finding is: critical, warning, or info."""

    message: str
    """Human-readable message in Persian (Farsi)."""

    details: Optional[str] = None
    """Optional supporting context (selector, snippet, value, ...)."""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class FetchResult:
    """Outcome of a single safe page fetch.

    Produced by the page-fetching module and consumed by the HTML analyzer.
    The ``body`` is the raw response text (UTF-8 decoded by the fetcher) so the
    analyzer can parse it without re-decoding.
    """

    url: str
    """The URL that was actually requested."""

    final_url: str
    """The final URL after any redirects."""

    status: int
    """HTTP status code of the final response."""

    headers: Dict[str, str]
    """Response headers (keys lower-cased for easy lookup)."""

    body: str
    """Decoded response body text."""

    elapsed_ms: float
    """Total elapsed time for the request in milliseconds."""

    content_type: Optional[str] = None
    """``Content-Type`` header value, if present."""

    redirect_count: int = 0
    """Number of redirects followed to reach the final URL."""


@dataclass
class AnalysisResult:
    """Measurements and links discovered while analyzing a page."""

    measurements: Dict[str, float | int | bool | None] = field(
        default_factory=dict
    )
    """Named measurements produced by the analyzer (e.g. title length)."""

    internal_links: List[str] = field(default_factory=list)
    """Absolute URLs of discovered internal links on the page."""

    issues: List[Issue] = field(default_factory=list)
    """Issues generated from this page's measurements."""


@dataclass
class HealthReport:
    """Complete health-check result for one URL.

    Aggregates the fetch result, the analysis result, and the final score into
    a single object that the response schema serializes.
    """

    requested_url: str
    final_url: str
    status: int
    response_time_ms: float
    score: int
    measurements: Dict[str, float | int | bool | None]
    issues: List[Issue]

    @property
    def critical_issues(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.CRITICAL]

    @property
    def warning_issues(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def info_issues(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == Severity.INFO]