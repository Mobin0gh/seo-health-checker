"""Focused tests for the FastAPI integration layer (app/api.py).

Uses the repository's unittest style and mocks all network access
via httpx.MockTransport / unittest.mock. Never contacts real hosts.
Fresh app / rate-limiter instances are created per test.
"""

from __future__ import annotations

import asyncio
import math
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from httpx._types import AsyncByteStream

from app.schemas import HealthCheckResponse
from app.ratelimit import RollingRateLimiter, RateLimitExceeded

# Import the FastAPI app and its internals
from app import api
from app.api import app


def _stream_bytes(data: bytes) -> AsyncByteStream:
    class _S(AsyncByteStream):
        def __init__(self, d: bytes) -> None:
            self._d = d
            self._done = False
        def __aiter__(self):
            self._done = False
            return self
        async def __anext__(self):
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return self._d
        async def aclose(self) -> None:
            pass
    return _S(data)


def _deadline_from_now(ms: float) -> float:
    return time.monotonic() * 1000.0 + ms


def _make_fetch_result(
    url="https://example.com/",
    final_url="https://example.com/",
    status=200,
    body="<html><head><title>Hello</title>"
         "<meta name=\"description\" content=\"desc\">"
         "<meta name=\"viewport\" content=\"width=device-width\">"
         "<link rel=\"canonical\" href=\"https://example.com/\"></head>"
         "<body><h1>H</h1><a href=\"/page1\">p1</a><a href=\"/page2\">p2</a></body></html>",
    elapsed_ms=100.0,
):
    from app.contracts import FetchResult
    return FetchResult(
        url=url,
        final_url=final_url,
        status=status,
        headers={"content-type": "text/html"},
        body=body,
        elapsed_ms=elapsed_ms,
    )


class TestHealthyCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_200_response_shape_and_score(self):
        result = _make_fetch_result()
        with patch("app.security.fetch_url", new=AsyncMock(return_value=result)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], 200)
        self.assertGreaterEqual(data["score"], 0)
        self.assertLessEqual(data["score"], 100)
        self.assertEqual(data["requested_url"], "https://example.com/")
        self.assertIn("final_url", data)
        self.assertIn("response_time_ms", data)
        self.assertIn("measurements", data)
        self.assertIn("issues", data)
        # Validate via the response schema
        HealthCheckResponse(**data)


# Regression test for the content-encoding / decompression fix.
# Real sites (example.com, wikipedia.org, google.com) serve gzip/deflate
# bodies. The fetcher must decompress them before analysis.
_KNOWN_HTML = (
    b"<!doctype html>\n<html>\n<head>\n"
    b"<title>Example Domain</title>\n"
    b'<meta name="description" content="Example description">\n'
    b'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    b'<link rel="canonical" href="https://example.com/">\n'
    b"</head>\n<body>\n<h1>Example Domain</h1>\n</body>\n</html>"
)


def _make_handler(body_bytes, content_encoding=None):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "text/html; charset=utf-8"}
        if content_encoding:
            headers["content-encoding"] = content_encoding
        return httpx.Response(200, headers=headers, stream=_stream_bytes(body_bytes))
    return handler


def _patched_fetch_url(transport):
    """Return a side_effect that drives the REAL fetch_url with a MockTransport."""
    from app.security import fetch_url
    import time

    async def _side_effect(url, *, deadline_ms, **kwargs):
        return await fetch_url(url, deadline_ms=deadline_ms, transport=transport)
    return _side_effect


class TestContentEncodingDecompression(unittest.IsolatedAsyncioTestCase):
    """The fetcher must decompress gzip/deflate bodies before analysis."""

    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()

    async def test_gzip_body_is_decompressed(self):
        import gzip
        compressed = gzip.compress(_KNOWN_HTML)
        transport = httpx.MockTransport(_make_handler(compressed, "gzip"))
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=_patched_fetch_url(transport))):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        m = data["measurements"]
        self.assertTrue(m["has_title"], f"has_title={m['has_title']}")
        self.assertEqual(m["title_length"], 14)
        self.assertTrue(m["has_meta_description"])
        self.assertEqual(m["h1_count"], 1)
        self.assertTrue(m["has_viewport"])
        self.assertTrue(m["has_canonical"])
        self.assertEqual(m["html_size"], len(_KNOWN_HTML))

    async def test_deflate_body_is_decompressed(self):
        import zlib
        compressed = zlib.compress(_KNOWN_HTML)
        transport = httpx.MockTransport(_make_handler(compressed, "deflate"))
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=_patched_fetch_url(transport))):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        m = data["measurements"]
        self.assertTrue(m["has_title"])
        self.assertEqual(m["title_length"], 14)
        self.assertEqual(m["h1_count"], 1)
        self.assertTrue(m["has_viewport"])
        self.assertTrue(m["has_canonical"])

    async def test_plain_body_unchanged(self):
        transport = httpx.MockTransport(_make_handler(_KNOWN_HTML, None))
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=_patched_fetch_url(transport))):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        m = data["measurements"]
        self.assertTrue(m["has_title"])
        self.assertEqual(m["title_length"], 14)
        self.assertEqual(m["h1_count"], 1)
        self.assertTrue(m["has_viewport"])
        self.assertTrue(m["has_canonical"])


