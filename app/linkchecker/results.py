"""Local result types for the link checker.

These dataclasses and enums are **local** to ``app/linkchecker``. They are not
part of the shared contracts in :mod:`app.contracts` and must not be imported
by other modules. The orchestrator (Task 6) will translate them into shared
:class:`app.contracts.Issue` values and measurements.

The three-state existence model
-------------------------------
``ResourceExists`` distinguishes three states so callers can tell apart *the
resource was fetched successfully* from *we could not determine whether it
exists*:

* ``TRUE``  — a definitive positive response (2xx) was received.
* ``FALSE`` — a definitive negative response (404) was received.
* ``UNKNOWN`` — the response was non-definitive (e.g. 403, 500, DNS failure,
  timeout, oversized body, redirect limit) or no response was received at all.

This is intentionally **not** a plain ``bool | None``: an explicit enum makes
the contract self-documenting and prevents callers from conflating "unknown"
with "absent".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResourceExists(Enum):
    """Three-state existence verdict for a fetched resource."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        """Allow ``if resource.exists`` style checks against TRUE."""
        return self is ResourceExists.TRUE


class LinkStatus(Enum):
    """High-level classification of a single link check."""

    WORKING = "working"
    BROKEN = "broken"


@dataclass
class ResourceCheckResult:
    """Outcome of checking a single resource (robots.txt / sitemap.xml)."""

    url: str
    """The URL that was actually checked."""

    exists: ResourceExists
    """Three-state existence verdict."""

    status: Optional[int] = None
    """HTTP status code of the final response, if a response was received."""

    reason: Optional[str] = None
    """Short machine-readable reason when ``exists`` is UNKNOWN or FALSE."""

    detail: Optional[str] = None
    """Human-readable detail (e.g. the underlying exception text)."""

    body: Optional[bytes] = None
    """Raw response body bytes, if a body was received. May be ``None`` when no
    response body was available (e.g. a redirect-only response)."""

    elapsed_ms: float = 0.0
    """Time spent on the fetch in milliseconds."""


@dataclass
class SitemapCheckResult:
    """Outcome of checking a sitemap resource.

    Existence and XML validity are tracked **separately**:

    * ``exists`` reflects whether the HTTP resource was fetched successfully
      (2xx). A 2xx response means the resource exists even if its body is empty
      or invalid XML.
    * ``valid`` reflects whether the body parsed as a well-formed sitemap of an
      supported root element. This is independent of ``exists``.
    """

    url: str
    """The URL that was actually checked."""

    exists: ResourceExists
    """Three-state existence verdict for the HTTP resource."""

    valid: bool = False
    """Whether the body parsed as a well-formed sitemap of a supported root."""

    status: Optional[int] = None
    """HTTP status code of the final response, if a response was received."""

    reason: Optional[str] = None
    """Short machine-readable reason when ``exists`` is UNKNOWN or FALSE."""

    detail: Optional[str] = None
    """Human-readable detail (e.g. the underlying exception text)."""

    url_count: int = 0
    """Number of direct ``url`` children (``urlset``) or ``sitemap`` children
    (``sitemapindex``). Zero when the document is invalid or has a wrong root."""

    root_tag: Optional[str] = None
    """Local name of the document root element, if parsed."""

    body: Optional[bytes] = None
    """Raw response body bytes, if a body was received."""

    elapsed_ms: float = 0.0
    """Time spent on the fetch in milliseconds."""


@dataclass
class LinkCheckResult:
    """Outcome of checking a single internal link."""

    url: str
    """The URL that was checked."""

    status: LinkStatus
    """Whether the link is working or broken."""

    http_status: Optional[int] = None
    """HTTP status code of the final response, if a response was received."""

    reason: Optional[str] = None
    """Short machine-readable reason when the link is broken without a status
    (e.g. ``timeout``, ``dns_failure``, ``url_validation``,
    ``response_too_large``, ``redirect_limit``)."""

    detail: Optional[str] = None
    """Human-readable detail (e.g. the underlying exception text)."""

    elapsed_ms: float = 0.0
    """Time spent on the fetch in milliseconds."""


@dataclass
class LinkCheckSummary:
    """Aggregate outcome of a bounded link check run."""

    results: list[LinkCheckResult] = field(default_factory=list)
    """Per-link results, in the **input order** of the deduplicated links."""

    checked_count: int = 0
    """Number of links actually checked (after dedup and the 20-link cap)."""

    broken_count: int = 0
    """Number of broken links in ``results``."""

    @property
    def broken_links(self) -> list[LinkCheckResult]:
        """Convenience accessor returning only the broken results."""
        return [r for r in self.results if r.status is LinkStatus.BROKEN]