"""Tests for AnonymousPermitMiddleware and allow_anonymous server mode."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from deriva_mcp_core.auth.anonymous import AnonymousPermitMiddleware, _extract_bearer_token
from deriva_mcp_core.auth.introspect_cache import IntrospectionCache
from deriva_mcp_core.auth.token_cache import DerivedTokenCache
from deriva_mcp_core.auth.verifier import CredenzaTokenVerifier
from deriva_mcp_core.config import Settings
from deriva_mcp_core.context import _current_credential, _current_user_id, _mutation_allowed
from deriva_mcp_core.plugin.api import _set_plugin_context
from deriva_mcp_core.server import build_http_app, create_server

_TOKEN = "mcp-bearer-token"
_SUB = "user@example.org"
_ISS = "https://credenza.example.org"
_DERIVED = "derived-token"
_EXP = int(time.time()) + 3600


def _anon_settings(**overrides) -> Settings:
    return Settings(allow_anonymous=True, **overrides)


def _mixed_settings(**overrides) -> Settings:
    return Settings(
        allow_anonymous=True,
        credenza_url="https://credenza.test.example.org",
        server_url="https://mcp.test.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        deriva_resource="urn:deriva:rest",
        client_id="test-client-id",
        client_secret="test-client-secret",
        **overrides,
    )


def teardown_function():
    _set_plugin_context(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _extract_bearer_token helper
# ---------------------------------------------------------------------------


def test_extract_bearer_token_present():
    scope = {"headers": [(b"authorization", b"Bearer mytoken123")]}
    assert _extract_bearer_token(scope) == "mytoken123"


def test_extract_bearer_token_case_insensitive():
    scope = {"headers": [(b"authorization", b"BEARER mytoken123")]}
    assert _extract_bearer_token(scope) == "mytoken123"


def test_extract_bearer_token_missing():
    scope = {"headers": [(b"content-type", b"application/json")]}
    assert _extract_bearer_token(scope) is None


def test_extract_bearer_token_empty_headers():
    assert _extract_bearer_token({"headers": []}) is None


# ---------------------------------------------------------------------------
# AnonymousPermitMiddleware: anonymous path (no Authorization header)
# ---------------------------------------------------------------------------


def _make_echo_app():
    """Minimal ASGI app that returns 200 OK."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    def homepage(request: Request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/", homepage)])


async def test_anonymous_request_sets_empty_credential():
    """No Authorization header sets credential={} in contextvar."""
    captured = {}

    async def inner(scope, receive, send):
        captured["credential"] = _current_credential.get()
        captured["user_id"] = _current_user_id.get()
        captured["mutation"] = _mutation_allowed.get()
        from starlette.responses import PlainTextResponse
        await PlainTextResponse("ok")(scope, receive, send)

    middleware = AnonymousPermitMiddleware(inner, verifier=None)
    client = TestClient(middleware)
    resp = client.get("/")
    assert resp.status_code == 200
    assert captured["credential"] == {}
    assert captured["user_id"] == "anonymous"
    assert captured["mutation"] is False


async def test_anonymous_request_non_http_scope_passes_through():
    """Non-HTTP scopes (e.g. lifespan) are forwarded without auth logic."""
    passed = []

    async def inner(scope, receive, send):
        passed.append(scope["type"])

    middleware = AnonymousPermitMiddleware(inner)
    await middleware({"type": "lifespan"}, None, None)
    assert passed == ["lifespan"]


# ---------------------------------------------------------------------------
# AnonymousPermitMiddleware: token present, no verifier (anonymous-only mode)
# ---------------------------------------------------------------------------


async def test_token_without_verifier_returns_401():
    """Bearer token provided but no verifier configured -> 401."""
    received = []

    async def inner(scope, receive, send):
        received.append("should_not_reach")  # pragma: no cover

    middleware = AnonymousPermitMiddleware(inner, verifier=None)
    client = TestClient(middleware)
    resp = client.get("/", headers={"Authorization": "Bearer sometoken"})
    assert resp.status_code == 401
    assert not received


# ---------------------------------------------------------------------------
# AnonymousPermitMiddleware: token present, verifier configured
# ---------------------------------------------------------------------------


