"""Resource checking (robots.txt, sitemap.xml) for SEO Health Checker."""

from __future__ import annotations

import time
import urllib.parse
from typing import Optional
from xml.etree import ElementTree as ET

import httpx
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
    LinkStatus,
    ResourceCheckResult,
    ResourceExists,
    SitemapCheckResult,
)


def _origin_from_url(url: str) -> str:
    """Return the origin (scheme://host[:port]) of ``url``.

    Preserves scheme, netloc, and explicit non-default ports.
    Query, path, and fragment are discarded.
    """
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("url must have scheme and netloc")
    # netloc already contains host[:port] if port is present/explicit
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _deadline_expired(deadline_ms: float) -> bool:
    """Return True if the absolute monotonic deadline has already passed."""
    return time.monotonic() * 1000.0 >= deadline_ms


async def check_robots(
    base_url: str, deadline_ms: float, transport: Optional[httpx.AsyncBaseTransport] = None
) -> ResourceCheckResult:
    """Check ``/robots.txt`` on the origin of ``base_url``.

    Args:
        base_url: Final page URL (after redirects) whose origin defines the
            robots.txt location.
        deadline_ms: Absolute monotonic deadline in milliseconds. The same
            value is passed to every :func:`app.security.fetch_url` call.

    Returns:
        A populated :class:`ResourceCheckResult`. The caller translates this
        into a shared :class:`app.contracts.Issue` if needed.
    """
    # Build the URL before checking deadline so it's always populated.
    robots_url = urllib.parse.urljoin(_origin_from_url(base_url), "/robots.txt")

    # If the deadline is already expired, return a clearly identified
    # unverified/deadline-expired result without scheduling any work.
    if _deadline_expired(deadline_ms):
        return ResourceCheckResult(
            url=robots_url,
            exists=ResourceExists.UNKNOWN,
            reason="deadline_expired",
            detail="Absolute monotonic deadline expired before scheduling",
        )
    return await _fetch_resource(robots_url, deadline_ms, "robots", transport)


async def check_sitemap(
    base_url: str, deadline_ms: float, transport: Optional[httpx.AsyncBaseTransport] = None
) -> SitemapCheckResult:
    """Check ``/sitemap.xml`` on the origin of ``base_url``.

    Args:
        base_url: Final page URL (after redirects) whose origin defines the
            sitemap location.
        deadline_ms: Absolute monotonic deadline in milliseconds. The same
            value is passed to every :func:`app.security.fetch_url` call.
        transport: Optional custom httpx transport (for testing).

    Returns:
        A populated :class:`SitemapCheckResult`. Existence and validity are
        tracked separately.
    """
    # Build the URL before checking deadline so it's always populated.
    sitemap_url = urllib.parse.urljoin(_origin_from_url(base_url), "/sitemap.xml")

    # If the deadline is already expired, return a clearly identified
    # unverified/deadline-expired result without scheduling any work.
    if _deadline_expired(deadline_ms):
        return SitemapCheckResult(
            url=sitemap_url,
            exists=ResourceExists.UNKNOWN,
            reason="deadline_expired",
            detail="Absolute monotonic deadline expired before scheduling",
        )
    result = await _fetch_resource(sitemap_url, deadline_ms, "sitemap", transport)
    # Convert generic ResourceCheckResult into a sitemap-specific result
    sitemap_result = SitemapCheckResult(
        url=result.url,
        exists=result.exists,
        status=result.status,
        reason=result.reason,
        detail=result.detail,
        body=result.body,
        elapsed_ms=result.elapsed_ms,
        valid=False,
        url_count=0,
        root_tag=None,
    )
    if result.exists is ResourceExists.TRUE and result.body is not None:
        # Attempt to parse the XML and determine validity/url_count
        try:
            # Parse raw bytes directly. We pre-scan for DOCTYPE to reject DTDs
            # (internal or external subset) before parsing, since stdlib
            # XMLParser does not support resolve_entities=False in Python 3.14+.
            body_lower = result.body.lower()
            if b"<!doctype" in body_lower:
                # Presence of a doctype means there is a DTD (internal or external
                # subset). Reject outright.
                sitemap_result.reason = "dtd_not_allowed"
                sitemap_result.detail = "Sitemap contains a DTD"
                # exists stays TRUE (we got a 2xx), valid remains FALSE
                return sitemap_result

            root = ET.fromstring(result.body)
            # Extract the local name (strip namespace if present).
            tag = root.tag
            if isinstance(tag, str) and tag.startswith("{"):
                # {namespace}localname -> localname
                tag = tag[tag.find("}") + 1 :]
            sitemap_result.root_tag = tag
            if tag in ("urlset", "sitemapindex"):
                sitemap_result.valid = True
                if tag == "urlset":
                    # Count direct children named "url" (namespace-aware)
                    ns = ""
                    if isinstance(root.tag, str) and root.tag.startswith("{"):
                        ns = root.tag[: root.tag.find("}") + 1]
                    url_tag = ns + "url"
                    sitemap_result.url_count = sum(
                        1 for e in root if e.tag == url_tag
                    )
                else:  # sitemapindex
                    ns = ""
                    if isinstance(root.tag, str) and root.tag.startswith("{"):
                        ns = root.tag[: root.tag.find("}") + 1]
                    sitemap_tag = ns + "sitemap"
                    sitemap_result.url_count = sum(
                        1 for e in root if e.tag == sitemap_tag
                    )
            else:
                # Wrong root (e.g. <rss>, <html>, etc.)
                sitemap_result.valid = False
                sitemap_result.reason = "wrong_root"
                sitemap_result.detail = f"Sitemap root is <{tag}>, expected <urlset> or <sitemapindex>"
        except ET.ParseError as e:
            # Invalid XML - expected malformed XML should be handled explicitly.
            sitemap_result.valid = False
            sitemap_result.reason = "invalid_xml"
            sitemap_result.detail = str(e)
            # exists stays TRUE (we got a 2xx), valid remains FALSE
    return sitemap_result


