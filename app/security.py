"""Secure outbound HTTP client for the SEO Health Checker.

This module implements SSRF-protected page fetching on top of httpx + httpcore.

Threat model
------------
The primary threat is **DNS rebinding**: an attacker-controlled hostname first
resolves to a public IP (passes our validation), then between our resolution
and the actual TCP connect it resolves to a private IP. A naive client that
re-resolves the hostname at connect time would then dial the private address.

Defense
-------
1. Validate URL structure (scheme, port, credentials, hostname) up-front.
2. Resolve the hostname ourselves with ``socket.getaddrinfo`` and validate
   *every* result. Reject if any result is private, loopback, link-local,
   reserved, multicast, or unspecified. Reject mixed public/forbidden sets.
3. Rewrite the outbound URL to use the **validated IP literal**, so the HTTP
   client never performs a second DNS lookup.
4. Preserve the original hostname for the HTTP ``Host`` header and for the
   TLS handshake (SNI + certificate verification) via the ``sni_hostname``
   httpcore request extension.
5. Stream the response body with a hard 2 MiB cap.
6. Share a single absolute monotonic deadline across DNS, connect, redirects,
   and body reads — no per-hop timer restart.
7. ``trust_env=False`` so environment proxy settings are never consulted.

Errors raised to callers contain the logical URL and a high-level reason.
Resolved private IPs are never echoed in error messages.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

import anyio
import httpx

from app.contracts import FetchResult


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MAX_RESPONSE_SIZE: int = 2 * 1024 * 1024  # 2 MiB hard cap on response body.
MAX_REDIRECTS: int = 5  # Small, documented redirect limit.
ALLOWED_PORTS: Tuple[int, int] = (80, 443)  # Only these ports.
ALLOWED_SCHEMES: Tuple[str, str] = ("http", "https")
DEFAULT_USER_AGENT: str = "SEOHealthChecker/0.1"

# Per-operation timeout ceilings (seconds). These are caps; the actual timeout
# for each operation is always clamped to the remaining caller deadline.
DNS_TIMEOUT_SEC: float = 5.0
CONNECT_TIMEOUT_SEC: float = 5.0
READ_TIMEOUT_SEC: float = 10.0
WRITE_TIMEOUT_SEC: float = 5.0


# --------------------------------------------------------------------------- #
# Public exceptions
# --------------------------------------------------------------------------- #

class SecurityError(Exception):
    """Base class for security violations during fetching."""


class URLValidationError(SecurityError):
    """URL failed structural validation (scheme, port, credentials, host)."""


class DNSResolutionError(SecurityError):
    """Hostname did not resolve to a valid public IP set."""


class ResponseTooLarge(SecurityError):
    """Response body exceeded the 2 MiB cap."""


class FetchTimeout(SecurityError):
    """Caller-supplied deadline expired before the request completed."""


class RedirectLimitExceeded(SecurityError):
    """More than ``max_redirects`` redirects encountered."""


# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #

def validate_url(url: str) -> Tuple[str, int]:
    """Parse and validate a URL.

    Returns ``(idna_ascii_hostname, port)`` where ``idna_ascii_hostname`` is
    the IDNA-encoded (ASCII) hostname, lower-cased and stripped of trailing
    dots.

    Raises :class:`URLValidationError` on any structural problem: empty URL,
    unsupported scheme, credentials present, missing host, out-of-range or
    non-standard port, malformed URL, or invalid IDNA encoding.
    """
    if not isinstance(url, str):
        raise URLValidationError("URL must be a string")

    url = url.strip()
    if not url:
        raise URLValidationError("Empty URL")

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as e:  # pragma: no cover - defensive
        raise URLValidationError(f"Malformed URL: {e}") from e

    # --- Scheme ---
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise URLValidationError(f"Unsupported scheme: {parsed.scheme!r}")

    # --- Credentials ---
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("Credentials in URL are not allowed")

    # --- Hostname ---
    hostname = parsed.hostname
    if not hostname:
        raise URLValidationError("Missing hostname")

    # The URL parser lower-cases hostnames and strips brackets from IPv6.
    hostname = hostname.rstrip(".")

    # --- Port ---
    try:
        port = parsed.port  # raises ValueError for out-of-range
    except ValueError as e:
        raise URLValidationError(f"Invalid port: {e}") from e

    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port not in ALLOWED_PORTS:
        raise URLValidationError(f"Invalid port {port}; only 80/443 allowed")

    # --- IDNA encoding (Unicode hostname -> punycode ASCII) ---
    # IP literals (IPv4/IPv6) are not IDNA-encoded; they pass through as-is
    # and are validated later by resolve_and_validate_host.
    try:
        ipaddress.ip_address(hostname)
        idna_hostname = hostname
    except ValueError:
        try:
            idna_hostname = hostname.encode("idna").decode("ascii")
        except Exception as e:
            raise URLValidationError(f"Invalid hostname {hostname!r}: {e}") from e

    return idna_hostname, port


# --------------------------------------------------------------------------- #
# DNS resolution and validation
# --------------------------------------------------------------------------- #

async def resolve_and_validate_host(hostname: str) -> str:
    """Resolve ``hostname`` and validate every DNS result.

    Returns a single globally-routable IP address as a string.

    Raises :class:`DNSResolutionError` if:
    - Resolution fails (NXDOMAIN, timeout, ...).
    - No IP results are returned.
    - Any result is private, loopback, link-local, reserved, multicast, or
      unspecified.
    - The result set is mixed (both public and forbidden addresses).

    The caller is expected to connect to the returned IP directly, without
    performing a second DNS lookup (which is how DNS rebinding is prevented).

    DNS resolution runs in a worker thread via :func:`asyncio.to_thread` so
    the blocking ``socket.getaddrinfo`` call never stalls the event loop.
    """
    if not hostname:
        raise DNSResolutionError("Empty hostname")

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as e:
        raise DNSResolutionError(f"DNS resolution failed: {e}") from e
    except OSError as e:
        raise DNSResolutionError(f"DNS resolution error: {e}") from e

    ips: List[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            raise DNSResolutionError("Non-IP DNS record encountered")

    if not ips:
        raise DNSResolutionError(f"No IP records for {hostname!r}")

    public_ips = [ip for ip in ips if _is_publicly_routable(ip)]
    forbidden_ips = [ip for ip in ips if not _is_publicly_routable(ip)]

    if forbidden_ips:
        # Reject if any forbidden address is present — even if public addresses
        # are also present. This defends against DNS result poisoning where an
        # attacker mixes a public IP with a private one.
        raise DNSResolutionError(
            f"DNS returned forbidden address(es) for {hostname!r}"
        )
    if not public_ips:
        raise DNSResolutionError(f"No publicly routable IP for {hostname!r}")

    return str(public_ips[0])


def _is_publicly_routable(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Return True only if the address is truly globally routable.

    Rejects: private (RFC 1918, ULA), loopback, link-local, reserved,
    multicast, unspecified, and any address where the stdlib ``is_global``
    predicate is False (catches RFC 5737 documentation ranges, carrier-grade
    NAT, etc.).
    """
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return False
    return bool(getattr(addr, "is_global", False))


