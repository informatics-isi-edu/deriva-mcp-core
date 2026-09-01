"""Unit tests for built-in MCP resources (tools/resources.py)."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deriva_mcp_core.plugin.api import PluginContext, _set_plugin_context

# ---------------------------------------------------------------------------
# Test schema fixture (same shape as ERMrest /schema response)
# ---------------------------------------------------------------------------

_SCHEMA_JSON = {
    "schemas": {
        "_ermrest": {"tables": {}, "comment": None},
        "public": {
            "comment": "Public schema",
            "tables": {
                "MyTable": {
                    "schema_name": "public",
                    "table_name": "MyTable",
                    "comment": "A test table",
                    "kind": "table",
                    "column_definitions": [
                        {"name": "RID", "type": {"typename": "ermrest_rid"}, "nullok": False, "comment": None},
                        {"name": "Name", "type": {"typename": "text"}, "nullok": True, "comment": "Display name"},
                    ],
                    "keys": [{"unique_columns": ["RID"]}],
                    "foreign_keys": [],
                }
            },
        },
    }
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _CapturingMCP:
    """FastMCP stand-in that captures registered resources by URI template."""

    def __init__(self) -> None:
        self.resources: dict[str, Any] = {}

    def tool(self, **kwargs):
        return lambda fn: fn

    def resource(self, uri, *args, **kwargs):
        def decorator(fn):
            self.resources[uri] = fn
            return fn
        return decorator

    def prompt(self, name=None, *args, **kwargs):
        return lambda fn: fn


@pytest.fixture()
def capturing_mcp():
    return _CapturingMCP()


@pytest.fixture()
def ctx(capturing_mcp):
    _ctx = PluginContext(capturing_mcp)
    _set_plugin_context(_ctx)
    yield _ctx
    _set_plugin_context(None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset in-process caches and RAG store between tests."""
    import deriva_mcp_core.rag.tools as _rag_tools
    from deriva_mcp_core.tools.catalog import _schema_cache
    _schema_cache.clear()
    _rag_tools._rag_store = None
    yield
    _schema_cache.clear()
    _rag_tools._rag_store = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(ctx):
    from deriva_mcp_core.tools.resources import register
    register(ctx)
    return ctx._mcp.resources


# ---------------------------------------------------------------------------
# Server status resource
# ---------------------------------------------------------------------------


async def test_server_status_returns_json(ctx):
    resources = _register(ctx)
    fn = resources["deriva://server/status"]
    result = json.loads(await fn())
    assert "version" in result
    assert "auth_mode" in result
    assert "rag_enabled" in result
    assert "mutating_tools_enabled" in result
    assert "allow_anonymous" in result
    assert "rag" in result  # rag config block (None when disabled)


async def test_server_status_rag_enabled_when_store_present(ctx):
    resources = _register(ctx)
    fn = resources["deriva://server/status"]
    mock_store = MagicMock()
    mock_rag_status = {"backend": "chromadb", "auto_update": True, "data_dir": "/tmp/rag"}
    with (
        patch("deriva_mcp_core.tools.resources.get_rag_store", return_value=mock_store),
        patch("deriva_mcp_core.tools.resources.get_rag_status", return_value=mock_rag_status),
    ):
        result = json.loads(await fn())
    assert result["rag_enabled"] is True
    assert result["rag"]["backend"] == "chromadb"
    assert result["rag"]["auto_update"] is True


async def test_server_status_rag_disabled_when_no_store(ctx):
    resources = _register(ctx)
    fn = resources["deriva://server/status"]
    with patch("deriva_mcp_core.tools.resources.get_rag_store", return_value=None):
        result = json.loads(await fn())
    assert result["rag_enabled"] is False


# ---------------------------------------------------------------------------
# Schema resource -- cache warm path
# ---------------------------------------------------------------------------


async def test_catalog_schema_returns_cached(ctx):
    from deriva_mcp_core.tools.catalog import _schema_cache
    _schema_cache[("host.example.org", "1", "stdio")] = (_SCHEMA_JSON, time.monotonic())

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/schema"]
    result = json.loads(await fn(hostname="host.example.org", catalog_id="1"))
    assert "schemas" in result
    assert "public" in result["schemas"]


async def test_catalog_schema_fetches_live_on_cache_miss(ctx):
    mock_catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = _SCHEMA_JSON
    mock_catalog.get.return_value = resp

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/schema"]

    with patch("deriva_mcp_core.tools.resources.get_catalog", return_value=mock_catalog):
        result = json.loads(await fn(hostname="host.example.org", catalog_id="1"))

    assert "schemas" in result
    mock_catalog.get.assert_called_once_with("/schema")