async def _fetch_resource(
    url: str, deadline_ms: float, kind: str, transport: Optional[httpx.AsyncBaseTransport] = None
) -> ResourceCheckResult:
    """Shared implementation for robots.txt and sitemap.xml fetching.

    Args:
        url: Absolute URL to fetch.
        deadline_ms: Absolute monotonic deadline in milliseconds.
        kind: Either "robots" or "sitemap" – used only for reason strings.

    Returns:
        A populated :class:`ResourceCheckResult`.
    """
    # NOTE: We deliberately do *not* catch broad Exception here. Only the
    # documented fetch/security exceptions are caught; programming errors
    # (AttributeError, etc.) must surface.
    try:
        fetch_result = await app.security.fetch_url(
            url,
            deadline_ms=deadline_ms,
            user_agent="SEOHealthChecker/0.1",
            transport=transport,
        )
        # Translate FetchResult into our three-state existence model
        if 200 <= fetch_result.status < 300:
            exists = ResourceExists.TRUE
            reason = None
            detail = None
        elif fetch_result.status == 404:
            exists = ResourceExists.FALSE
            reason = "not_found"
            detail = f"{kind} not found (404)"
        else:
            # Any other status (4xx other than 404, 5xx, etc.) is non-definitive
            exists = ResourceExists.UNKNOWN
            reason = f"http_status_{fetch_result.status}"
            detail = f"Non-definitive HTTP status {fetch_result.status}"
        return ResourceCheckResult(
            url=url,
            exists=exists,
            status=fetch_result.status,
            reason=reason,
            detail=detail,
            body=fetch_result.body.encode("utf-8", errors="replace")
            if fetch_result.body
            else None,
            elapsed_ms=fetch_result.elapsed_ms,
        )
    except URLValidationError as e:
        # Invalid URL structure (should not happen for origin-built URLs)
        return ResourceCheckResult(
            url=url,
            exists=ResourceExists.UNKNOWN,
            reason="url_validation",
            detail=str(e),
        )
    except DNSResolutionError as e:
        return ResourceCheckResult(
            url=url,
            exists=ResourceExists.UNKNOWN,
            reason="dns_failure",
            detail=str(e),
        )
    except ResponseTooLarge as e:
        return ResourceCheckResult(
            url=url,
            exists=ResourceExists.UNKNOWN,
            reason="response_too_large",
            detail=str(e),
        )
    except FetchTimeout as e:
        return ResourceCheckResult(
            url=url,
            exists=ResourceExists.UNKNOWN,
            reason="timeout",
            detail=str(e),
        )
    except RedirectLimitExceeded as e:
        return ResourceCheckResult(
            url=url,
            exists=ResourceExists.UNKNOWN,
            reason="redirect_limit",
            detail=str(e),
        )
    except SecurityError as e:
        # Catch-all for other security errors (e.g. connection refused)
        return ResourceCheckResult(
            url=url,
            exists=ResourceExists.UNKNOWN,
            reason="security_error",
            detail=str(e),
        )