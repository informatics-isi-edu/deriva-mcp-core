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
