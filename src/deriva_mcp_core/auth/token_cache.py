"""Smart derived token cache for deriva-mcp-core.

Caches DERIVA-scoped derived tokens keyed by subject identifier (sub). On cache miss
or near-expiry, performs a token exchange against Credenza and stores the result.

Per-sub async locking prevents duplicate exchanges under concurrent requests for the
same user (double-checked locking pattern).

Usage:
    cache = DerivedTokenCache()

    # Inside auth middleware:
    derived_token = await cache.get(sub=result.sub, subject_token=bearer_token)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _CacheEntry:
    derived_token: str
    expires_at: float


class DerivedTokenCache:
    """Cache of derived DERIVA-scoped tokens keyed by subject identifier."""

    def __init__(self, buffer_seconds: int = 60) -> None:
        """
        Args:
            buffer_seconds: Treat a token as near-expiry this many seconds before
                its actual expiry. Triggers a proactive exchange to avoid serving
                a token that will expire mid-request.
        """
        self._buffer_seconds = buffer_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _is_valid(self, entry: _CacheEntry) -> bool:
        return time.time() < (entry.expires_at - self._buffer_seconds)

    async def get(self, sub: str, subject_token: str) -> str:
        """Return a valid derived token for sub, exchanging if necessary.

        Args:
            sub: Subject identifier from introspection (cache key).
            subject_token: The original bearer token, used if exchange is needed.

        Returns:
            A derived token valid for at least buffer_seconds.
        """
        # TODO (Phase 2): implement with per-sub locking and exchange call
        raise NotImplementedError

    def invalidate(self, sub: str) -> None:
        """Explicitly evict a cached entry (e.g., after a 401 from downstream)."""
        self._cache.pop(sub, None)