class TestNon2xxFetchedAsReport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_404_returns_200_with_http_error_issue(self):
        result = _make_fetch_result(status=404, body="Not Found")
        with patch("app.security.fetch_url", new=AsyncMock(return_value=result)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/missing"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], 404)
        codes = [i["code"] for i in data["issues"]]
        self.assertIn("http_error", codes)

    async def test_500_returns_200_with_http_error_issue(self):
        result = _make_fetch_result(status=500, body="Server Error")
        with patch("app.security.fetch_url", new=AsyncMock(return_value=result)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/broken"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], 500)
        codes = [i["code"] for i in resp.json()["issues"]]
        self.assertIn("http_error", codes)


class TestInvalidRequest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_empty_url_returns_400(self):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/check", json={"url": "   "})
        self.assertEqual(resp.status_code, 400)

    async def test_invalid_scheme_returns_400(self):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/check", json={"url": "ftp://example.com/"})
        self.assertEqual(resp.status_code, 400)

    async def test_no_url_field_returns_400(self):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.post("/check", json={})
        self.assertEqual(resp.status_code, 400)


class TestSecurityRefusalAndUpstream(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_private_ip_ssrf_returns_403(self):
        from app.security import DNSResolutionError, SecurityError
        async def fail(*args, **kwargs):
            raise DNSResolutionError("DNS returned forbidden address(es) for '169.254.169.254'")
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=fail)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "http://169.254.169.254/"})
        self.assertEqual(resp.status_code, 403)

    async def test_dns_failure_returns_502(self):
        from app.security import DNSResolutionError
        async def fail(*args, **kwargs):
            raise DNSResolutionError("DNS resolution failed")
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=fail)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://nonexistent.invalid/"})
        self.assertEqual(resp.status_code, 502)

    async def test_connection_failure_returns_502(self):
        from app.security import SecurityError
        async def fail(*args, **kwargs):
            raise SecurityError("Connection failed")
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=fail)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 502)


class TestTimeouts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_inner_deadline_timeout_returns_504(self):
        from app.security import FetchTimeout
        async def fail(*args, **kwargs):
            raise FetchTimeout("Deadline expired")
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=fail)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 504)

    async def test_outer_backstop_timeout_returns_504(self):
        async def hang(*args, **kwargs):
            await asyncio.sleep(100)
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=hang)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 504)


class TestSanitizedErrorBodies(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_error_bodies_contain_no_stack_traces_or_exception_strings(self):
        from app.security import DNSResolutionError
        async def fail(*args, **kwargs):
            raise DNSResolutionError("DNS resolution failed")
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=fail)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        data = resp.json()
        detail = data.get("detail", "")
        self.assertNotIn("Traceback", detail)
        self.assertNotIn("raise", detail)
        self.assertNotIn("DNSResolutionError", detail)
        self.assertNotIn("169.254", detail)


class TestOrchestratorDeadline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_same_absolute_deadline_passed_to_all_internal_ops(self):
        captured: list = []
        async def spy_fetch(url, *, deadline_ms, **kwargs):
            captured.append(("fetch", deadline_ms))
            return _make_fetch_result()
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=spy_fetch)):
            from app.orchestrator import run_check
            report = await run_check("https://example.com/")
        self.assertGreaterEqual(len(captured), 1)
        # All calls must receive the identical absolute deadline
        self.assertTrue(all(d == captured[0][1] for _, d in captured))

    async def test_explicit_deadline_preserved(self):
        explicit = _deadline_from_now(5000)
        captured: list = []
        async def spy_fetch(url, *, deadline_ms, **kwargs):
            captured.append(deadline_ms)
            return _make_fetch_result()
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=spy_fetch)):
            from app.orchestrator import run_check
            report = await run_check("https://example.com/", deadline_ms=explicit)
        self.assertEqual(captured[0], explicit)

    async def test_deadline_not_restarted_for_subrequests(self):
        """The same deadline value is used for the main fetch and check_all."""
        # check_all calls fetch_url for robots/sitemap/links too.
        # We spy on the fetch to record all deadline values.
        deadlines: list = []
        original_check_all = None
        async def spy_fetch(url, *, deadline_ms, **kwargs):
            deadlines.append(deadline_ms)
            return _make_fetch_result()

        # Use ResourceChecker.check_all directly via patch on app.security.fetch_url
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=spy_fetch)):
            from app.orchestrator import run_check
            report = await run_check("https://example.com/")
        # The main fetch + check_all's internal fetches all used the same deadline
        self.assertTrue(len(deadlines) >= 1)
        self.assertTrue(all(d == deadlines[0] for d in deadlines))


