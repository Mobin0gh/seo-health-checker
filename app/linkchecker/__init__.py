"""Link checker module for SEO Health Checker.

Provides ``ResourceChecker`` and ``LinkChecker`` interfaces that other
modules can depend on. The implementations are in ``resources.py`` and
``linkchecker.py``, respectively.

The module keeps the public API simple: clients import the classes and
invoke their public methods. All internal result types are kept in the
``results`` submodule so they can be refined without touching shared
contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

import httpx

if TYPE_CHECKING:
    from app.linkchecker.results import (
        LinkCheckSummary,
        ResourceCheckResult,
        SitemapCheckResult,
    )


class ResourceChecker:
    """API for checking static resources (robots.txt, sitemap.xml)."""

    @staticmethod
    async def check_robots(
        base_url: str, deadline_ms: float, transport: Optional[httpx.AsyncBaseTransport] = None
    ) -> ResourceCheckResult:
        """Check ``/robots.txt`` on the origin of ``base_url``.

        See :func:`~app.linkchecker.resources.check_robots` for details.
        """
        from app.linkchecker.resources import check_robots as impl

        return await impl(base_url, deadline_ms, transport)

    @staticmethod
    async def check_sitemap(
        base_url: str, deadline_ms: float, transport: Optional[httpx.AsyncBaseTransport] = None
    ) -> SitemapCheckResult:
        """Check ``/sitemap.xml`` on the origin of ``base_url``.

        See :func:`~app.linkchecker.resources.check_sitemap` for details.
        """
        from app.linkchecker.resources import check_sitemap as impl

        return await impl(base_url, deadline_ms, transport)

    @staticmethod
    async def check_all(
        base_url: str, links: List[str], deadline_ms: float
    ) -> Tuple[ResourceCheckResult, SitemapCheckResult, LinkCheckSummary]:
        """Convenience method that runs all three checks sequentially.

        Returns ``robots_result``, ``sitemap_result``, and ``link_summary``.
        """
        from app.linkchecker.linkchecker import check_all as impl

        return await impl(base_url, links, deadline_ms)


class LinkChecker:
    """API for checking internal links discovered on a page."""

    @staticmethod
    async def check_links(links: List[str], deadline_ms: float) -> LinkCheckSummary:
        """Check a list of internal links with bounded concurrency.

        See :func:`~app.linkchecker.linkchecker.check_links` for details.
        """
        from app.linkchecker.linkchecker import check_links as impl

        return await impl(links, deadline_ms)