# ---------------------------------------------------------------------------
# Tables resource
# ---------------------------------------------------------------------------


async def test_catalog_tables_excludes_system_schemas(ctx):
    from deriva_mcp_core.tools.catalog import _schema_cache
    _schema_cache[("host.example.org", "1", "stdio")] = (_SCHEMA_JSON, time.monotonic())

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/tables"]
    result = json.loads(await fn(hostname="host.example.org", catalog_id="1"))

    schema_names = {row["schema"] for row in result["tables"]}
    assert "_ermrest" not in schema_names
    assert "public" in schema_names


async def test_catalog_tables_shape(ctx):
    from deriva_mcp_core.tools.catalog import _schema_cache
    _schema_cache[("host.example.org", "1", "stdio")] = (_SCHEMA_JSON, time.monotonic())

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/tables"]
    result = json.loads(await fn(hostname="host.example.org", catalog_id="1"))

    assert len(result["tables"]) == 1
    row = result["tables"][0]
    assert row["schema"] == "public"
    assert row["table"] == "MyTable"
    assert row["comment"] == "A test table"


# ---------------------------------------------------------------------------
# Table resource
# ---------------------------------------------------------------------------


async def test_catalog_table_returns_definition(ctx):
    from deriva_mcp_core.tools.catalog import _schema_cache
    _schema_cache[("host.example.org", "1", "stdio")] = (_SCHEMA_JSON, time.monotonic())

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/table/{schema}/{table}"]
    result = json.loads(await fn(
        hostname="host.example.org", catalog_id="1", schema="public", table="MyTable"
    ))

    assert result["table_name"] == "MyTable"
    col_names = [c["name"] for c in result["column_definitions"]]
    assert "RID" in col_names
    assert "Name" in col_names


async def test_catalog_table_unknown_schema_returns_error(ctx):
    from deriva_mcp_core.tools.catalog import _schema_cache
    _schema_cache[("host.example.org", "1", "stdio")] = (_SCHEMA_JSON, time.monotonic())

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/table/{schema}/{table}"]
    result = json.loads(await fn(
        hostname="host.example.org", catalog_id="1", schema="nosuchschema", table="MyTable"
    ))

    assert "error" in result
    assert "nosuchschema" in result["error"]


async def test_catalog_table_unknown_table_returns_error(ctx):
    from deriva_mcp_core.tools.catalog import _schema_cache
    _schema_cache[("host.example.org", "1", "stdio")] = (_SCHEMA_JSON, time.monotonic())

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/table/{schema}/{table}"]
    result = json.loads(await fn(
        hostname="host.example.org", catalog_id="1", schema="public", table="NoSuchTable"
    ))

    assert "error" in result
    assert "NoSuchTable" in result["error"]


# ---------------------------------------------------------------------------
# Cache population via _fetch_schema
# ---------------------------------------------------------------------------


def test_fetch_schema_populates_cache(ctx):
    """_fetch_schema() must update _schema_cache as a side effect."""
    from unittest.mock import patch
    from deriva_mcp_core.tools.catalog import _schema_cache, _fetch_schema

    mock_catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = _SCHEMA_JSON
    mock_catalog.get.return_value = resp

    with (
        patch("deriva_mcp_core.tools.catalog.get_catalog", return_value=mock_catalog),
        patch("deriva_mcp_core.tools.catalog.fire_catalog_connect"),
        patch("deriva_mcp_core.tools.catalog._connected_user_catalogs", set()),
    ):
        _fetch_schema("host.example.org", "1", "user@example.org")

    key = ("host.example.org", "1", "user@example.org")
    assert key in _schema_cache
    cached_schema, _fetched_at = _schema_cache[key]
    assert cached_schema is _SCHEMA_JSON


def test_fetch_schema_cache_hit_skips_refetch(ctx):
    """A second _fetch_schema() call within the TTL must not touch get_catalog()."""
    from unittest.mock import patch
    from deriva_mcp_core.tools.catalog import _fetch_schema

    mock_catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = _SCHEMA_JSON
    mock_catalog.get.return_value = resp

    with (
        patch("deriva_mcp_core.tools.catalog.get_catalog", return_value=mock_catalog) as mock_get_catalog,
        patch("deriva_mcp_core.tools.catalog.fire_catalog_connect"),
        patch("deriva_mcp_core.tools.catalog._connected_user_catalogs", set()),
    ):
        first = _fetch_schema("host.example.org", "1", "user@example.org")
        second = _fetch_schema("host.example.org", "1", "user@example.org")

    assert first is _SCHEMA_JSON
    assert second is _SCHEMA_JSON
    mock_get_catalog.assert_called_once()


