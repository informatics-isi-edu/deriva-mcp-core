from __future__ import annotations

"""Smart derived token cache for deriva-mcp-core.

Caches DERIVA-scoped derived tokens keyed by principal (iss/sub). On cache miss
or near-expiry, performs a token exchange against Credenza and stores the result.

Keying on iss/sub rather than bare sub prevents collisions when the server
accepts tokens from multiple issuers (e.g., Keycloak and Globus) that could
independently produce the same sub value.

Per-principal async locking prevents duplicate exchanges under concurrent requests
for the same user (double-checked locking pattern). Lock creation is safe without
its own lock because asyncio is single-threaded: the check-and-set on _locks is
never interrupted by another coroutine.

Usage:
    cache = DerivedTokenCache(settings)

    # Inside auth verifier:
    derived_token = await cache.get(principal=principal, subject_token=bearer_token)
"""
import asyncio
import time
from dataclasses import dataclass
from .exchange import exchange
from ..config import Settings
from ..telemetry import audit_event


@dataclass
class _CacheEntry:
    derived_token: str
    expires_at: float


class DerivedTokenCache:
    """Cache of derived DERIVA-scoped tokens keyed by principal (iss/sub)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._buffer_seconds = settings.token_cache_buffer_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _is_valid(self, entry: _CacheEntry) -> bool:
        return time.time() < (entry.expires_at - self._buffer_seconds)

    async def get(self, principal: str, subject_token: str) -> str:
        """Return a valid derived token for principal, exchanging if necessary.

        Args:
            principal: Fully-qualified identity string (iss/sub) used as cache key.
            subject_token: The original bearer token, used if exchange is needed.

        Returns:
            A derived token valid for at least buffer_seconds from now.
        """
        # Fast path: return cached token without acquiring any lock.
        entry = self._cache.get(principal)
        if entry and self._is_valid(entry):
            return entry.derived_token

        # Slow path: acquire per-principal lock before exchange.
        # Lock creation is safe here -- asyncio is cooperative and the check-and-set
        # on _locks is never interrupted between the `if` and the assignment.
        if principal not in self._locks:
            self._locks[principal] = asyncio.Lock()
        lock = self._locks[principal]

        async with lock:
            # Re-check after acquiring lock: another coroutine may have exchanged
            # while we were waiting.
            entry = self._cache.get(principal)
            if entry and self._is_valid(entry):
                return entry.derived_token

            result = await exchange(subject_token, self._settings)
            self._cache[principal] = _CacheEntry(
                derived_token=result.access_token,
                expires_at=result.expires_at,
            )
            audit_event("token_exchange_success", principal=principal, expires_in=result.expires_in)
            return result.access_token

    def invalidate(self, principal: str) -> None:
        """Explicitly evict a cached entry (e.g., after a 401 from downstream)."""
        self._cache.pop(principal, None)
