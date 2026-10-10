"""Rate limiting for the document ingestion API.

Provides an in-memory ``RateLimiter`` implementation for the single-instance
phase. A Redis-backed implementation is a planned M3 evolution item; the
protocol below is designed so that swapping implementations requires no
changes to the middleware or route handlers.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class RateLimitResult:
    """The outcome of a rate limit check."""

    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter(Protocol):
    """Protocol for rate limiting backends.

    Implementations should be safe to call from async request handlers.
    The Redis-backed variant (M3) will replace the in-memory store without
    changing the call sites.
    """

    def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        """Record one hit for ``key`` and check if it is within the limit."""
        ...


@dataclass
class _Window:
    """A sliding-window hit counter."""

    hits: list[float] = field(default_factory=list)


class InMemoryRateLimiter:
    """Sliding-window rate limiter using per-key hit lists.

    Suitable for single-instance deployment. Entries older than the window
    are pruned lazily on each call, so memory usage stays bounded by the
    number of distinct keys within their respective windows.
    """

    def __init__(self) -> None:
        """Create an empty rate limiter."""
        self._windows: dict[str, _Window] = defaultdict(_Window)

    def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> RateLimitResult:
        """Record a hit and return whether the request is allowed."""
        now = time.monotonic()
        window = self._windows[key]
        cutoff = now - window_seconds
        window.hits[:] = [t for t in window.hits if t > cutoff]

        if len(window.hits) >= limit:
            oldest = window.hits[0]
            retry_after = max(1, int(cutoff - oldest + window_seconds))
            return RateLimitResult(
                allowed=False,
                remaining=0,
                retry_after_seconds=retry_after,
            )

        window.hits.append(now)
        remaining = limit - len(window.hits)
        return RateLimitResult(
            allowed=True,
            remaining=remaining,
            retry_after_seconds=0,
        )