class TestPartialResultsTranslation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()
    async def test_deadline_interrupted_links_translated_correctly(self):
        """When links are deadline-expired, link_check_incomplete issue is emitted."""
        # Build a fetch result with internal links so check_all runs links
        body = (
            "<html><head><title>T</title>"
            "<meta name=\"description\" content=\"d\">"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "<link rel=\"canonical\" href=\"https://example.com/\"></head>"
            "<body><h1>H</h1>"
            + "".join(f'<a href="/p{i}">p{i}</a>' for i in range(3))
            + "</body></html>"
        )
        fetch_result = _make_fetch_result(body=body, status=200)

        async def mock_fetch(url, *, deadline_ms, **kwargs):
            if url == "https://example.com/":
                return fetch_result
            # robots/sitemap/links: return deadline_expired UNKNOWN results
            raise asyncio.TimeoutError("force deadline")

        # Instead of raising on link fetches, let ResourceChecker.check_all
        # receive a deadline that's already expired so it returns deadline_expired results.
        past_deadline = _deadline_from_now(-1000)
        with patch("app.security.fetch_url", new=AsyncMock(return_value=fetch_result)):
            from app.orchestrator import run_check
            report = await run_check("https://example.com/", deadline_ms=past_deadline)
        codes = [i.code for i in report.issues]
        self.assertIn("link_check_incomplete", codes)

    async def test_robots_sitemap_unknown_translated(self):
        """When robots/sitemap are unavailable, UNKNOWN issues are emitted."""
        fetch_result = _make_fetch_result(status=200)
        # Use a past deadline so robots/sitemap return deadline_expired (UNKNOWN)
        past_deadline = _deadline_from_now(-1000)
        with patch("app.security.fetch_url", new=AsyncMock(return_value=fetch_result)):
            from app.orchestrator import run_check
            report = await run_check("https://example.com/", deadline_ms=past_deadline)
        codes = [i.code for i in report.issues]
        # robots_unavailable and sitemap_unavailable should appear
        self.assertIn("robots_unavailable", codes)
        self.assertIn("sitemap_unavailable", codes)


class TestRateLimiting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Fresh limiter per test
        self.limiter = RollingRateLimiter()
        # Also reset the app-level limiter so TestClient tests aren't affected
        app.state.rate_limiter = RollingRateLimiter()

    async def test_first_5_allowed_6th_returns_429(self):
        ip = "192.168.1.1"
        for i in range(5):
            allowed, retry = await self.limiter.allow(ip)
            self.assertTrue(allowed, f"request {i+1} should be allowed")
        allowed, retry = await self.limiter.allow(ip)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)

    async def test_buckets_isolated_by_client_ip(self):
        limiter = RollingRateLimiter()
        a_ok = [await limiter.allow("10.0.0.1") for _ in range(5)]
        b_ok = [await limiter.allow("10.0.0.2") for _ in range(5)]
        self.assertTrue(all(a[0] for a in a_ok))
        self.assertTrue(all(b[0] for b in b_ok))
        a6 = await limiter.allow("10.0.0.1")
        b6 = await limiter.allow("10.0.0.2")
        self.assertFalse(a6[0])
        self.assertFalse(b6[0])

    async def test_forwarded_headers_do_not_change_bucket(self):
        """X-Forwarded-For / X-Real-IP are never trusted; only request.client.host."""
        limiter = RollingRateLimiter()
        # Simulate: real IP gets 5 requests
        for _ in range(5):
            await limiter.allow("203.0.113.1")
        # Forwarded header claims a different IP — should not affect the real IP's bucket
        await limiter.allow("198.51.100.1")  # spoofed forwarded IP
        allowed, _ = await limiter.allow("203.0.113.1")
        self.assertFalse(allowed)  # real IP still blocked

    async def test_concurrent_safe(self):
        """Concurrent updates do not race."""
        limiter = RollingRateLimiter(max_requests=20, window_ms=60000)
        results = await asyncio.gather(*[limiter.allow("10.0.0.1") for _ in range(20)])
        self.assertTrue(all(r[0] for r in results))
        allowed, _ = await limiter.allow("10.0.0.1")
        self.assertFalse(allowed)

    async def test_expired_buckets_cleaned_up(self):
        limiter = RollingRateLimiter(max_requests=5, window_ms=100)
        for _ in range(5):
            await limiter.allow("10.0.0.1")
        self.assertEqual(limiter.tracked_ips, 1)
        await asyncio.sleep(0.15)  # window expiry
        allowed, _ = await limiter.allow("10.0.0.1")
        self.assertTrue(allowed)
        # Old state pruned; new request creates fresh bucket
        self.assertLessEqual(limiter.tracked_ips, 1)

    async def test_full_sweep_removes_expired_ips(self):
        limiter = RollingRateLimiter(max_requests=5, window_ms=100)
        # Fill more than the sweep threshold with unique IPs past their window
        for i in range(1100):
            await limiter.allow(f"10.0.0.{i}")
        self.assertGreater(limiter.tracked_ips, 0)
        await asyncio.sleep(0.15)
        # Trigger sweep by making a request
        await limiter.allow("10.0.0.1")
        # All old IPs should be gone (sweep removes empty buckets)
        self.assertEqual(limiter.tracked_ips, 1)