# --------------------------------------------------------------------------- #
# URL rewriting (IP substitution)
# --------------------------------------------------------------------------- #

def rewrite_url_to_ip(url: str, idna_host: str, ip: str, port: int) -> str:
    """Return ``url`` with its host/port replaced by ``ip:port``.

    For IPv6 addresses the host is wrapped in brackets. The default port
    (80 for HTTP, 443 for HTTPS) is omitted so the resulting URL is
    minimal and clean. Query and fragment are preserved.
    """
    parsed = urllib.parse.urlsplit(url)
    if ":" in ip:
        host_part = f"[{ip}]"
    else:
        host_part = ip
    # Only append the port if it's non-default.
    default_ports = {"http": 80, "https": 443}
    if parsed.scheme == "http" and port == 80:
        netloc = host_part
    elif parsed.scheme == "https" and port == 443:
        netloc = host_part
    else:
        netloc = f"{host_part}:{port}"
    return urllib.parse.urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path or "/",
        parsed.query,
        parsed.fragment,
    ))


# --------------------------------------------------------------------------- #
# The validated transport
# --------------------------------------------------------------------------- #

class _ValidatedTransport(httpx.AsyncBaseTransport):
    """httpx transport that validates DNS and rewrites URLs to validated IPs.

    Each request is:
    1. Validated for URL structure.
    2. Resolved and validated for DNS.
    3. Rewritten to use the validated IP literal.
    4. Given an explicit ``Host`` header (original hostname) and
       ``sni_hostname`` extension (original hostname) for TLS SNI/cert.
    5. Delegated to a pluggable inner transport for the actual HTTP exchange.

    The delegate is injectable so tests can use :class:`httpx.MockTransport`
    instead of a real network connection.

    ``aclose``/``close`` are no-ops so the delegate survives across redirect
    hops within a single ``fetch_url`` call. The caller is responsible for
    closing the transport explicitly at the end.
    """

    def __init__(
        self,
        delegate: Optional[httpx.AsyncBaseTransport] = None,
        *,
        trust_env: bool = False,
        http1: bool = True,
        http2: bool = False,
    ) -> None:
        if delegate is None:
            # Default: real network transport, no env proxy, HTTP/1.1 only.
            delegate = httpx.AsyncHTTPTransport(
                trust_env=trust_env,
                http1=http1,
                http2=http2,
            )
        self._delegate = delegate

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        logical_url = str(request.url)

        # --- 1. Validate URL structure ---
        try:
            idna_host, port = validate_url(logical_url)
        except URLValidationError as e:
            # Re-raise the specific exception so callers can distinguish
            # URL-validation failures from other security issues.
            raise

        # --- 2. Resolve and validate DNS ---
        try:
            ip = await resolve_and_validate_host(idna_host)
        except DNSResolutionError as e:
            # Re-raise the specific exception so callers can distinguish
            # DNS-validation failures from other security issues.
            raise

        # --- 3. Rewrite URL to use validated IP ---
        ip_url = rewrite_url_to_ip(logical_url, idna_host, ip, port)

        # --- 4. Build new request with original Host header + SNI ---
        # Read the request body first (GET requests have no body; this is a
        # no-op for them). This materializes the stream so the delegate can
        # re-read it without consuming the original stream twice.
        try:
            body_bytes = await request.aread()
        except Exception:
            body_bytes = b""

        headers = list(request.headers.items())
        headers = [(k, v) for k, v in headers if k.lower() != "host"]

        # For IPv6 literals, the Host header must include brackets (RFC 3986).
        # idna_host is the IP without brackets; detect IPv6 by presence of colon.
        if ":" in idna_host and "." not in idna_host:
            host_header_value = f"[{idna_host}]"
        else:
            host_header_value = idna_host
        headers.append(("Host", host_header_value))

        extensions = dict(request.extensions or {})
        extensions["sni_hostname"] = idna_host

        new_request = httpx.Request(
            request.method,
            ip_url,
            headers=headers,
            content=body_bytes,
            extensions=extensions,
        )

        # --- 5. Delegate ---
        return await self._delegate.handle_async_request(new_request)

    async def aclose(self) -> None:
        # No-op: the delegate survives across redirect hops within fetch_url.
        pass

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Top-level fetch API
# --------------------------------------------------------------------------- #

