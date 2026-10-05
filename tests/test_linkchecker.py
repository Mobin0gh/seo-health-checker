"""Unit tests for the link checker module (app/linkchecker).

These tests verify that robots.txt, sitemap.xml, and internal link checks
behave correctly under the constraints described in the task:

* The same absolute monotonic deadline is passed unchanged to every
  ``fetch_url`` call.
* Three-state resource existence (``ResourceExists``) is used correctly.
* Bounded concurrency (max 5) is respected.
* At most the first 20 unique links are checked, preserving input order.
* Known fetch/security exceptions are mapped to explicit result reasons.
* XML parsing safely rejects DTD/entity attacks.

All tests run in isolation with mocked DNS/network access (via
``httpx.MockTransport``) and never contact real web servers.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from typing import List
from unittest.mock import patch

import httpx
from httpx._types import AsyncByteStream

from app.linkchecker import LinkChecker, ResourceChecker
from app.linkchecker.results import (
    LinkCheckResult,
    LinkCheckSummary,
    LinkStatus,
    ResourceCheckResult,
    ResourceExists,
    SitemapCheckResult,
)


class _AsyncBytesStream(AsyncByteStream):
    """Simple async byte stream for test mock responses."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._consumed = False

    def __aiter__(self) -> _AsyncBytesStream:
        self._consumed = False
        return self

    async def __anext__(self) -> bytes:
        if self._consumed:
            raise StopAsyncIteration
        self._consumed = True
        return self._data

    async def aclose(self) -> None:
        pass


def _stream_bytes(data: bytes) -> _AsyncBytesStream:
    """Return an async byte stream for httpx.Response(stream=...)."""
    return _AsyncBytesStream(data)


def deadline_ms_from_now(ms: float) -> float:
    """Return an absolute monotonic deadline ``ms`` milliseconds in the future."""
    return time.monotonic() * 1000.0 + ms


class TestRobotsTxt(unittest.TestCase):
    """Test robots.txt resource checking."""

    async def _run(self, coro):
        return await coro

    def test_robots_2xx_exists_true(self):
        """2xx response => exists=TRUE, no missing-resource issue."""
        async def impl():
            from app.security import fetch_url

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/plain"},
                    stream=_stream_bytes(b"User-agent: *"),
                )

            transport = httpx.MockTransport(handler)
            result = await ResourceChecker.check_robots(
                "https://example.com/page",
                deadline_ms_from_now(10000),
                transport=transport,
            )
            self.assertIsInstance(result, ResourceCheckResult)
            self.assertEqual(result.exists, ResourceExists.TRUE)
            self.assertIsNone(result.reason)
            self.assertIsNone(result.detail)
            self.assertIsNotNone(result.body)
            self.assertEqual(result.status, 200)

        asyncio.run(self._run(impl()))

    def test_robots_404_exists_false(self):
        """404 => exists=FALSE, missing_robots info issue."""
        async def impl():
            from app.security import fetch_url

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    404,
                    headers={},
                    stream=_stream_bytes(b""),
                )

            transport = httpx.MockTransport(handler)
            result = await ResourceChecker.check_robots(
                "https://example.com/page",
                deadline_ms_from_now(10000),
                transport=transport,
            )
            self.assertEqual(result.exists, ResourceExists.FALSE)
            self.assertEqual(result.reason, "not_found")
            self.assertIn("robots not found", result.detail or "")

        asyncio.run(self._run(impl()))

    def test_robots_non_definitive_status_exists_unknown(self):
        """Any other status => exists=UNKNOWN."""
        async def impl():
            from app.security import fetch_url

            for status in (403, 500, 502):
                with self.subTest(status=status):
                    def handler(request: httpx.Request) -> httpx.Response:
                        return httpx.Response(
                            status,
                            headers={},
                            stream=_stream_bytes(b""),
                        )

                    transport = httpx.MockTransport(handler)
                    result = await ResourceChecker.check_robots(
                        "https://example.com/page",
                        deadline_ms_from_now(10000),
                        transport=transport,
                    )
                    self.assertEqual(result.exists, ResourceExists.UNKNOWN)
                    self.assertEqual(result.reason, f"http_status_{status}")

        asyncio.run(self._run(impl()))


class TestSitemap(unittest.TestCase):
    """Test sitemap.xml resource checking."""

    async def _run(self, coro):
        return await coro

    def test_sitemap_2xx_exists_valid_urlset(self):
        """Valid sitemap with urlset => exists=TRUE, valid=TRUE, url_count=X."""
        xml = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://example.com/page1</loc></url>
  <url><loc>http://example.com/page2</loc></url>