def test_fetch_schema_cache_expired_refetches(ctx):
    """An entry older than schema_cache_ttl_seconds must be treated as a miss."""
    from unittest.mock import patch
    from deriva_mcp_core.tools.catalog import _schema_cache, _fetch_schema

    mock_catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = _SCHEMA_JSON
    mock_catalog.get.return_value = resp

    key = ("host.example.org", "1", "user@example.org")
    _schema_cache[key] = ({"schemas": {}}, time.monotonic() - 100000)

    with (
        patch("deriva_mcp_core.tools.catalog.get_catalog", return_value=mock_catalog) as mock_get_catalog,
        patch("deriva_mcp_core.tools.catalog.fire_catalog_connect"),
        patch("deriva_mcp_core.tools.catalog._connected_user_catalogs", set()),
    ):
        result = _fetch_schema(*key)

    mock_get_catalog.assert_called_once()
    assert result is _SCHEMA_JSON


def test_fetch_schema_respects_configured_ttl(ctx):
    """A non-default schema_cache_ttl_seconds must actually be read at call time,
    not a hardcoded constant -- proven by an entry that is fresh under the 900s
    default but stale under a configured 1s TTL."""
    from unittest.mock import patch
    from deriva_mcp_core.tools.catalog import _schema_cache, _fetch_schema

    mock_catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = _SCHEMA_JSON
    mock_catalog.get.return_value = resp

    key = ("host.example.org", "1", "user@example.org")
    _schema_cache[key] = ({"schemas": {}}, time.monotonic() - 5)

    with (
        patch("deriva_mcp_core.tools.catalog.get_catalog", return_value=mock_catalog) as mock_get_catalog,
        patch("deriva_mcp_core.tools.catalog.fire_catalog_connect"),
        patch("deriva_mcp_core.tools.catalog._connected_user_catalogs", set()),
        patch("deriva_mcp_core.tools.catalog._settings.schema_cache_ttl_seconds", 1),
    ):
        result = _fetch_schema(*key)

    mock_get_catalog.assert_called_once()
    assert result is _SCHEMA_JSON


def test_schema_change_hook_purges_all_users_for_catalog():
    """_handle_schema_change must clear every cached user's entry for that catalog
    but leave other catalogs/hosts untouched."""
    from deriva_mcp_core.tools.catalog import _schema_cache, _handle_schema_change

    _schema_cache[("host.example.org", "1", "alice")] = ({}, time.monotonic())
    _schema_cache[("host.example.org", "1", "bob")] = ({}, time.monotonic())
    _schema_cache[("host.example.org", "2", "alice")] = ({}, time.monotonic())
    _schema_cache[("other.example.org", "1", "alice")] = ({}, time.monotonic())

    _handle_schema_change("host.example.org", "1")

    assert ("host.example.org", "1", "alice") not in _schema_cache
    assert ("host.example.org", "1", "bob") not in _schema_cache
    assert ("host.example.org", "2", "alice") in _schema_cache
    assert ("other.example.org", "1", "alice") in _schema_cache


async def test_get_schema_resource_scoped_per_user(ctx):
    """The deriva:// schema resource must not serve one user's cached schema to another."""
    from deriva_mcp_core.tools.catalog import _schema_cache

    _schema_cache[("host.example.org", "1", "alice")] = (_SCHEMA_JSON, time.monotonic())

    mock_catalog = MagicMock()
    resp = MagicMock()
    resp.json.return_value = {"schemas": {}}
    mock_catalog.get.return_value = resp

    resources = _register(ctx)
    fn = resources["deriva://catalog/{hostname}/{catalog_id}/schema"]

    with (
        patch("deriva_mcp_core.tools.resources.resolve_user_identity", return_value="bob"),
        patch("deriva_mcp_core.tools.resources.get_catalog", return_value=mock_catalog),
    ):
        result = json.loads(await fn(hostname="host.example.org", catalog_id="1"))

    # bob must get a live fetch (empty schema), not alice's cached _SCHEMA_JSON
    assert result == {"schemas": {}}
    mock_catalog.get.assert_called_once_with("/schema")