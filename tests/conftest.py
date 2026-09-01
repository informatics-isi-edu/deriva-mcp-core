"""Shared pytest fixtures for deriva-mcp-core tests.

Test configuration:
    Unit tests (default): no live services required; Credenza endpoints are mocked
        via pytest-httpx.
    Integration tests (marker: 'integration'): require live Credenza and DERIVA;
        run with:  pytest -m integration
"""

from __future__ import annotations

import pytest

from deriva_mcp_core.config import Settings


@pytest.fixture(autouse=True)
def _reset_schema_cache():
    """Clear the module-level schema cache around every test.

    catalog._fetch_schema() now reads this cache before fetching, so stale
    entries left by one test would otherwise be served to unrelated tests
    that reuse the same placeholder hostname/catalog_id/user fixtures.
    """
    from deriva_mcp_core.tools.catalog import _schema_cache

    _schema_cache.clear()
    yield
    _schema_cache.clear()


@pytest.fixture
def test_settings() -> Settings:
    """Settings instance with safe test defaults (no live services required)."""
    return Settings(
        credenza_url="https://credenza.test.example.org",
        server_url="https://mcp.test.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        deriva_resource="urn:deriva:rest",
        client_id="test-client-id",
        client_secret="test-client-secret",
        token_cache_buffer_seconds=60,
    )
