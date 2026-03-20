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
    get_deriva_server,
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
# get_deriva_server and get_hatrac_store
# ---------------------------------------------------------------------------


def test_get_deriva_server_raises_when_no_credential():
    """get_deriva_server() propagates RuntimeError when no credential is set."""

    def check():
        _current_credential.set(None)
        with pytest.raises(RuntimeError, match="No credential in current request context"):
            get_deriva_server("deriva.example.org")

    _run_isolated(check)


def test_get_hatrac_store_raises_when_no_credential():
    """get_hatrac_store() propagates RuntimeError when no credential is set."""

    def check():
        _current_credential.set(None)
        with pytest.raises(RuntimeError, match="No credential in current request context"):
            get_hatrac_store("deriva.example.org")

    _run_isolated(check)


def test_get_deriva_server_returns_object_with_credential():
    """get_deriva_server() returns a DerivaServer instance when credential is set."""
    from deriva.core import DerivaServer

    def check():
        set_current_credential(_BEARER_CRED)
        server = get_deriva_server("deriva.example.org")
        assert isinstance(server, DerivaServer)

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