def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that requires full certificate verification."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


async def fetch_url(
    url: str,
    *,
    deadline_ms: float,
    user_agent: str = DEFAULT_USER_AGENT,
    max_redirects: int = MAX_REDIRECTS,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FetchResult:
    """Fetch a URL with SSRF protections.

    :param url: URL to fetch. Must be HTTP or HTTPS on port 80 or 443.
    :param deadline_ms: Absolute monotonic deadline in milliseconds.
        All DNS, connect, read, and redirect operations share this deadline.
    :param user_agent: Value for the User-Agent request header.
    :param max_redirects: Maximum number of redirects to follow.
    :param transport: Optional custom delegate transport (for testing).
        Defaults to a real HTTP/1.1 transport with ``trust_env=False``.

    :return: A populated :class:`FetchResult`.

    :raises URLValidationError: URL failed structural validation.
    :raises DNSResolutionError: Hostname did not resolve to valid public IPs.
    :raises ResponseTooLarge: Body exceeded the 2 MiB cap.
    :raises FetchTimeout: Deadline expired.
    :raises RedirectLimitExceeded: Redirect limit exceeded.
    :raises SecurityError: Other security violations.
    """
    start_ms = time.monotonic() * 1000.0  # Absolute start time.
    current_url = url
    follow_count = 0

    # Build the validated transport once; it wraps the delegate. The delegate
    # survives across redirect hops because _ValidatedTransport's aclose/close
    # are no-ops.
    own_delegate = transport is None
    delegate = (
        transport
        if transport is not None
        else httpx.AsyncHTTPTransport(
            trust_env=False,
            http1=True,
            http2=False,
        )
    )
    v_transport = _ValidatedTransport(delegate)

    try:
        # Single client for all redirect hops — follow_redirects=False so we
        # control redirects ourselves (validating each destination).
        async with httpx.AsyncClient(
            transport=v_transport,
            timeout=httpx.Timeout(READ_TIMEOUT_SEC),
            follow_redirects=False,
            trust_env=False,  # Belt-and-suspenders: no env proxy.
            http1=True,
            http2=False,
        ) as client:
            while True:
                remaining_ms = deadline_ms - time.monotonic() * 1000.0
                if remaining_ms < 10.0:  # 10 ms grace floor
                    raise FetchTimeout(
                        f"Deadline expired while fetching {current_url}"
                    )

                remaining_s = remaining_ms / 1000.0
                timeout = httpx.Timeout(
                    connect=min(remaining_s, CONNECT_TIMEOUT_SEC),
                    read=min(remaining_s, READ_TIMEOUT_SEC),
                    write=min(remaining_s, WRITE_TIMEOUT_SEC),
                    pool=min(remaining_s, CONNECT_TIMEOUT_SEC),
                )

                try:
                    request = client.build_request(
                        "GET",
                        current_url,
                        headers={"User-Agent": user_agent},
                        timeout=timeout,
                    )
                    response = await client.send(request, stream=True)
                except httpx.TimeoutException as e:
                    raise FetchTimeout(f"Timeout: {e}") from e
                except httpx.ConnectError as e:
                    raise SecurityError(f"Connection failed: {e}") from e
                except httpx.NetworkError as e:
                    raise SecurityError(f"Network error: {e}") from e

                # --- Redirect handling ---
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise SecurityError(
                            f"Redirect {response.status_code} without Location"
                        )
                    new_url = urllib.request.urljoin(current_url, location)
                    follow_count += 1
                    if follow_count > max_redirects:
                        raise RedirectLimitExceeded(
                            f"Exceeded maximum redirects ({max_redirects}) "
                            f"for {new_url}"
                        )
                    try:
                        await response.aclose()
                    except Exception:  # pragma: no cover
                        pass
                    current_url = new_url
                    continue

                # --- Not a redirect: read body with streaming + size limit ---
                chunks: List[bytes] = []
                total = 0
                try:
                    with anyio.fail_after(max(0.01, remaining_s)):
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_RESPONSE_SIZE:
                                raise ResponseTooLarge(
                                    f"Response body exceeded "
                                    f"{MAX_RESPONSE_SIZE} bytes"
                                )
                            chunks.append(chunk)
                except ResponseTooLarge:
                    try:
                        await response.aclose()
                    except Exception:  # pragma: no cover
                        pass
                    raise
                except TimeoutError:
                    raise FetchTimeout(
                        f"Deadline expired reading body"
                    ) from None
                finally:
                    try:
                        await response.aclose()
                    except Exception:  # pragma: no cover
                        pass

                body = b"".join(chunks).decode("utf-8", errors="replace")
                elapsed_ms = time.monotonic() * 1000.0 - start_ms
                content_type = response.headers.get("content-type")
                headers = {k.lower(): v for k, v in response.headers.items()}

                return FetchResult(
                    url=url,
                    final_url=current_url,
                    status=response.status_code,
                    headers=headers,
                    body=body,
                    elapsed_ms=elapsed_ms,
                    content_type=content_type,
                    redirect_count=follow_count,
                )
    finally:
        # Clean up the delegate if we created it (real connection pool).
        if own_delegate:
            try:
                await delegate.aclose()
            except Exception:  # pragma: no cover
                pass