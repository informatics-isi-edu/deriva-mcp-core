"""Unit tests for deriva_mcp_core.config."""

from __future__ import annotations

import pytest

from deriva_mcp_core.config import Settings, find_config_file


def test_defaults():
    """Optional fields have correct defaults; required-for-HTTP fields default to empty."""
    s = Settings()
    assert s.token_cache_buffer_seconds == 60
    assert s.credenza_url == ""
    assert s.server_resource == ""
    assert s.deriva_resource == "urn:deriva:rest:service:all"
    assert s.client_id == "deriva-mcp"
    assert s.client_secret == ""
    assert s.disable_mutating_tools is True


# ---------------------------------------------------------------------------
# find_config_file
# ---------------------------------------------------------------------------


def test_find_config_file_returns_none_when_no_file_present(tmp_path, monkeypatch):
    """Returns None when no config file exists in any search location."""
    monkeypatch.chdir(tmp_path)
    # Patch home so ~/deriva-mcp.env does not exist either
    monkeypatch.setenv("HOME", str(tmp_path))
    result = find_config_file()
    assert result is None


def test_find_config_file_finds_cwd_file(tmp_path, monkeypatch):
    """Finds deriva-mcp.env in the current working directory."""
    config = tmp_path / "deriva-mcp.env"
    config.write_text("DERIVA_MCP_CLIENT_ID=from-cwd\n")
    monkeypatch.chdir(tmp_path)
    result = find_config_file()
    assert result is not None
    assert result == str(config.resolve())


def test_find_config_file_explicit_path(tmp_path):
    """Explicit path is returned as-is when the file exists."""
    config = tmp_path / "custom.env"
    config.write_text("DERIVA_MCP_CLIENT_ID=custom\n")
    result = find_config_file(explicit=str(config))
    assert result == str(config.resolve())


def test_find_config_file_explicit_missing_raises():
    """Raises FileNotFoundError when an explicit path does not exist."""
    with pytest.raises(FileNotFoundError):
        find_config_file(explicit="/nonexistent/path/deriva-mcp.env")


def test_settings_loads_values_from_env_file(tmp_path):
    """Settings picks up values from the env file returned by find_config_file."""
    config = tmp_path / "deriva-mcp.env"
    config.write_text("DERIVA_MCP_CLIENT_ID=loaded-from-file\n")
    s = Settings(_env_file=str(config))
    assert s.client_id == "loaded-from-file"


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
    """validate_for_http() raises ValueError listing all missing variables.

    Fields with non-empty defaults (deriva_resource, client_id) are not missing.
    """
    s = Settings()
    with pytest.raises(ValueError) as exc_info:
        s.validate_for_http()
    msg = str(exc_info.value)
    assert "DERIVA_MCP_CREDENZA_URL" in msg
    assert "DERIVA_MCP_SERVER_URL" in msg
    assert "DERIVA_MCP_SERVER_RESOURCE" in msg
    assert "DERIVA_MCP_CLIENT_SECRET" in msg
    # These have non-empty defaults and should not appear as missing
    assert "DERIVA_MCP_DERIVA_RESOURCE" not in msg
    assert "DERIVA_MCP_CLIENT_ID" not in msg


def test_validate_for_http_raises_on_partial_missing():
    """validate_for_http() names only the fields that are actually missing."""
    s = Settings(
        credenza_url="https://credenza.example.org",
        server_url="https://mcp.example.org",
        server_resource="urn:deriva:rest:service:mcp",
        # client_secret still empty
    )
    with pytest.raises(ValueError) as exc_info:
        s.validate_for_http()
    msg = str(exc_info.value)
    assert "DERIVA_MCP_CREDENZA_URL" not in msg
    assert "DERIVA_MCP_SERVER_URL" not in msg
    assert "DERIVA_MCP_SERVER_RESOURCE" not in msg
    assert "DERIVA_MCP_CLIENT_SECRET" in msg