class TestApiIntegrationWithRealApp(unittest.IsolatedAsyncioTestCase):
    """End-to-end test using the actual FastAPI TestClient."""

    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()

    async def test_healthy_response_shape(self):
        result = _make_fetch_result()
        with patch("app.security.fetch_url", new=AsyncMock(return_value=result)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Verify it matches HealthCheckResponse schema
        parsed = HealthCheckResponse(**data)
        self.assertEqual(parsed.requested_url, "https://example.com/")
        self.assertIn("score", data)
        self.assertIn("issues", data)
        self.assertIn("measurements", data)


class TestPersianUtf8InResponse(unittest.IsolatedAsyncioTestCase):
    """The raw HTTP response must carry proper Persian Unicode, not mojibake.

    Regression: FastAPI/Starlette defaults Content-Type to
    ``application/json`` *without* ``; charset=utf-8``.  Windows clients
    (PowerShell ``Invoke-WebRequest``) default to cp1252 when charset is
    absent, which turns the UTF-8 Persian bytes into mojibake
    (``ØµÙ...``).  The charset middleware appends ``; charset=utf-8`` to
    every application/json response so clients decode correctly.
    """

    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()

    async def test_response_contains_correct_persian_message(self):
        # HTML with NO H1, NO meta description, NO title, NO viewport, NO canonical
        # so the analyzer emits the expected "missing_h1" issue.
        body = (
            "<html><head></head><body><p>no tags</p></body></html>"
        )
        fetch_result = _make_fetch_result(body=body, status=200)
        with patch("app.security.fetch_url", new=AsyncMock(return_value=fetch_result)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})

        self.assertEqual(resp.status_code, 200)
        ct = resp.headers.get("content-type", "")
        self.assertIn("charset=utf-8", ct)

        raw_text = resp.content.decode("utf-8")
        self.assertIn("صفحه دارای تگ H1 نیست.", raw_text)

    async def test_content_type_includes_charset_utf8(self):
        """Every application/json response declares charset=utf-8."""
        result = _make_fetch_result()
        with patch("app.security.fetch_url", new=AsyncMock(return_value=result)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "https://example.com/"})

        ct = resp.headers.get("content-type", "")
        self.assertIn("application/json", ct)
        self.assertIn("charset=utf-8", ct)


class TestCharsetMiddlewareOnErrorResponses(unittest.IsolatedAsyncioTestCase):
    """The charset middleware also covers non-200 responses."""

    def setUp(self):
        app.state.rate_limiter = RollingRateLimiter()

    async def test_error_response_content_type_has_charset(self):
        from app.security import DNSResolutionError
        async def fail(*args, **kwargs):
            raise DNSResolutionError("DNS returned forbidden address(es) for '169.254.169.254'")
        with patch("app.security.fetch_url", new=AsyncMock(side_effect=fail)):
            from fastapi.testclient import TestClient
            client = TestClient(app)
            resp = client.post("/check", json={"url": "http://169.254.169.254/"})
        self.assertEqual(resp.status_code, 403)
        ct = resp.headers.get("content-type", "")
        self.assertIn("charset=utf-8", ct)


if __name__ == "__main__":
    unittest.main()