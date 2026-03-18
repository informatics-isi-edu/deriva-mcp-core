"""Unit tests for deriva_mcp_core.config."""

from __future__ import annotations

import pytest

from deriva_mcp_core.config import Settings


def test_defaults():
    """Optional fields have correct defaults; required fields default to empty string."""
    s = Settings()
    assert s.token_cache_buffer_seconds == 60
    assert s.credenza_url == ""
    assert s.server_resource == ""
    assert s.deriva_resource == ""
    assert s.client_id == ""
    assert s.client_secret == ""


def test_explicit_values():
    """Settings can be constructed with explicit values."""
    s = Settings(
        credenza_url="https://credenza.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        deriva_resource="urn:deriva:rest",
        client_id="my-client",
        client_secret="my-secret",
        token_cache_buffer_seconds=30,
    )
    assert s.credenza_url == "https://credenza.example.org"
    assert s.server_resource == "urn:deriva:rest:service:mcp"
    assert s.deriva_resource == "urn:deriva:rest"
    assert s.client_id == "my-client"
    assert s.client_secret == "my-secret"
    assert s.token_cache_buffer_seconds == 30


def test_validate_for_http_passes(test_settings: Settings):
    """validate_for_http() does not raise when all required fields are set."""
    test_settings.validate_for_http()  # should not raise


def test_validate_for_http_raises_on_missing_fields():
    """validate_for_http() raises ValueError listing all missing variables."""
    s = Settings()  # all required fields are empty
    with pytest.raises(ValueError) as exc_info:
        s.validate_for_http()
    msg = str(exc_info.value)
    assert "DERIVA_MCP_CREDENZA_URL" in msg
    assert "DERIVA_MCP_SERVER_RESOURCE" in msg
    assert "DERIVA_MCP_DERIVA_RESOURCE" in msg
    assert "DERIVA_MCP_CLIENT_ID" in msg
    assert "DERIVA_MCP_CLIENT_SECRET" in msg


def test_validate_for_http_raises_on_partial_missing():
    """validate_for_http() names only the fields that are actually missing."""
    s = Settings(
        credenza_url="https://credenza.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        # deriva_resource, client_id, client_secret left empty
    )
    with pytest.raises(ValueError) as exc_info:
        s.validate_for_http()
    msg = str(exc_info.value)
    assert "DERIVA_MCP_CREDENZA_URL" not in msg
    assert "DERIVA_MCP_SERVER_RESOURCE" not in msg
    assert "DERIVA_MCP_DERIVA_RESOURCE" in msg
    assert "DERIVA_MCP_CLIENT_ID" in msg
    assert "DERIVA_MCP_CLIENT_SECRET" in msg
