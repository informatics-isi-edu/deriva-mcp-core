"""Unit tests for CredenzaTokenVerifier."""

from __future__ import annotations

import logging
import time

from deriva_mcp_core.auth.introspect_cache import IntrospectionCache
from deriva_mcp_core.auth.token_cache import DerivedTokenCache
from deriva_mcp_core.auth.verifier import CredenzaTokenVerifier
from deriva_mcp_core.config import Settings
from deriva_mcp_core.context import _current_credential, _current_user_id

_TOKEN = "mcp-bearer-token"
_SUB = "user@example.org"
_ISS = "https://credenza.example.org"
_DERIVED = "derived-token"
_EXP = int(time.time()) + 3600


def _introspect_url(s: Settings) -> str:
    return f"{s.credenza_url}/introspect"


def _token_url(s: Settings) -> str:
    return f"{s.credenza_url}/token"


def _make_verifier(settings: Settings) -> CredenzaTokenVerifier:
    return CredenzaTokenVerifier(
        settings,
        DerivedTokenCache(settings),
        IntrospectionCache(settings),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_verify_token_success(httpx_mock, test_settings):
    httpx_mock.add_response(
        url=_introspect_url(test_settings),
        json={"active": True, "sub": _SUB, "iss": _ISS, "aud": [test_settings.server_resource], "exp": _EXP},
    )
    httpx_mock.add_response(
        url=_token_url(test_settings),
        json={"access_token": _DERIVED, "expires_in": 1800},
    )
    access_token = await _make_verifier(test_settings).verify_token(_TOKEN)
    assert access_token is not None
    assert access_token.token == _DERIVED
    assert access_token.client_id == f"{_ISS}/{_SUB}"
    assert access_token.expires_at == _EXP
    assert access_token.resource == test_settings.server_resource


async def test_verify_token_sets_credential(httpx_mock, test_settings):
    """verify_token() sets the per-request contextvar with the derived token."""
    httpx_mock.add_response(
        url=_introspect_url(test_settings),
        json={"active": True, "sub": _SUB, "iss": _ISS, "aud": [test_settings.server_resource], "exp": _EXP},
    )
    httpx_mock.add_response(
        url=_token_url(test_settings),
        json={"access_token": _DERIVED, "expires_in": 1800},
    )
    verifier = _make_verifier(test_settings)
    await verifier.verify_token(_TOKEN)
    assert _current_credential.get() == {"bearer-token": _DERIVED}


async def test_verify_token_sets_user_id(httpx_mock, test_settings):
    """verify_token() sets the per-request user_id contextvar with iss/sub from the MCP token."""
    httpx_mock.add_response(
        url=_introspect_url(test_settings),
        json={"active": True, "sub": _SUB, "iss": _ISS, "aud": [test_settings.server_resource], "exp": _EXP},
    )
    httpx_mock.add_response(
        url=_token_url(test_settings),
        json={"access_token": _DERIVED, "expires_in": 1800},
    )
    verifier = _make_verifier(test_settings)
    await verifier.verify_token(_TOKEN)
    # Principal must come from the MCP-token introspection, not the derived token.
    assert _current_user_id.get() == f"{_ISS}/{_SUB}"


async def test_verify_token_logs_principal(httpx_mock, test_settings, caplog):
    """verify_token() emits an INFO audit log with principal and display name."""
    httpx_mock.add_response(
        url=_introspect_url(test_settings),
        json={
            "active": True,
            "sub": _SUB,
            "iss": _ISS,
            "email": "user@example.org",
            "aud": [test_settings.server_resource],
            "exp": _EXP,
        },
    )
    httpx_mock.add_response(
        url=_token_url(test_settings),
        json={"access_token": _DERIVED, "expires_in": 1800},
    )
    with caplog.at_level(logging.INFO, logger="deriva_mcp_core.auth.verifier"):
        await _make_verifier(test_settings).verify_token(_TOKEN)
    assert any(f"{_ISS}/{_SUB}" in r.message for r in caplog.records)
    assert any("user@example.org" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Rejection cases -- all should return None (FastMCP issues 401)
# ---------------------------------------------------------------------------


async def test_verify_token_inactive_returns_none(httpx_mock, test_settings):
    httpx_mock.add_response(
        url=_introspect_url(test_settings),
        json={"active": False},
    )
    result = await _make_verifier(test_settings).verify_token(_TOKEN)
    assert result is None


async def test_verify_token_introspect_http_error_returns_none(httpx_mock, test_settings):
    httpx_mock.add_response(url=_introspect_url(test_settings), status_code=500)
    result = await _make_verifier(test_settings).verify_token(_TOKEN)
    assert result is None


async def test_verify_token_aud_mismatch_returns_none(httpx_mock, test_settings):
    httpx_mock.add_response(
        url=_introspect_url(test_settings),
        json={
            "active": True,
            "sub": _SUB,
            "aud": ["urn:some-other-resource"],
            "exp": _EXP,
        },
    )
    result = await _make_verifier(test_settings).verify_token(_TOKEN)
    assert result is None
    # Exchange should NOT have been called
    assert len(httpx_mock.get_requests()) == 1


async def test_verify_token_exchange_failure_returns_none(httpx_mock, test_settings):
    httpx_mock.add_response(
        url=_introspect_url(test_settings),
        json={"active": True, "sub": _SUB, "iss": _ISS, "aud": [test_settings.server_resource], "exp": _EXP},
    )
    httpx_mock.add_response(url=_token_url(test_settings), status_code=400)
    result = await _make_verifier(test_settings).verify_token(_TOKEN)
    assert result is None
