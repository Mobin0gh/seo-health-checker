"""Per-client-IP rolling-window rate limiter.

In-memory, per-process. No Redis, no configurable settings that could
weaken the 5-per-60-second or 10-second limits.

Concurrency is protected by an :class:`asyncio.Lock`.
Old timestamps are pruned on every check so tracked state does not
grow without bound.  Once the number of tracked IPs exceeds the
internal sweep threshold, a full sweep removes expired buckets.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque, Dict, Tuple

MAX_REQUESTS: int = 5
WINDOW_MS: int = 60 * 1000  # 60 seconds
_SWEEP_THRESHOLD: int = 1000


class RateLimitExceeded(Exception):
    """Raised when the client has exceeded the rolling budget.

    Carries ``retry_after_ms`` so the API layer can emit a correct
    ``Retry-After`` header.
    """

    def __init__(self, retry_after_ms: int) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after_ms = retry_after_ms


class RollingRateLimiter:
    """Rolling-window rate limiter keyed on client IP."""

    def __init__(
        self,
        max_requests: int = MAX_REQUESTS,
        window_ms: int = WINDOW_MS,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_ms < 1:
            raise ValueError("window_ms must be >= 1")
        self._max_requests = max_requests
        self._window_ms = window_ms
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _prune_bucket(self, now_ms: float, ip: str) -> Deque[float]:
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = deque()
            self._buckets[ip] = bucket
        cutoff = now_ms - self._window_ms
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def _sweep(self, now_ms: float) -> None:
        if len(self._buckets) < _SWEEP_THRESHOLD:
            return
        cutoff = now_ms - self._window_ms
        empty: list[str] = []
        for ip, b in self._buckets.items():
            while b and b[0] < cutoff:
                b.popleft()
            if not b:
                empty.append(ip)
        for ip in empty:
            del self._buckets[ip]

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def allow(self, ip: str) -> Tuple[bool, int]:
        """Record a request from ``ip`` and decide whether it is allowed.

        Returns ``(allowed, retry_after_ms)`` where ``retry_after_ms`` is
        > 0 when ``allowed`` is False.
        """
        if not ip:
            ip = "unknown"
        now_ms = time.monotonic() * 1000.0
        async with self._lock:
            bucket = self._prune_bucket(now_ms, ip)
            if len(bucket) < self._max_requests:
                bucket.append(now_ms)
                self._sweep(now_ms)
                return True, 0
            oldest = bucket[0]
            retry_after_ms = int(max(0.0, (oldest + self._window_ms) - now_ms)) + 1
            self._sweep(now_ms)
            return False, retry_after_ms

    async def check(self, ip: str) -> None:
        """Raise :class:`RateLimitExceeded` when the request is not allowed."""
        allowed, retry_after_ms = await self.allow(ip)
        if not allowed:
            raise RateLimitExceeded(retry_after_ms)

    @property
    def tracked_ips(self) -> int:  # pragma: no cover - test helper
        return len(self._buckets)

    def reset(self) -> None:  # pragma: no cover - test helper
        self._buckets.clear()