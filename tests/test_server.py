"""Unit tests for the server factory and /health endpoint."""

from __future__ import annotations

from unittest.mock import patch

from deriva_mcp_core.config import Settings
from deriva_mcp_core.plugin.api import _set_plugin_context
from deriva_mcp_core.server import create_server


def _test_settings(**overrides) -> Settings:
    return Settings(
        credenza_url="https://credenza.test.example.org",
        server_url="https://mcp.test.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        deriva_resource="urn:deriva:rest",
        client_id="test-client-id",
        client_secret="test-client-secret",
        **overrides,
    )


def teardown_function():
    """Reset the plugin context singleton after each test."""
    _set_plugin_context(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Server creation
# ---------------------------------------------------------------------------


def test_create_server_http_returns_fastmcp():
    from mcp.server.fastmcp import FastMCP

    mcp = create_server(transport="http", settings=_test_settings())
    assert isinstance(mcp, FastMCP)


def test_create_server_stdio_returns_fastmcp():
    from mcp.server.fastmcp import FastMCP

    with patch("deriva_mcp_core.server._get_credential_for_stdio", create=True):
        # Patch deriva.core.get_credential so we don't need ~/.deriva/credential.json
        with patch("deriva.core.get_credential", return_value=lambda h: {}):
            mcp = create_server(transport="stdio", settings=_test_settings())
    assert isinstance(mcp, FastMCP)


def test_create_server_http_validates_settings():
    """create_server() raises ValueError when required HTTP settings are missing."""
    import pytest

    with pytest.raises(ValueError, match="DERIVA_MCP_CREDENZA_URL"):
        create_server(transport="http", settings=Settings())


# ---------------------------------------------------------------------------
# /health endpoint
# ---------------------------------------------------------------------------


def test_health_route_registered():
    """The /health custom route is registered on the FastMCP instance."""
    mcp = create_server(transport="http", settings=_test_settings())
    paths = [r.path for r in mcp._custom_starlette_routes]
    assert "/health" in paths


async def test_health_returns_ok():
    """GET /health returns 200 with {"status": "ok"}."""
    from starlette.testclient import TestClient

    mcp = create_server(transport="http", settings=_test_settings())
    app = mcp.streamable_http_app()
    client = TestClient(app, raise_server_exceptions=True)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