async def test_valid_token_calls_verifier(httpx_mock, test_settings):
    """A valid bearer token is passed to the verifier, contextvars are set."""
    httpx_mock.add_response(
        url=f"{test_settings.credenza_url}/introspect",
        json={
            "active": True,
            "sub": _SUB,
            "iss": _ISS,
            "aud": [test_settings.server_resource],
            "exp": _EXP,
        },
    )
    httpx_mock.add_response(
        url=f"{test_settings.credenza_url}/token",
        json={"access_token": _DERIVED, "expires_in": 1800},
    )
    verifier = CredenzaTokenVerifier(
        test_settings, DerivedTokenCache(test_settings), IntrospectionCache(test_settings)
    )
    captured = {}

    async def inner(scope, receive, send):
        captured["credential"] = _current_credential.get()
        captured["user_id"] = _current_user_id.get()
        from starlette.responses import PlainTextResponse
        await PlainTextResponse("ok")(scope, receive, send)

    middleware = AnonymousPermitMiddleware(inner, verifier=verifier)
    client = TestClient(middleware)
    resp = client.get("/", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200
    assert captured["credential"] == {"bearer-token": _DERIVED}
    assert captured["user_id"] == f"{_ISS}/{_SUB}"


async def test_invalid_token_returns_401(httpx_mock, test_settings):
    """An invalid/expired bearer token results in 401 (not silently downgraded)."""
    httpx_mock.add_response(
        url=f"{test_settings.credenza_url}/introspect",
        json={"active": False},
    )
    verifier = CredenzaTokenVerifier(
        test_settings, DerivedTokenCache(test_settings), IntrospectionCache(test_settings)
    )
    middleware = AnonymousPermitMiddleware(_make_echo_app(), verifier=verifier)
    client = TestClient(middleware)
    resp = client.get("/", headers={"Authorization": "Bearer badtoken"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# create_server / build_http_app with allow_anonymous
# ---------------------------------------------------------------------------


def test_create_server_anonymous_only_does_not_require_credenza():
    """allow_anonymous=True with no credenza_url creates the server without error."""
    mcp = create_server(transport="http", settings=_anon_settings())
    assert hasattr(mcp, "_allow_anonymous_verifier")
    assert mcp._allow_anonymous_verifier is None


def test_create_server_mixed_mode_creates_verifier():
    """allow_anonymous=True with credenza_url creates a verifier."""
    mcp = create_server(transport="http", settings=_mixed_settings())
    assert hasattr(mcp, "_allow_anonymous_verifier")
    assert mcp._allow_anonymous_verifier is not None


def test_create_server_normal_mode_no_anonymous_attr():
    """Normal (non-anonymous) mode does not set _allow_anonymous_verifier."""
    settings = Settings(
        credenza_url="https://credenza.test.example.org",
        server_url="https://mcp.test.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        deriva_resource="urn:deriva:rest",
        client_id="test-client-id",
        client_secret="test-client-secret",
    )
    mcp = create_server(transport="http", settings=settings)
    assert not hasattr(mcp, "_allow_anonymous_verifier")


def test_build_http_app_anonymous_allows_unauthenticated():
    """build_http_app() in anonymous mode allows requests without a token."""
    mcp = create_server(transport="http", settings=_anon_settings())
    app = build_http_app(mcp)
    client = TestClient(app)
    # /health is available and requires no token in anonymous mode
    resp = client.get("/health")
    assert resp.status_code == 200


def test_build_http_app_normal_mode_rejects_unauthenticated():
    """build_http_app() in normal mode requires a bearer token."""
    settings = Settings(
        credenza_url="https://credenza.test.example.org",
        server_url="https://mcp.test.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        deriva_resource="urn:deriva:rest",
        client_id="test-client-id",
        client_secret="test-client-secret",
    )
    mcp = create_server(transport="http", settings=settings)
    app = build_http_app(mcp)
    client = TestClient(app, raise_server_exceptions=False)
    # MCP endpoint requires auth in normal mode; /health is a custom route that bypasses it
    resp = client.get("/health")
    assert resp.status_code == 200  # /health is unprotected regardless of mode


def test_anonymous_health_endpoint_accessible():
    """/health is reachable without a token in anonymous mode."""
    mcp = create_server(transport="http", settings=_anon_settings())
    app = build_http_app(mcp)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# validate_for_http with allow_anonymous
# ---------------------------------------------------------------------------


def test_validate_for_http_anonymous_only_passes_without_credenza():
    """allow_anonymous + no credenza_url: validate_for_http() passes."""
    s = Settings(allow_anonymous=True)
    s.validate_for_http()  # should not raise


def test_validate_for_http_anonymous_only_raises_when_partial_credenza():
    """allow_anonymous + partial credenza config: validate_for_http() still validates."""
    s = Settings(allow_anonymous=True, credenza_url="https://c.example.org")
    with pytest.raises(ValueError, match="DERIVA_MCP_SERVER_URL"):
        s.validate_for_http()


def test_validate_for_http_normal_mode_raises_without_credenza():
    """Normal mode: missing credenza_url raises as before."""
    with pytest.raises(ValueError, match="DERIVA_MCP_CREDENZA_URL"):
        Settings().validate_for_http()