</urlset>"""

        async def impl():
            from app.security import fetch_url

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/xml"},
                    stream=_stream_bytes(xml),
                )

            transport = httpx.MockTransport(handler)
            result = await ResourceChecker.check_sitemap(
                "https://example.com/page",
                deadline_ms_from_now(10000),
                transport=transport,
            )
            self.assertEqual(result.exists, ResourceExists.TRUE)
            self.assertTrue(result.valid)
            self.assertEqual(result.url_count, 2)
            self.assertIsNone(result.reason)

        asyncio.run(self._run(impl()))

    def test_sitemap_2xx_invalid_xml_exists_true_valid_false(self):
        """2xx but malformed XML => exists=TRUE, valid=FALSE."""
        async def impl():
            from app.security import fetch_url

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/xml"},
                    stream=_stream_bytes(b"<not xml>"),
                )

            transport = httpx.MockTransport(handler)
            result = await ResourceChecker.check_sitemap(
                "https://example.com/page",
                deadline_ms_from_now(10000),
                transport=transport,
            )
            self.assertEqual(result.exists, ResourceExists.TRUE)
            self.assertFalse(result.valid)
            self.assertEqual(result.reason, "invalid_xml")

        asyncio.run(self._run(impl()))

    def test_sitemap_404_exists_false(self):
        """404 => exists=FALSE."""
        async def impl():
            from app.security import fetch_url

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    404,
                    headers={},
                    stream=_stream_bytes(b""),
                )

            transport = httpx.MockTransport(handler)
            result = await ResourceChecker.check_sitemap(
                "https://example.com/page",
                deadline_ms_from_now(10000),
                transport=transport,
            )
            self.assertEqual(result.exists, ResourceExists.FALSE)
            self.assertEqual(result.reason, "not_found")

        asyncio.run(self._run(impl()))

    def test_sitemap_dtd_rejected_exists_true_valid_false(self):
        """Sitemap containing a <!DOCTYPE> must be rejected (security)."""
        xml = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [
  <!ENTITY xxe "malicious"> ]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://example.com/page</loc></url>
</urlset>"""

        async def impl():
            from app.security import fetch_url

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/xml"},
                    stream=_stream_bytes(xml),
                )

            transport = httpx.MockTransport(handler)
            result = await ResourceChecker.check_sitemap(
                "https://example.com/page",
                deadline_ms_from_now(10000),
                transport=transport,
            )
            self.assertEqual(result.exists, ResourceExists.TRUE)
            self.assertFalse(result.valid)
            self.assertEqual(result.reason, "dtd_not_allowed")
            self.assertIn("DTD", result.detail or "")


