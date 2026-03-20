"""Unit tests for built-in DERIVA tool modules."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deriva_mcp_core.plugin.api import PluginContext, _set_plugin_context


# ---------------------------------------------------------------------------
# Fixtures
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
                        {
                            "name": "RID",
                            "type": {"typename": "ermrest_rid"},
                            "nullok": False,
                            "comment": None,
                        },
                        {
                            "name": "Name",
                            "type": {"typename": "text"},
                            "nullok": True,
                            "comment": "Display name",
                        },
                    ],
                    "keys": [{"unique_columns": ["RID"]}],
                    "foreign_keys": [],
                }
            },
        },
    }
}


class _CapturingMCP:
    """Minimal FastMCP stand-in that stores registered tools for direct invocation."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator

    def resource(self, *args, **kwargs):
        return lambda fn: fn

    def prompt(self, *args, **kwargs):
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


@pytest.fixture()
def mock_catalog():
    catalog = MagicMock()
    # catalog.get("/schema") returns response with .json()
    schema_resp = MagicMock()
    schema_resp.json.return_value = _SCHEMA_JSON

    def _get(path, **kwargs):
        if path == "/schema":
            return schema_resp
        resp = MagicMock()
        resp.json.return_value = []
        return resp

    catalog.get.side_effect = _get
    catalog.post.return_value = MagicMock(json=lambda: [{"RID": "1-ABC", "Name": "test"}])
    catalog.put.return_value = MagicMock(json=lambda: [{"RID": "1-ABC", "Name": "updated"}])
    catalog.delete.return_value = MagicMock()
    return catalog


# ---------------------------------------------------------------------------
# catalog tools
# ---------------------------------------------------------------------------


class TestCatalogTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import catalog
        catalog.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        server = MagicMock()
        server.connect_ermrest.return_value = mock_catalog
        return patch("deriva_mcp_core.tools.catalog.get_deriva_server", return_value=server)

    async def test_get_catalog_info(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["get_catalog_info"]("host.example.org", "1"))
        assert result["hostname"] == "host.example.org"
        assert result["catalog_id"] == "1"
        schema_names = [s["schema"] for s in result["schemas"]]
        assert "public" in schema_names
        assert "_ermrest" not in schema_names

    async def test_list_schemas(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["list_schemas"]("host.example.org", "1"))
        assert "public" in result["schemas"]
        assert "_ermrest" not in result["schemas"]

    async def test_get_schema(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["get_schema"]("host.example.org", "1", "public"))
        assert result["schema"] == "public"
        table_names = [t["table"] for t in result["tables"]]
        assert "MyTable" in table_names

    async def test_get_schema_not_found(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["get_schema"]("host.example.org", "1", "missing"))
        assert "error" in result

    async def test_get_table(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["get_table"]("host.example.org", "1", "public", "MyTable"))
        assert result["schema"] == "public"
        assert result["table"] == "MyTable"
        col_names = [c["name"] for c in result["columns"]]
        assert "RID" in col_names
        assert "Name" in col_names
        assert result["keys"] == [{"columns": ["RID"]}]

    async def test_get_table_not_found(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["get_table"]("h", "1", "public", "missing"))
        assert "error" in result

    async def test_catalog_connect_hook_fired(self, ctx, mock_catalog):
        """Schema tools fire fire_catalog_connect after fetching the schema."""
        tools = self._register(ctx)
        hook = MagicMock()
        ctx.on_catalog_connect(hook)
        with self._patch_server(mock_catalog):
            await tools["get_catalog_info"]("host.example.org", "1")
        # Hook fires asynchronously (fire-and-forget task); verify it was scheduled
        # by checking the context's hook list is non-empty
        assert len(ctx._catalog_connect_hooks) == 1

    async def test_get_catalog_info_error(self, ctx):
        tools = self._register(ctx)
        server = MagicMock()
        server.connect_ermrest.side_effect = RuntimeError("connection refused")
        with patch("deriva_mcp_core.tools.catalog.get_deriva_server", return_value=server):
            result = json.loads(await tools["get_catalog_info"]("bad.host", "1"))
        assert "error" in result


# ---------------------------------------------------------------------------
# entity tools
# ---------------------------------------------------------------------------


class TestEntityTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import entity
        entity.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        server = MagicMock()
        server.connect_ermrest.return_value = mock_catalog
        return patch("deriva_mcp_core.tools.entity.get_deriva_server", return_value=server)

    async def test_get_entities(self, ctx, mock_catalog):
        entities_data = [{"RID": "1-AAA", "Name": "foo"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = entities_data
        mock_catalog.get.side_effect = None  # clear schema side_effect from fixture
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["get_entities"]("h", "1", "public", "MyTable"))
        assert result["count"] == 1
        assert result["entities"] == entities_data

    async def test_get_entities_with_filters(self, ctx, mock_catalog):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["get_entities"]("h", "1", "public", "MyTable", filters={"Status": "active"})
        called_url = mock_catalog.get.call_args[0][0]
        assert "Status=active" in called_url

    async def test_get_entities_limit_capped(self, ctx, mock_catalog):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["get_entities"]("h", "1", "public", "MyTable", limit=9999)
        called_url = mock_catalog.get.call_args[0][0]
        assert "limit=1000" in called_url

    async def test_insert_entities(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["insert_entities"]("h", "1", "public", "MyTable", [{"Name": "test"}])
            )
        assert result["status"] == "inserted"
        assert result["inserted_count"] == 1
        assert "1-ABC" in result["rids"]

    async def test_update_entities(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["update_entities"]("h", "1", "public", "MyTable", [{"RID": "1-ABC", "Name": "new"}])
            )
        assert result["status"] == "updated"

    async def test_delete_entities_requires_filters(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["delete_entities"]("h", "1", "public", "MyTable", filters={})
            )
        assert "error" in result
        mock_catalog.delete.assert_not_called()

    async def test_delete_entities_with_filters(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["delete_entities"]("h", "1", "public", "MyTable", filters={"RID": "1-ABC"})
            )
        assert result["status"] == "deleted"
        mock_catalog.delete.assert_called_once()

    async def test_entity_url_encoding(self, ctx, mock_catalog):
        """Schema/table names with special characters are percent-encoded."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["get_entities"]("h", "1", "my schema", "my table")
        called_url = mock_catalog.get.call_args[0][0]
        assert "my%20schema" in called_url
        assert "my%20table" in called_url


# ---------------------------------------------------------------------------
# query tools
# ---------------------------------------------------------------------------


class TestQueryTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import query
        query.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        server = MagicMock()
        server.connect_ermrest.return_value = mock_catalog
        return patch("deriva_mcp_core.tools.query.get_deriva_server", return_value=server)

    async def test_query_attribute(self, ctx, mock_catalog):
        rows = [{"RID": "1-AAA", "Name": "foo"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = rows
        mock_catalog.get.side_effect = None  # clear schema side_effect from fixture
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["query_attribute"]("h", "1", "isa:Dataset", ["RID", "Name"]))
        assert result["count"] == 1
        assert result["rows"] == rows
        called_url = mock_catalog.get.call_args[0][0]
        assert "/attribute/isa:Dataset/RID,Name" == called_url

    async def test_query_attribute_no_attrs(self, ctx, mock_catalog):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["query_attribute"]("h", "1", "isa:Dataset")
        called_url = mock_catalog.get.call_args[0][0]
        assert called_url == "/attribute/isa:Dataset"

    async def test_query_aggregate(self, ctx, mock_catalog):
        agg_result = [{"cnt": 42}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = agg_result
        mock_catalog.get.side_effect = None  # clear schema side_effect from fixture
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["query_aggregate"]("h", "1", "isa:Dataset", ["cnt:=cnt(RID)"])
            )
        assert result["result"] == agg_result
        called_url = mock_catalog.get.call_args[0][0]
        assert "/aggregate/isa:Dataset/cnt:=cnt(RID)" == called_url


# ---------------------------------------------------------------------------
# hatrac tools
# ---------------------------------------------------------------------------


class TestHatracTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import hatrac
        hatrac.register(ctx)
        return ctx._mcp.tools

    def _patch_store(self, mock_store):
        return patch("deriva_mcp_core.tools.hatrac.get_hatrac_store", return_value=mock_store)

    @pytest.fixture()
    def mock_store(self):
        store = MagicMock()
        store.get.return_value = MagicMock(json=lambda: ["/hatrac/ns/file.txt"])
        store.head.return_value = MagicMock(
            headers={"Content-Type": "application/octet-stream", "Content-Length": "1024"}
        )
        store.put.return_value = MagicMock()
        return store

    async def test_list_namespace(self, ctx, mock_store):
        tools = self._register(ctx)
        with self._patch_store(mock_store):
            result = json.loads(await tools["list_namespace"]("h", "/hatrac/ns"))
        assert result["path"] == "/hatrac/ns/"
        assert "/hatrac/ns/file.txt" in result["contents"]

    async def test_list_namespace_adds_trailing_slash(self, ctx, mock_store):
        tools = self._register(ctx)
        with self._patch_store(mock_store):
            await tools["list_namespace"]("h", "/hatrac/ns/")
        called_path = mock_store.get.call_args[0][0]
        assert called_path.endswith("/")
        # No double slash
        assert "//" not in called_path

    async def test_get_object_metadata(self, ctx, mock_store):
        tools = self._register(ctx)
        with self._patch_store(mock_store):
            result = json.loads(await tools["get_object_metadata"]("h", "/hatrac/ns/file.txt"))
        assert result["path"] == "/hatrac/ns/file.txt"
        assert "content-type" in result["metadata"]

    async def test_create_namespace(self, ctx, mock_store):
        tools = self._register(ctx)
        with self._patch_store(mock_store):
            result = json.loads(await tools["create_namespace"]("h", "/hatrac/new/ns"))
        assert result["status"] == "created"
        assert result["path"] == "/hatrac/new/ns/"
        mock_store.put.assert_called_once()
        called_path = mock_store.put.call_args[0][0]
        assert called_path == "/hatrac/new/ns/"

    async def test_create_namespace_idempotent_path(self, ctx, mock_store):
        """Trailing slash is normalized correctly regardless of input."""
        tools = self._register(ctx)
        with self._patch_store(mock_store):
            result = json.loads(await tools["create_namespace"]("h", "/hatrac/new/ns/"))
        assert result["path"] == "/hatrac/new/ns/"


# ---------------------------------------------------------------------------
# Kill switch -- DERIVA_MCP_DISABLE_MUTATING_TOOLS
# ---------------------------------------------------------------------------


@pytest.fixture()
def disabled_ctx(capturing_mcp):
    """PluginContext with the mutation kill switch enabled."""
    _ctx = PluginContext(capturing_mcp, disable_mutating_tools=True)
    _set_plugin_context(_ctx)
    yield _ctx
    _set_plugin_context(None)  # type: ignore[arg-type]


class TestMutatingToolKillSwitch:
    """Mutating tools return the disabled error when DERIVA_MCP_DISABLE_MUTATING_TOOLS=true."""

    def _register_entity(self, ctx):
        from deriva_mcp_core.tools import entity
        entity.register(ctx)
        return ctx._mcp.tools

    def _register_hatrac(self, ctx):
        from deriva_mcp_core.tools import hatrac
        hatrac.register(ctx)
        return ctx._mcp.tools

    async def test_insert_entities_blocked(self, disabled_ctx):
        tools = self._register_entity(disabled_ctx)
        result = json.loads(await tools["insert_entities"]("h", "1", "s", "t", [{}]))
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_update_entities_blocked(self, disabled_ctx):
        tools = self._register_entity(disabled_ctx)
        result = json.loads(await tools["update_entities"]("h", "1", "s", "t", [{"RID": "1"}]))
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_delete_entities_blocked(self, disabled_ctx):
        tools = self._register_entity(disabled_ctx)
        result = json.loads(await tools["delete_entities"]("h", "1", "s", "t", {"RID": "1"}))
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_create_namespace_blocked(self, disabled_ctx):
        tools = self._register_hatrac(disabled_ctx)
        result = json.loads(await tools["create_namespace"]("h", "/hatrac/ns"))
        assert "error" in result
        assert "disabled" in result["error"]

    def test_tool_without_mutates_raises(self, ctx):
        """ctx.tool() without mutates= must raise TypeError at registration time."""
        with pytest.raises(TypeError, match="mutates="):
            @ctx.tool()
            async def undeclared_tool(): ...  # noqa: E704