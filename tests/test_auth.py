"""Unit tests for the Phase 2 auth layer.

All Credenza HTTP calls are mocked via pytest-httpx. No live Credenza required.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from deriva_mcp_core.auth.exchange import ExchangeError, ExchangeResult, exchange
from deriva_mcp_core.auth.introspect import (
    IntrospectionError,
    IntrospectionResult,
    TokenInactiveError,
    introspect,
)
from deriva_mcp_core.auth.introspect_cache import IntrospectionCache
from deriva_mcp_core.auth.token_cache import DerivedTokenCache
from deriva_mcp_core.config import Settings

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_TOKEN = "test-bearer-token"
_SUB = "user@example.org"
_DERIVED_TOKEN = "derived-token-xyz"
_EXPIRES_IN = 1800

_ISS = "https://credenza.example.org"

_ACTIVE_PAYLOAD = {
    "active": True,
    "sub": _SUB,
    "iss": _ISS,
    "aud": ["urn:deriva:rest:service:mcp"],
    "exp": int(time.time()) + 3600,
}

_EXCHANGE_PAYLOAD = {
    "access_token": _DERIVED_TOKEN,
    "token_type": "bearer",
    "expires_in": _EXPIRES_IN,
}


def _introspect_url(settings: Settings) -> str:
    return f"{settings.credenza_url}/introspect"


def _token_url(settings: Settings) -> str:
    return f"{settings.credenza_url}/token"


# ---------------------------------------------------------------------------
# introspect()
# ---------------------------------------------------------------------------


async def test_introspect_success(httpx_mock, test_settings):
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)
    result = await introspect(_TOKEN, test_settings)
    assert isinstance(result, IntrospectionResult)
    assert result.active is True
    assert result.sub == _SUB
    assert result.iss == _ISS
    assert result.aud == ["urn:deriva:rest:service:mcp"]
    assert result.exp is not None
    assert result.payload == _ACTIVE_PAYLOAD


async def test_introspect_sends_resource_param(httpx_mock, test_settings):
    """Introspect POST must include resource= so Credenza enforces audience binding."""
    from urllib.parse import parse_qs
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)
    await introspect(_TOKEN, test_settings)
    req = httpx_mock.get_requests()[0]
    body = parse_qs(req.content.decode())
    assert "resource" in body
    assert body["resource"][0] == test_settings.server_resource


async def test_introspect_aud_string_normalized_to_list(httpx_mock, test_settings):
    """RFC 7662 aud may be a plain string; we normalize it to a list."""
    payload = {**_ACTIVE_PAYLOAD, "aud": "urn:deriva:rest:service:mcp"}
    httpx_mock.add_response(url=_introspect_url(test_settings), json=payload)
    result = await introspect(_TOKEN, test_settings)
    assert result.aud == ["urn:deriva:rest:service:mcp"]


async def test_introspect_inactive_token_raises(httpx_mock, test_settings):
    httpx_mock.add_response(url=_introspect_url(test_settings), json={"active": False})
    with pytest.raises(TokenInactiveError):
        await introspect(_TOKEN, test_settings)


async def test_introspect_missing_sub_raises(httpx_mock, test_settings):
    payload = {**_ACTIVE_PAYLOAD}
    del payload["sub"]
    httpx_mock.add_response(url=_introspect_url(test_settings), json=payload)
    with pytest.raises(IntrospectionError, match="sub"):
        await introspect(_TOKEN, test_settings)


async def test_introspect_http_error_raises(httpx_mock, test_settings):
    httpx_mock.add_response(url=_introspect_url(test_settings), status_code=401)
    import httpx as _httpx

    with pytest.raises(_httpx.HTTPStatusError):
        await introspect(_TOKEN, test_settings)


async def test_introspect_missing_aud_defaults_to_empty_list(httpx_mock, test_settings):
    payload = {k: v for k, v in _ACTIVE_PAYLOAD.items() if k != "aud"}
    httpx_mock.add_response(url=_introspect_url(test_settings), json=payload)
    result = await introspect(_TOKEN, test_settings)
    assert result.aud == []


# ---------------------------------------------------------------------------
# exchange()
# ---------------------------------------------------------------------------


async def test_exchange_success(httpx_mock, test_settings):
    before = time.time()
    httpx_mock.add_response(url=_token_url(test_settings), json=_EXCHANGE_PAYLOAD)
    result = await exchange(_TOKEN, test_settings)
    assert isinstance(result, ExchangeResult)
    assert result.access_token == _DERIVED_TOKEN
    assert result.expires_in == _EXPIRES_IN
    assert result.expires_at >= before + _EXPIRES_IN


async def test_exchange_missing_access_token_raises(httpx_mock, test_settings):
    httpx_mock.add_response(url=_token_url(test_settings), json={"token_type": "bearer"})
    with pytest.raises(ExchangeError, match="access_token"):
        await exchange(_TOKEN, test_settings)


async def test_exchange_http_error_raises(httpx_mock, test_settings):
    httpx_mock.add_response(url=_token_url(test_settings), status_code=400)
    import httpx as _httpx

    with pytest.raises(_httpx.HTTPStatusError):
        await exchange(_TOKEN, test_settings)


async def test_exchange_sends_correct_grant_type(httpx_mock, test_settings):
    """Verify the RFC 8693 grant type and token type URNs are sent correctly."""
    httpx_mock.add_response(url=_token_url(test_settings), json=_EXCHANGE_PAYLOAD)
    await exchange(_TOKEN, test_settings)
    request = httpx_mock.get_requests()[0]
    body = request.content.decode()
    assert "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange" in body
    assert "resource=" in body
    assert test_settings.deriva_resource.replace(":", "%3A") in body


# ---------------------------------------------------------------------------
# DerivedTokenCache
# ---------------------------------------------------------------------------


async def test_cache_miss_triggers_exchange(httpx_mock, test_settings):
    httpx_mock.add_response(url=_token_url(test_settings), json=_EXCHANGE_PAYLOAD)
    cache = DerivedTokenCache(test_settings)
    token = await cache.get(sub=_SUB, subject_token=_TOKEN)
    assert token == _DERIVED_TOKEN
    assert len(httpx_mock.get_requests()) == 1


async def test_cache_hit_does_not_exchange(httpx_mock, test_settings):
    httpx_mock.add_response(url=_token_url(test_settings), json=_EXCHANGE_PAYLOAD)
    cache = DerivedTokenCache(test_settings)
    first = await cache.get(sub=_SUB, subject_token=_TOKEN)
    second = await cache.get(sub=_SUB, subject_token=_TOKEN)
    assert first == second == _DERIVED_TOKEN
    # Only one exchange should have been made
    assert len(httpx_mock.get_requests()) == 1


async def test_cache_near_expiry_triggers_exchange(httpx_mock, test_settings):
    """A cached entry within buffer_seconds of expiry should be re-exchanged."""
    # First exchange: returns a token that is already within the buffer window
    near_expiry_payload = {
        "access_token": "near-expiry-token",
        "token_type": "bearer",
        "expires_in": test_settings.token_cache_buffer_seconds - 1,
    }
    fresh_payload = {**_EXCHANGE_PAYLOAD, "access_token": "fresh-token"}
    httpx_mock.add_response(url=_token_url(test_settings), json=near_expiry_payload)
    httpx_mock.add_response(url=_token_url(test_settings), json=fresh_payload)

    cache = DerivedTokenCache(test_settings)
    first = await cache.get(sub=_SUB, subject_token=_TOKEN)
    assert first == "near-expiry-token"

    # Second call should detect near-expiry and re-exchange
    second = await cache.get(sub=_SUB, subject_token=_TOKEN)
    assert second == "fresh-token"
    assert len(httpx_mock.get_requests()) == 2


async def test_cache_invalidate_forces_exchange(httpx_mock, test_settings):
    httpx_mock.add_response(url=_token_url(test_settings), json=_EXCHANGE_PAYLOAD)
    fresh_payload = {**_EXCHANGE_PAYLOAD, "access_token": "fresh-token"}
    httpx_mock.add_response(url=_token_url(test_settings), json=fresh_payload)

    cache = DerivedTokenCache(test_settings)
    await cache.get(sub=_SUB, subject_token=_TOKEN)
    cache.invalidate(_SUB)
    second = await cache.get(sub=_SUB, subject_token=_TOKEN)
    assert second == "fresh-token"
    assert len(httpx_mock.get_requests()) == 2


async def test_cache_concurrent_requests_exchange_once(httpx_mock, test_settings):
    """Concurrent get() calls for the same sub must only exchange once."""
    httpx_mock.add_response(url=_token_url(test_settings), json=_EXCHANGE_PAYLOAD)
    cache = DerivedTokenCache(test_settings)

    results = await asyncio.gather(
        cache.get(sub=_SUB, subject_token=_TOKEN),
        cache.get(sub=_SUB, subject_token=_TOKEN),
        cache.get(sub=_SUB, subject_token=_TOKEN),
    )

    assert all(r == _DERIVED_TOKEN for r in results)
    assert len(httpx_mock.get_requests()) == 1


async def test_cache_independent_subs_exchange_independently(httpx_mock, test_settings):
    """Different subs each get their own exchange and cache entry."""
    httpx_mock.add_response(
        url=_token_url(test_settings),
        json={**_EXCHANGE_PAYLOAD, "access_token": "token-for-alice"},
    )
    httpx_mock.add_response(
        url=_token_url(test_settings),
        json={**_EXCHANGE_PAYLOAD, "access_token": "token-for-bob"},
    )
    cache = DerivedTokenCache(test_settings)
    alice = await cache.get(sub="alice@example.org", subject_token="alice-bearer")
    bob = await cache.get(sub="bob@example.org", subject_token="bob-bearer")
    assert alice == "token-for-alice"
    assert bob == "token-for-bob"
    assert len(httpx_mock.get_requests()) == 2


# ---------------------------------------------------------------------------
# IntrospectionCache
# ---------------------------------------------------------------------------


async def test_introspect_cache_miss_calls_credenza(httpx_mock, test_settings):
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)
    cache = IntrospectionCache(test_settings)
    result = await cache.get(_TOKEN)
    assert result.sub == _SUB
    assert len(httpx_mock.get_requests()) == 1


async def test_introspect_cache_hit_does_not_call_credenza(httpx_mock, test_settings):
    """A second get() for the same token must be served from cache."""
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)
    cache = IntrospectionCache(test_settings)
    first = await cache.get(_TOKEN)
    second = await cache.get(_TOKEN)
    assert first.sub == second.sub == _SUB
    assert len(httpx_mock.get_requests()) == 1


async def test_introspect_cache_expired_entry_re_introspects(httpx_mock, test_settings):
    """An entry whose wall-clock expiry has passed is re-fetched."""
    # Provide two identical responses so the second introspect can succeed.
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)

    cache = IntrospectionCache(test_settings)
    await cache.get(_TOKEN)

    # Manually expire the cache entry.
    cache._cache[_TOKEN].expires_at = time.time() - 1

    await cache.get(_TOKEN)
    assert len(httpx_mock.get_requests()) == 2


async def test_introspect_cache_respects_token_exp(httpx_mock, test_settings):
    """Cache entry expires at min(ttl, token.exp) -- token exp < ttl must win."""
    near_exp = int(time.time()) + 5  # expires in 5 s, well inside 60 s TTL
    payload = {**_ACTIVE_PAYLOAD, "exp": near_exp}
    httpx_mock.add_response(url=_introspect_url(test_settings), json=payload)

    cache = IntrospectionCache(test_settings)
    await cache.get(_TOKEN)

    entry = cache._cache[_TOKEN]
    # expires_at must be clamped to the token's exp, not ttl seconds from now.
    assert entry.expires_at <= float(near_exp) + 0.1


async def test_introspect_cache_invalidate_forces_re_introspect(httpx_mock, test_settings):
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)

    cache = IntrospectionCache(test_settings)
    await cache.get(_TOKEN)
    cache.invalidate(_TOKEN)
    await cache.get(_TOKEN)
    assert len(httpx_mock.get_requests()) == 2


async def test_introspect_cache_concurrent_calls_introspect_once(httpx_mock, test_settings):
    """Concurrent get() calls for the same token must only call Credenza once."""
    httpx_mock.add_response(url=_introspect_url(test_settings), json=_ACTIVE_PAYLOAD)
    cache = IntrospectionCache(test_settings)

    results = await asyncio.gather(
        cache.get(_TOKEN),
        cache.get(_TOKEN),
        cache.get(_TOKEN),
    )

    assert all(r.sub == _SUB for r in results)
    assert len(httpx_mock.get_requests()) == 1


async def test_introspect_cache_inactive_token_raises(httpx_mock, test_settings):
    """IntrospectionCache propagates TokenInactiveError from introspect()."""
    httpx_mock.add_response(url=_introspect_url(test_settings), json={"active": False})
    cache = IntrospectionCache(test_settings)
    with pytest.raises(TokenInactiveError):
        await cache.get(_TOKEN)