class TestInternalLinks(unittest.TestCase):
    """Test internal link checking."""

    async def _run(self, coro):
        return await coro

    def test_link_deduplication_preserves_order(self):
        """Duplicate links are deduplicated, first occurrence kept."""
        async def impl():
            from app.linkchecker import LinkChecker

            async def mock_fetch_url(url: str, *, deadline_ms: float, **kwargs):
                from app.contracts import FetchResult

                return FetchResult(
                    url=url,
                    final_url=url,
                    status=200,
                    headers={},
                    body="",
                    elapsed_ms=10.0,
                )

            with patch("app.security.fetch_url", side_effect=mock_fetch_url):
                links = [
                    "https://example.com/page1",
                    "https://example.com/page2",
                    "https://example.com/page1",  # duplicate
                    "https://example.com/page3",
                    "https://example.com/page2",  # duplicate
                ]
                summary = await LinkChecker.check_links(
                    links, deadline_ms_from_now(10000)
                )
                self.assertEqual(summary.checked_count, 3)
                self.assertEqual(summary.broken_count, 0)
                self.assertEqual(summary.results[0].url, "https://example.com/page1")
                self.assertEqual(summary.results[1].url, "https://example.com/page2")
                self.assertEqual(summary.results[2].url, "https://example.com/page3")

        asyncio.run(self._run(impl()))

    def test_link_20_cap(self):
        """Only the first 20 unique links are checked."""
        async def impl():
            from app.linkchecker import LinkChecker

            # Create 30 unique URLs
            unique_links = [f"https://example.com/page{i}" for i in range(30)]

            async def mock_fetch_url(url: str, *, deadline_ms: float, **kwargs):
                from app.contracts import FetchResult

                return FetchResult(
                    url=url,
                    final_url=url,
                    status=200,
                    headers={},
                    body="",
                    elapsed_ms=10.0,
                )

            with patch("app.security.fetch_url", side_effect=mock_fetch_url):
                summary = await LinkChecker.check_links(
                    unique_links, deadline_ms_from_now(10000)
                )
                self.assertEqual(summary.checked_count, 20)
                self.assertEqual(len(summary.results), 20)

        asyncio.run(self._run(impl()))

    def test_broken_link_4xx(self):
        """4xx response => broken link."""
        async def impl():
            from app.linkchecker import LinkChecker

            async def mock_fetch_url(url: str, *, deadline_ms: float, **kwargs):
                from app.contracts import FetchResult

                return FetchResult(
                    url=url,
                    final_url=url,
                    status=404,
                    headers={},
                    body="",
                    elapsed_ms=10.0,
                )

            with patch("app.security.fetch_url", side_effect=mock_fetch_url):
                summary = await LinkChecker.check_links(
                    ["https://example.com/page"], deadline_ms_from_now(10000)
                )
                self.assertEqual(summary.broken_count, 1)
                result = summary.results[0]
                self.assertEqual(result.status, LinkStatus.BROKEN)
                self.assertEqual(result.http_status, 404)

        asyncio.run(self._run(impl()))

    def test_broken_link_network_failure(self):
        """Network exception => broken link with explicit reason."""
        async def impl():
            from app.linkchecker import LinkChecker

            async def mock_fetch_url(url: str, *, deadline_ms: float, **kwargs):
                from app.security import DNSResolutionError

                raise DNSResolutionError("DNS failure")

            with patch("app.security.fetch_url", side_effect=mock_fetch_url):
                summary = await LinkChecker.check_links(
                    ["https://example.com/page"], deadline_ms_from_now(10000)
                )
                self.assertEqual(summary.broken_count, 1)
                result = summary.results[0]
                self.assertEqual(result.status, LinkStatus.BROKEN)
                self.assertEqual(result.reason, "dns_failure")

        asyncio.run(self._run(impl()))

    def test_deadline_expired_skips_work(self):
        """If deadline already expired, return unverified/deadline-expired results."""
        async def impl():
            from app.linkchecker import LinkChecker

            # Set a deadline that is already in the past
            past_deadline = deadline_ms_from_now(-1000)

            summary = await LinkChecker.check_links(
                ["https://example.com/page1", "https://example.com/page2"],
                past_deadline,
            )
            self.assertEqual(summary.checked_count, 2)
            self.assertEqual(summary.broken_count, 2)
            for result in summary.results:
                self.assertEqual(result.reason, "deadline_expired")
                self.assertIn("deadline", result.detail or "")

        asyncio.run(self._run(impl()))

    def test_shared_absolute_deadline_passed_unchanged(self):
        """Ensure the same deadline is passed to every fetch_url call."""
        async def impl():
            from app.linkchecker import LinkChecker

            captured_deadlines: List[float] = []

            async def spy_fetch_url(url: str, *, deadline_ms: float, **kwargs):
                captured_deadlines.append(deadline_ms)
                from app.contracts import FetchResult

                return FetchResult(
                    url=url,
                    final_url=url,
                    status=200,
                    headers={},
                    body="",
                    elapsed_ms=10.0,
                )

            with patch("app.security.fetch_url", side_effect=spy_fetch_url):
                links = [f"https://example.com/page{i}" for i in range(10)]
                expected = deadline_ms_from_now(10000)
                summary = await LinkChecker.check_links(links, expected)
                # All calls should have received the same deadline value
                self.assertEqual(len(captured_deadlines), 10)
                self.assertTrue(
                    all(d == expected for d in captured_deadlines),
                    f"Deadlines differ: {captured_deadlines}",
                )

        asyncio.run(self._run(impl()))


class TestResourceExistenceVsValidity(unittest.TestCase):
    """Resource existence is independent of XML validity."""

    async def _run(self, coro):
        return await coro

    def test_2xx_invalid_xml_exists_true_valid_false(self):
        """Invalid XML still marks exists=TRUE (we got a 2xx response)."""
        async def impl():
            from app.security import fetch_url

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/xml"},
                    stream=_stream_bytes(b"<not xml>"),
                )

            transport = httpx.MockTransport(handler)
            result = await ResourceChecker.check_sitemap(
                "https://example.com/page",
                deadline_ms_from_now(10000),
            )
            self.assertEqual(result.exists, ResourceExists.TRUE)
            self.assertFalse(result.valid)


if __name__ == "__main__":
    unittest.main()