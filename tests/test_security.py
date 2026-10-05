"""Unit tests for the secure outbound HTTP client (app/security.py).

These tests verify that the fetcher enforces the SSRF and input-validation
requirements documented in the task description.

Because the environment has no outbound internet connectivity, all tests use
httpx.MockTransport to simulate network interactions. DNS is also mocked
(monkey-patch socket.getaddrinfo) so we can inject controlled address lists.

The test class inherits from unittest.TestCase so it can be run by pytest
(which discovers unittest tests) or directly via ``python -m unittest``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import unittest
from typing import AsyncIterator
from unittest.mock import patch

import httpx
from httpx._types import AsyncByteStream

from app.security import (
    DNSResolutionError,
    FetchTimeout,
    ResponseTooLarge,
    RedirectLimitExceeded,
    SecurityError,
    URLValidationError,
    _is_publicly_routable,
    fetch_url,
    resolve_and_validate_host,
    rewrite_url_to_ip,
    validate_url,
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


class TestURLValidation(unittest.TestCase):
    """Test structural URL validation."""

    def test_valid_http_url(self):
        host, port = validate_url("http://example.com/")
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 80)

    def test_valid_https_url(self):
        host, port = validate_url("https://example.com/")
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 443)

    def test_url_with_path_and_query(self):
        host, port = validate_url(
            "https://example.com/path/to/resource?query=value#frag"
        )
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 443)

    def test_url_normalizes_hostname(self):
        host, port = validate_url("http://Example.COM/")
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 80)

    def test_url_strips_trailing_dot(self):
        host, port = validate_url("http://example.com./")
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 80)

    def test_ipv4_literal_hostname(self):
        host, port = validate_url("http://8.8.8.8/")
        self.assertEqual(host, "8.8.8.8")
        self.assertEqual(port, 80)

    def test_ipv6_literal_hostname(self):
        host, port = validate_url("http://[2606:4700:4700::1111]/")
        self.assertEqual(host, "2606:4700:4700::1111")
        self.assertEqual(port, 80)

    def test_rejects_empty_url(self):
        with self.assertRaises(URLValidationError):
            validate_url("")

    def test_rejects_missing_scheme(self):
        with self.assertRaises(URLValidationError):
            validate_url("example.com/")

    def test_rejects_unsupported_scheme(self):
        for scheme in ("ftp", "file", "ws", "wss"):
            with self.subTest(scheme=scheme):
                with self.assertRaises(URLValidationError):
                    validate_url(f"{scheme}://example.com/")

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(URLValidationError):
            validate_url("http://user:pass@example.com/")

    def test_rejects_missing_hostname(self):
        with self.assertRaises(URLValidationError):
            validate_url("http:///")

    def test_rejects_out_of_range_port(self):
        with self.assertRaises(URLValidationError):
            validate_url("http://example.com:99999/")

    def test_rejects_non_standard_port(self):
        for port in (22, 8080, 8443):
            with self.subTest(port=port):
                with self.assertRaises(URLValidationError):
                    validate_url(f"http://example.com:{port}/")

    def test_rejects_invalid_idna_hostname(self):
        with self.assertRaises(URLValidationError):
            validate_url("http://example..com/")  # empty label

    def test_rejects_idna_label_too_long(self):
        # Label longer than 63 bytes after IDNA encoding.
        long_label = "x" * 64
        with self.assertRaises(URLValidationError):
            validate_url(f"http://{long_label}.com/")


class TestDNSResolution(unittest.TestCase):
    """Test hostname resolution and IP validation."""

    def test_resolve_public_ipv4(self):
        ip = resolve_and_validate_host("8.8.8.8")
        self.assertEqual(ip, "8.8.8.8")

    def test_resolve_public_ipv6(self):
        ip = resolve_and_validate_host("2606:4700:4700::1111")
        self.assertEqual(ip, "2606:4700:4700::1111")

    def test_resolve_hostname_with_a_record(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]
            ip = resolve_and_validate_host("example.com")
            self.assertEqual(ip, "93.184.216.34")
            mock_getaddrinfo.assert_called_once_with(
                "example.com", None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )

    def test_resolve_hostname_with_aaaa_record(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0),
                )
            ]
            ip = resolve_and_validate_host("example.com")
            self.assertEqual(ip, "2606:2800:220:1:248:1893:25c8:1946")

    def test_resolve_hostname_mixed_a_and_aaaa(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0),
                ),
            ]
            # Should return the first (A record) because both are public.
            ip = resolve_and_validate_host("example.com")
            self.assertEqual(ip, "93.184.216.34")

    def test_rejects_no_dns_records(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = []  # NXDOMAIN-like
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("example.com")

    def test_rejects_private_ipv4(self):
        for private in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with self.subTest(private=private):
                with patch("socket.getaddrinfo") as mock_getaddrinfo:
                    mock_getaddrinfo.return_value = [
                        (socket.AF_INET, socket.SOCK_STREAM, 0, "", (private, 0))
                    ]
                    with self.assertRaises(DNSResolutionError):
                        resolve_and_validate_host("evil.com")

    def test_rejects_ipv4_loopback(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv4_link_local(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.0.1", 0))
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv4_multicast(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("224.0.0.1", 0))
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv4_reserved(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("240.0.0.1", 0))
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv4_unspecified(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("0.0.0.0", 0))
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv6_loopback(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0))
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv6_link_local(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("fe80::1", 0, 0, 0),
                )
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv6_multicast(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("ff02::1", 0, 0, 0),
                )
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv6_reserved(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("2001:db8::1", 0, 0, 0),
                )
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_ipv6_unspecified(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::", 0, 0, 0))
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_mixed_public_and_forbidden_dns(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),  # public
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),  # loopback
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_uga_addresses(self):
        # RFC 4193 Unique Local Addresses (fd00::/8) should be rejected.
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    0,
                    "",
                    ("fd12:3456:789a::1", 0, 0, 0),
                )
            ]
            with self.assertRaises(DNSResolutionError):
                resolve_and_validate_host("evil.com")

    def test_rejects_documentation_range(self):
        # RFC 5737 IPv4 documentation addresses (should be rejected by is_global).
        for doc in ("192.0.2.1", "198.51.100.1", "203.0.113.1"):
            with self.subTest(doc=doc):
                with patch("socket.getaddrinfo") as mock_getaddrinfo:
                    mock_getaddrinfo.return_value = [
                        (socket.AF_INET, socket.SOCK_STREAM, 0, "", (doc, 0))
                    ]
                    with self.assertRaises(DNSResolutionError):
                        resolve_and_validate_host("evil.com")


class TestPublicRoutable(unittest.TestCase):
    """Test _is_publicly_routable helper."""

    def test_public_ipv4(self):
        self.assertTrue(_is_publicly_routable(ipaddress.ip_address("8.8.8.8")))
        self.assertTrue(_is_publicly_routable(ipaddress.ip_address("1.1.1.1")))

    def test_public_ipv6(self):
        self.assertTrue(
            _is_publicly_routable(ipaddress.ip_address("2606:4700:4700::1111"))
        )
        self.assertTrue(
            _is_publicly_routable(ipaddress.ip_address("2a04:4e42::1"))
        )

    def test_private_ipv4(self):
        for addr in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
            with self.subTest(addr=addr):
                self.assertFalse(
                    _is_publicly_routable(ipaddress.ip_address(addr))
                )

    def test_ipv4_loopback(self):
        self.assertFalse(_is_publicly_routable(ipaddress.ip_address("127.0.0.1")))

    def test_ipv4_link_local(self):
        self.assertFalse(
            _is_publicly_routable(ipaddress.ip_address("169.254.0.1"))
        )

    def test_ipv4_multicast(self):
        self.assertFalse(_is_publicly_routable(ipaddress.ip_address("224.0.0.1")))

    def test_ipv4_reserved(self):
        self.assertFalse(_is_publicly_routable(ipaddress.ip_address("240.0.0.1")))

    def test_ipv4_unspecified(self):
        self.assertFalse(_is_publicly_routable(ipaddress.ip_address("0.0.0.0")))

    def test_ipv6_loopback(self):
        self.assertFalse(_is_publicly_routable(ipaddress.ip_address("::1")))

    def test_ipv6_link_local(self):
        self.assertFalse(
            _is_publicly_routable(ipaddress.ip_address("fe80::1"))
        )

    def test_ipv6_multicast(self):
        self.assertFalse(_is_publicly_routable(ipaddress.ip_address("ff02::1")))

    def test_ipv6_reserved(self):
        self.assertFalse(
            _is_publicly_routable(ipaddress.ip_address("2001:db8::1"))
        )

    def test_ipv6_unspecified(self):
        self.assertFalse(_is_publicly_routable(ipaddress.ip_address("::")))

    def test_documentation_range_v4(self):
        for addr in ("192.0.2.1", "198.51.100.1", "203.0.113.1"):
            with self.subTest(addr=addr):
                self.assertFalse(
                    _is_publicly_routable(ipaddress.ip_address(addr))
                )


class TestURLRewriting(unittest.TestCase):
    """Test rewrite_url_to_ip function."""

    def test_rewrite_ipv4(self):
        url = "http://example.com/path?q=1"
        rewritten = rewrite_url_to_ip(url, "example.com", "93.184.216.34", 80)
        self.assertEqual(rewritten, "http://93.184.216.34/path?q=1")

    def test_rewrite_ipv4_https(self):
        url = "https://example.com/"
        rewritten = rewrite_url_to_ip(url, "example.com", "93.184.216.34", 443)
        self.assertEqual(rewritten, "https://93.184.216.34/")

    def test_rewrite_ipv6(self):
        url = "http://example.com/"
        rewritten = rewrite_url_to_ip(
            url, "example.com", "2606:4700:4700::1111", 80
        )
        self.assertEqual(
            rewritten, "http://[2606:4700:4700::1111]/"
        )

    def test_rewrite_ipv6_https(self):
        url = "https://example.com/"
        rewritten = rewrite_url_to_ip(
            url, "example.com", "2606:4700:4700::1111", 443
        )
        self.assertEqual(
            rewritten, "https://[2606:4700:4700::1111]/"
        )

    def test_rewrite_preserves_query_and_fragment(self):
        url = "https://example.com/path?q=1#frag"
        rewritten = rewrite_url_to_ip(url, "example.com", "1.2.3.4", 443)
        self.assertEqual(rewritten, "https://1.2.3.4/path?q=1#frag")


class TestFetchURLSuccess(unittest.IsolatedAsyncioTestCase):
    """Test successful fetches using MockTransport."""

    async def test_successful_http_fetch(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.url.host, "93.184.216.34")
                # Default port is omitted in the rewritten URL, so httpx reports None.
                self.assertIsNone(request.url.port)
                self.assertEqual(request.headers["Host"], "example.com")
                # sni_hostname extension should be present for TLS (though not used for HTTP)
                self.assertIn("sni_hostname", request.extensions)
                self.assertEqual(request.extensions["sni_hostname"], "example.com")
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    stream=_stream_bytes(b"<html><title>OK</title></html>"),
                )

            transport = httpx.MockTransport(handler)
            result = await fetch_url(
                "http://example.com/",
                deadline_ms=deadline_ms_from_now(5000),
                transport=transport,
            )
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, "<html><title>OK</title></html>")
            self.assertEqual(result.url, "http://example.com/")
            self.assertEqual(result.final_url, "http://example.com/")
            self.assertEqual(result.redirect_count, 0)
            self.assertGreaterEqual(result.elapsed_ms, 0)

    async def test_successful_https_fetch(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.url.host, "93.184.216.34")
                # Default port is omitted in the rewritten URL, so httpx reports None.
                self.assertIsNone(request.url.port)
                self.assertEqual(request.headers["Host"], "example.com")
                self.assertIn("sni_hostname", request.extensions)
                self.assertEqual(request.extensions["sni_hostname"], "example.com")
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=_stream_bytes(b'{"ok": true}'),
                )

            transport = httpx.MockTransport(handler)
            result = await fetch_url(
                "https://example.com/api",
                deadline_ms=deadline_ms_from_now(5000),
                transport=transport,
            )
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, '{"ok": true}')
            self.assertEqual(result.content_type, "application/json")

    async def test_fetch_with_ipv6_literal(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "2606:4700:4700::1111")
            # Default port is omitted in the rewritten URL, so httpx reports None.
            self.assertIsNone(request.url.port)
            self.assertEqual(request.headers["Host"], "[2606:4700:4700::1111]")  # literal
            self.assertIn("sni_hostname", request.extensions)
            self.assertEqual(
                request.extensions["sni_hostname"], "2606:4700:4700::1111"
            )
            return httpx.Response(200, stream=_stream_bytes(b"ipv6-ok"))

        transport = httpx.MockTransport(handler)
        result = await fetch_url(
            "http://[2606:4700:4700::1111]/",
            deadline_ms=deadline_ms_from_now(5000),
            transport=transport,
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, "ipv6-ok")

    async def test_fetch_redirect_loop_protection(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First hop: redirect to same host (simulating a loop)
                return httpx.Response(
                    302,
                    headers={"location": "http://example.com/"},
                )
            # Second hop: return the actual content
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                stream=_stream_bytes(b"final"),
            )

        transport = httpx.MockTransport(handler)
        result = await fetch_url(
            "http://example.com/",
            deadline_ms=deadline_ms_from_now(5000),
            max_redirects=3,
            transport=transport,
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, "final")
        self.assertEqual(result.redirect_count, 1)  # one redirect followed

    async def test_fetch_respects_redirect_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            # Always redirect, creating an infinite loop if not limited.
            return httpx.Response(
                301,
                headers={"location": "http://example.com/"},
            )

        transport = httpx.MockTransport(handler)
        with self.assertRaises(RedirectLimitExceeded):
            await fetch_url(
                "http://example.com/",
                deadline_ms=deadline_ms_from_now(5000),
                max_redirects=2,
                transport=transport,
            )


class TestFetchURLFailures(unittest.IsolatedAsyncioTestCase):
    """Test failure cases."""

    async def test_url_validation_error_propagates(self):
        with self.assertRaises(URLValidationError):
            await fetch_url("", deadline_ms=deadline_ms_from_now(1000))

    async def test_dns_resolution_error_propagates(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.side_effect = socket.gaierror(
                socket.EAI_AGAIN, "Temporary failure"
            )
            with self.assertRaises(DNSResolutionError):
                await fetch_url(
                    "http://example.com/",
                    deadline_ms=deadline_ms_from_now(5000),
                )

    async def test_dns_returns_forbidden_ip(self):
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))
            ]
            with self.assertRaises(SecurityError):  # wrapped by fetch_url
                await fetch_url(
                    "http://example.com/",
                    deadline_ms=deadline_ms_from_now(5000),
                )

    async def test_response_too_large(self):
        def handler(request: httpx.Request) -> httpx.Response:
            # Return a body larger than MAX_RESPONSE_SIZE (2 MiB)
            huge = b"x" * (3 * 1024 * 1024)  # 3 MiB
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                stream=_stream_bytes(huge),
            )

        transport = httpx.MockTransport(handler)
        with self.assertRaises(ResponseTooLarge):
            await fetch_url(
                "http://example.com/",
                deadline_ms=deadline_ms_from_now(5000),
                transport=transport,
            )

    async def test_deadline_expired_during_dns(self):
        # Use a vanishingly small deadline so DNS (which we fake-slow) times out.
        # We cannot easily patch socket.getaddrinfo to be async, so we test
        # the timeout during the actual connect/read by making the transport sleep.
        def handler(request: httpx.Request) -> httpx.Response:
            # Simulate a server that never sends the first byte.
            # The transport's timeout should trigger.
            # We'll rely on the transport timeout rather than trying to sleep in DNS.
            raise httpx.TimeoutException("read timeout")

        transport = httpx.MockTransport(handler)
        with self.assertRaises(FetchTimeout):
            await fetch_url(
                "http://example.com/",
                deadline_ms=deadline_ms_from_now(50),  # 50 ms total deadline
                transport=transport,
            )

    async def test_deadline_expired_redirect_chain(self):
        step = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal step
            step += 1
            # Small delay to simulate network time and trigger deadline
            await asyncio.sleep(0.03)
            if step <= 10:
                return httpx.Response(
                    302,
                    headers={"location": f"http://example.com/{step}"},
                )
            return httpx.Response(200, stream=_stream_bytes(b"done"))

        transport = httpx.MockTransport(handler)
        with self.assertRaises(FetchTimeout):
            await fetch_url(
                "http://example.com/",
                deadline_ms=deadline_ms_from_now(200),  # too short for 10 redirects + final
                max_redirects=20,
                transport=transport,
            )


if __name__ == "__main__":
    unittest.main()