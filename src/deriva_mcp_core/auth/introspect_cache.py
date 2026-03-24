from __future__ import annotations

"""Short-TTL introspection result cache for deriva-mcp-core.

Caches IntrospectionResult objects keyed by bearer token string. A fixed TTL
(default 60 s) prevents a Credenza round-trip on every MCP protocol message
within a single connection burst (initialize, tools/list, etc.) while still
invalidating stale entries quickly enough for interactive use.

The cache entry expires at min(now + ttl_seconds, token.exp) so a cached
active result is never served for a token that has already expired upstream.

Per-token async locking prevents duplicate introspect calls for the same token
under concurrent requests (double-checked locking pattern). Lock creation is
safe without its own lock because asyncio is cooperative: the check-and-set on
_locks is never interrupted between the if and the assignment.

Usage:
    cache = IntrospectionCache(settings)

    result = await cache.get(token, settings)
"""

import asyncio
import time
from dataclasses import dataclass

from .introspect import IntrospectionResult, introspect
from ..config import Settings


@dataclass
class _CacheEntry:
    result: IntrospectionResult
    expires_at: float


class IntrospectionCache:
    """Cache of IntrospectionResult objects keyed by bearer token string."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ttl = settings.introspect_cache_ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _is_valid(self, entry: _CacheEntry) -> bool:
        return time.time() < entry.expires_at

    async def get(self, token: str) -> IntrospectionResult:
        """Return a cached IntrospectionResult, calling Credenza on miss.

        Args:
            token: The opaque bearer token to validate.

        Returns:
            An IntrospectionResult with active=True.

        Raises:
            TokenInactiveError: Token is inactive or expired.
            IntrospectionError: Introspection response is missing required fields.
            httpx.HTTPStatusError: Credenza returned a non-2xx response.
        """
        # Fast path: return cached result without acquiring any lock.
        entry = self._cache.get(token)
        if entry and self._is_valid(entry):
            return entry.result

        # Slow path: acquire per-token lock before calling Credenza.
        if token not in self._locks:
            self._locks[token] = asyncio.Lock()
        lock = self._locks[token]

        async with lock:
            # Re-check after acquiring lock.
            entry = self._cache.get(token)
            if entry and self._is_valid(entry):
                return entry.result

            result = await introspect(token, self._settings)

            # Expire at min(now + ttl, token exp) so we never serve an
            # active result past the token's own expiry.
            wall_expiry = time.time() + self._ttl
            if result.exp is not None:
                wall_expiry = min(wall_expiry, float(result.exp))

            self._cache[token] = _CacheEntry(result=result, expires_at=wall_expiry)
            return result

    def invalidate(self, token: str) -> None:
        """Explicitly evict a cached entry (e.g., on 401 from downstream)."""
        self._cache.pop(token, None)
