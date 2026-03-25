"""Unit tests for deriva_mcp_core.context.

Tests use contextvars.copy_context() to run each case in an isolated copy of
the current context so that credential state does not bleed between tests.
"""

from __future__ import annotations

import contextvars

import pytest

from deriva_mcp_core.context import (
    _current_credential,
    _current_user_id,
    _is_401,
    _set_token_cache,
    deriva_call,
    get_catalog,
    get_hatrac_store,
    get_request_credential,
    get_request_user_id,
    set_current_credential,
    set_current_user_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BEARER_CRED = {"bearer-token": "test-derived-token"}
_COOKIE_CRED = {"cookie": "webauthn=test-session-cookie"}


def _run_isolated(fn):
    """Run fn() in a fresh copy of the current context and return its result."""
    result = {}

    def wrapper():
        result["value"] = fn()

    ctx = contextvars.copy_context()
    ctx.run(wrapper)
    return result["value"]


# ---------------------------------------------------------------------------
# get_request_credential
# ---------------------------------------------------------------------------


def test_get_request_credential_raises_when_no_credential():
    """get_request_credential() raises RuntimeError outside a handler context."""

    def check():
        # Ensure contextvar is unset in this isolated context
        _current_credential.set(None)
        with pytest.raises(RuntimeError, match="No credential in current request context"):
            get_request_credential()

    _run_isolated(check)


def test_get_request_credential_returns_credential():
    """get_request_credential() returns the credential set by set_current_credential()."""

    def check():
        set_current_credential(_BEARER_CRED)
        assert get_request_credential() == _BEARER_CRED

    _run_isolated(check)


def test_get_request_credential_cookie_format():
    """get_request_credential() works with cookie-format credentials (stdio mode)."""

    def check():
        set_current_credential(_COOKIE_CRED)
        assert get_request_credential() == _COOKIE_CRED

    _run_isolated(check)


def test_set_current_credential_overwrites():
    """A second call to set_current_credential() replaces the previous credential."""

    def check():
        set_current_credential(_BEARER_CRED)
        set_current_credential(_COOKIE_CRED)
        assert get_request_credential() == _COOKIE_CRED

    _run_isolated(check)


# ---------------------------------------------------------------------------
# Context isolation between concurrent requests
# ---------------------------------------------------------------------------


def test_credential_is_context_local():
    """Credential set in one context does not bleed into a sibling context."""
    outer_result = {}

    def outer():
        # Set a credential in the outer context
        set_current_credential(_BEARER_CRED)

        inner_result = {}

        def inner():
            # inner starts with its own fresh credential (None)
            _current_credential.set(None)
            inner_result["error"] = None
            try:
                get_request_credential()
            except RuntimeError as exc:
                inner_result["error"] = str(exc)

        # Run inner in an isolated context -- should NOT see outer's credential
        ctx = contextvars.copy_context()
        ctx.run(inner)

        outer_result["cred"] = get_request_credential()
        outer_result["inner_error"] = inner_result["error"]

    ctx = contextvars.copy_context()
    ctx.run(outer)

    assert outer_result["cred"] == _BEARER_CRED
    assert outer_result["inner_error"] is not None


# ---------------------------------------------------------------------------
# get_catalog and get_hatrac_store
# ---------------------------------------------------------------------------


def test_get_catalog_raises_when_no_credential():
    """get_catalog() propagates RuntimeError when no credential is set."""

    def check():
        _current_credential.set(None)
        with pytest.raises(RuntimeError, match="No credential in current request context"):
            get_catalog("deriva.example.org", "1")

    _run_isolated(check)


def test_get_hatrac_store_raises_when_no_credential():
    """get_hatrac_store() propagates RuntimeError when no credential is set."""

    def check():
        _current_credential.set(None)
        with pytest.raises(RuntimeError, match="No credential in current request context"):
            get_hatrac_store("deriva.example.org")

    _run_isolated(check)


def test_get_hatrac_store_returns_object_with_credential():
    """get_hatrac_store() returns a HatracStore instance when credential is set."""
    from deriva.core import HatracStore

    def check():
        set_current_credential(_BEARER_CRED)
        store = get_hatrac_store("deriva.example.org")
        assert isinstance(store, HatracStore)

    _run_isolated(check)


# ---------------------------------------------------------------------------
# get_request_user_id / set_current_user_id
# ---------------------------------------------------------------------------


def test_get_request_user_id_default_is_stdio():
    """get_request_user_id() returns 'stdio' when no user id has been set."""

    def check():
        _current_user_id.set("stdio")  # reset to default in isolated ctx
        assert get_request_user_id() == "stdio"

    _run_isolated(check)


def test_set_current_user_id_roundtrip():
    """set_current_user_id() / get_request_user_id() roundtrip."""

    def check():
        set_current_user_id("user@example.org")
        assert get_request_user_id() == "user@example.org"

    _run_isolated(check)


def test_user_id_is_context_local():
    """User ID set in one context does not bleed into a sibling context."""
    outer_result = {}

    def outer():
        set_current_user_id("outer-user")

        inner_result = {}

        def inner():
            _current_user_id.set("stdio")  # reset in inner context
            inner_result["uid"] = get_request_user_id()

        ctx = contextvars.copy_context()
        ctx.run(inner)

        outer_result["uid"] = get_request_user_id()
        outer_result["inner_uid"] = inner_result["uid"]

    ctx = contextvars.copy_context()
    ctx.run(outer)

    assert outer_result["uid"] == "outer-user"
    assert outer_result["inner_uid"] == "stdio"


# ---------------------------------------------------------------------------
# _is_401
# ---------------------------------------------------------------------------


def test_is_401_true_for_401_response():
    exc = Exception("401 Unauthorized")
    response = type("R", (), {"status_code": 401})()
    exc.response = response
    assert _is_401(exc) is True


def test_is_401_false_for_403_response():
    exc = Exception("403 Forbidden")
    response = type("R", (), {"status_code": 403})()
    exc.response = response
    assert _is_401(exc) is False


def test_is_401_false_for_no_response():
    assert _is_401(ValueError("no response attr")) is False


def test_is_401_false_for_none_response():
    exc = Exception()
    exc.response = None
    assert _is_401(exc) is False


# ---------------------------------------------------------------------------
# deriva_call
# ---------------------------------------------------------------------------


def test_deriva_call_passthrough_on_success():
    """deriva_call() does not interfere with normal execution."""
    with deriva_call():
        result = 1 + 1
    assert result == 2


def test_deriva_call_reraises_non_401():
    """deriva_call() re-raises non-401 exceptions without eviction."""
    with pytest.raises(ValueError, match="something broke"):
        with deriva_call():
            raise ValueError("something broke")


def test_deriva_call_reraises_401_and_evicts():
    """deriva_call() evicts the cached token and re-raises on a 401."""
    evicted = []

    class _FakeCache:
        def invalidate(self, principal):
            evicted.append(principal)

    _principal = "https://idp.example.org/user-42"

    def run():
        _set_token_cache(_FakeCache())
        set_current_user_id(_principal)

        exc = Exception("401 Unauthorized")
        exc.response = type("R", (), {"status_code": 401})()

        with pytest.raises(Exception, match="401 Unauthorized"):
            with deriva_call():
                raise exc

    _run_isolated(run)

    assert evicted == [_principal]


def test_deriva_call_no_op_eviction_when_no_cache():
    """deriva_call() re-raises 401 gracefully when no cache is registered (stdio mode)."""
    def run():
        _set_token_cache(None)
        set_current_user_id("https://idp.example.org/user")

        exc = Exception("401 Unauthorized")
        exc.response = type("R", (), {"status_code": 401})()

        with pytest.raises(Exception, match="401 Unauthorized"):
            with deriva_call():
                raise exc

    _run_isolated(run)
