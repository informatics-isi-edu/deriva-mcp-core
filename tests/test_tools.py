"""Unit tests for built-in DERIVA tool modules."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
    # catalog.get("/schema") returns response with .json() -- used by catalog tools
    schema_resp = MagicMock()
    schema_resp.json.return_value = _SCHEMA_JSON

    entity_resp = MagicMock()
    entity_resp.json.return_value = [{"RID": "1-AAA", "Name": "foo"}]

    def _get(path, **kwargs):
        if path == "/schema":
            return schema_resp
        if path.startswith("/entity/"):
            return entity_resp
        resp = MagicMock()
        resp.json.return_value = []
        return resp

    catalog.get.side_effect = _get
    catalog._entity_resp = entity_resp  # expose for assertions in entity tests
    catalog.post.return_value = MagicMock(json=lambda: [{"RID": "1-ABC", "Name": "test"}])
    catalog.put.return_value = MagicMock(json=lambda: [{"RID": "1-ABC", "Name": "updated"}])
    catalog.delete.return_value = MagicMock()

    # Datapath API -- used by vocabulary, insert/update/delete entity tools via catalog.getPathBuilder()
    mock_path = MagicMock()
    mock_path.filter.return_value = mock_path  # chainable
    mock_path.entities.return_value.fetch.return_value = [{"RID": "1-AAA", "Name": "foo"}]
    mock_path.insert.return_value = [{"RID": "1-ABC", "Name": "test"}]
    mock_path.update.return_value = [{"RID": "1-ABC", "Name": "updated"}]
    mock_path.delete.return_value = None

    mock_pb = MagicMock()
    mock_pb.schemas.__getitem__.return_value.tables.__getitem__.return_value = mock_path
    catalog.getPathBuilder.return_value = mock_pb
    catalog._mock_path = mock_path  # expose for assertions in entity tests

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
        return patch("deriva_mcp_core.tools.catalog.get_catalog", return_value=mock_catalog)

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
            result = json.loads(
                await tools["get_table"]("host.example.org", "1", "public", "MyTable")
            )
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
        with patch("deriva_mcp_core.tools.catalog.get_catalog", side_effect=RuntimeError("connection refused")):
            result = json.loads(await tools["get_catalog_info"]("bad.host", "1"))
        assert "error" in result

    async def test_list_schemas_error(self, ctx):
        tools = self._register(ctx)
        with patch("deriva_mcp_core.tools.catalog.get_catalog", side_effect=RuntimeError("boom")):
            result = json.loads(await tools["list_schemas"]("bad.host", "1"))
        assert "error" in result

    async def test_get_schema_error(self, ctx):
        tools = self._register(ctx)
        with patch("deriva_mcp_core.tools.catalog.get_catalog", side_effect=RuntimeError("boom")):
            result = json.loads(await tools["get_schema"]("bad.host", "1", "public"))
        assert "error" in result

    async def test_get_table_schema_not_found(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_table"]("h", "1", "missing_schema", "MyTable")
            )
        assert "error" in result
        assert "Schema not found" in result["error"]

    async def test_get_table_error(self, ctx):
        tools = self._register(ctx)
        with patch("deriva_mcp_core.tools.catalog.get_catalog", side_effect=RuntimeError("boom")):
            result = json.loads(await tools["get_table"]("bad.host", "1", "public", "T"))
        assert "error" in result

    async def test_get_table_with_foreign_keys(self, ctx, mock_catalog):
        """get_table returns FK summaries when the schema has foreign keys."""
        fk_schema = {
            "schemas": {
                "public": {
                    "comment": None,
                    "tables": {
                        "Dataset": {
                            "comment": "Dataset table",
                            "kind": "table",
                            "column_definitions": [
                                {"name": "RID", "type": {"typename": "ermrest_rid"}, "nullok": False}
                            ],
                            "keys": [],
                            "foreign_keys": [
                                {
                                    "foreign_key_columns": [{"column_name": "DatasetType"}],
                                    "referenced_columns": [
                                        {"schema_name": "vocab", "table_name": "DatasetType", "column_name": "RID"}
                                    ],
                                }
                            ],
                        }
                    },
                }
            }
        }
        fk_resp = MagicMock()
        fk_resp.json.return_value = fk_schema
        fk_catalog = MagicMock()
        fk_catalog.get.return_value = fk_resp
        tools = self._register(ctx)
        with patch("deriva_mcp_core.tools.catalog.get_catalog", return_value=fk_catalog):
            result = json.loads(await tools["get_table"]("h", "1", "public", "Dataset"))
        assert result["table"] == "Dataset"
        assert len(result["foreign_keys"]) == 1
        assert result["foreign_keys"][0]["columns"] == ["DatasetType"]
        assert result["foreign_keys"][0]["references"] == "vocab:DatasetType"


# ---------------------------------------------------------------------------
# entity tools
# ---------------------------------------------------------------------------


class TestEntityTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import entity

        entity.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        return patch("deriva_mcp_core.tools.entity.get_catalog", return_value=mock_catalog)

    async def test_get_entities(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(await tools["get_entities"]("h", "1", "public", "MyTable"))
        assert result["count"] == 1
        assert result["entities"] == [{"RID": "1-AAA", "Name": "foo"}]
        url = mock_catalog.get.call_args[0][0]
        assert "public:MyTable" in url
        assert "@sort(RID)" in url

    async def test_get_entities_with_filters(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["get_entities"]("h", "1", "public", "MyTable", filters={"Status": "active"})
        url = mock_catalog.get.call_args[0][0]
        assert "/Status=active" in url
        assert "@sort(RID)" in url

    async def test_get_entities_limit_capped(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["get_entities"]("h", "1", "public", "MyTable", limit=9999)
        url = mock_catalog.get.call_args[0][0]
        assert "?limit=1000" in url

    async def test_get_entities_after_rid(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["get_entities"]("h", "1", "public", "MyTable", limit=10, after_rid="Q-Y4CM")
        url = mock_catalog.get.call_args[0][0]
        assert "@sort(RID)@after(Q-Y4CM)" in url
        assert "?limit=10" in url

    async def test_get_entities_preflight_count(self, ctx, mock_catalog):
        mock_catalog.get.side_effect = None
        mock_catalog.get.return_value.json.return_value = [{"cnt": 42}]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_entities"]("h", "1", "public", "MyTable", preflight_count=True)
            )
        assert result["total_count"] == 42
        assert result["entities_fetched"] is False
        assert "action_required" in result

    async def test_insert_entities(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["insert_entities"]("h", "1", "public", "MyTable", [{"Name": "test"}])
            )
        assert result["status"] == "inserted"
        assert result["inserted_count"] == 1
        assert "1-ABC" in result["rids"]
        mock_catalog._mock_path.insert.assert_called_once_with([{"Name": "test"}])

    async def test_update_entities(self, ctx, mock_catalog):
        """update_entities uses EntitySet.update() (PUT /attributegroup) for sparse updates."""
        tools = self._register(ctx)
        updates = [{"RID": "1-ABC", "Name": "new"}]
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["update_entities"]("h", "1", "public", "MyTable", updates)
            )
        assert result["status"] == "updated"
        # Must call path.update(), not path.put() or catalog.put()
        mock_catalog._mock_path.update.assert_called_once_with(updates)

    async def test_delete_entities_requires_filters(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["delete_entities"]("h", "1", "public", "MyTable", filters={})
            )
        assert "error" in result
        mock_catalog._mock_path.delete.assert_not_called()

    async def test_delete_entities_with_filters(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["delete_entities"](
                    "h", "1", "public", "MyTable", filters={"RID": "1-ABC"}
                )
            )
        assert result["status"] == "deleted"
        mock_catalog._mock_path.filter.assert_called_once()
        mock_catalog._mock_path.delete.assert_called_once()

    async def test_get_entities_not_found_returns_hint(self, ctx, mock_catalog):
        """Not-found HTTP error surfaces RAG suggestions when the store has results."""
        mock_catalog.get.side_effect = RuntimeError("404 Not Found: table does not exist")
        suggestions = [{"name": "public:Dataset", "description": "Dataset table", "relevance": 0.9}]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.entity._rag_suggestions", new=AsyncMock(return_value=suggestions)
        ):
            result = json.loads(await tools["get_entities"]("h", "1", "public", "Typo"))
        assert "error" in result
        assert "hint" in result
        assert "suggestions" in result
        assert result["suggestions"] == suggestions

    async def test_get_entities_not_found_no_rag_no_hint(self, ctx, mock_catalog):
        """Not-found HTTP error with no RAG results returns error only -- no hint field."""
        mock_catalog.get.side_effect = RuntimeError("404 Not Found: table does not exist")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.entity._rag_suggestions", new=AsyncMock(return_value=[])
        ):
            result = json.loads(await tools["get_entities"]("h", "1", "public", "Typo"))
        assert "error" in result
        assert "hint" not in result
        assert "suggestions" not in result

    async def test_get_entities_does_not_exist_triggers_hint(self, ctx, mock_catalog):
        """'does not exist' HTTP error from ERMrest triggers RAG hint."""
        mock_catalog.get.side_effect = RuntimeError("relation 'sample' does not exist")
        suggestions = [{"name": "Data:Specimen", "description": "Specimen table", "relevance": 0.8}]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.entity._rag_suggestions", new=AsyncMock(return_value=suggestions)
        ):
            result = json.loads(await tools["get_entities"]("h", "1", "Data", "sample"))
        assert "error" in result
        assert "hint" in result
        assert result["suggestions"] == suggestions

    async def test_insert_entities_not_found_returns_hint(self, ctx, mock_catalog):
        mock_catalog._mock_path.insert.side_effect = RuntimeError("table not found in schema")
        suggestions = [{"name": "public:Dataset", "description": "Dataset table", "relevance": 0.8}]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.entity._rag_suggestions", new=AsyncMock(return_value=suggestions)
        ):
            result = json.loads(
                await tools["insert_entities"]("h", "1", "public", "Typo", [{"Name": "x"}])
            )
        assert "error" in result
        assert "hint" in result

    async def test_insert_entities_failure_emits_audit(self, ctx, mock_catalog):
        mock_catalog._mock_path.insert.side_effect = RuntimeError("DB error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.entity.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["insert_entities"]("h", "1", "s", "t", [{"Name": "x"}])
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "entity_insert_failed"

    async def test_update_entities_failure_emits_audit(self, ctx, mock_catalog):
        mock_catalog._mock_path.update.side_effect = RuntimeError("DB error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.entity.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["update_entities"]("h", "1", "s", "t", [{"RID": "1-ABC"}])
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "entity_update_failed"

    async def test_delete_entities_failure_emits_audit(self, ctx, mock_catalog):
        mock_catalog._mock_path.delete.side_effect = RuntimeError("DB error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.entity.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["delete_entities"]("h", "1", "s", "t", filters={"RID": "1-ABC"})
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "entity_delete_failed"


# ---------------------------------------------------------------------------
# query tools
# ---------------------------------------------------------------------------


class TestQueryTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import query

        query.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        return patch("deriva_mcp_core.tools.query.get_catalog", return_value=mock_catalog)

    async def test_query_attribute(self, ctx, mock_catalog):
        rows = [{"RID": "1-AAA", "Name": "foo"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = rows
        mock_catalog.get.side_effect = None  # clear schema side_effect from fixture
        mock_catalog.get.return_value = mock_resp
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["query_attribute"]("h", "1", "isa:Dataset", ["RID", "Name"])
            )
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
        assert called_url == "/attribute/isa:Dataset/*"

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


# ---------------------------------------------------------------------------
# vocabulary tools
# ---------------------------------------------------------------------------


class TestVocabularyTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import vocabulary

        vocabulary.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        return patch("deriva_mcp_core.tools.vocabulary.get_catalog", return_value=mock_catalog)

    async def test_list_vocabulary_terms(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["list_vocabulary_terms"]("h", "1", "vocab", "Tissue")
            )
        assert result["schema"] == "vocab"
        assert result["table"] == "Tissue"
        assert result["count"] == 1
        assert result["terms"] == [{"RID": "1-AAA", "Name": "foo"}]

    async def test_lookup_term_by_name(self, ctx, mock_catalog):
        # Use a separate mock for the filter chain so it does not share
        # entities().fetch() with the unfiltered path.
        filter_path = MagicMock()
        filter_path.entities.return_value.fetch.return_value = [
            {"RID": "1-AAA", "Name": "Brain", "Synonyms": []}
        ]
        mock_catalog._mock_path.filter.return_value = filter_path
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["lookup_term"]("h", "1", "vocab", "Tissue", "Brain")
            )
        assert result["term"]["Name"] == "Brain"

    async def test_lookup_term_by_synonym(self, ctx, mock_catalog):
        """Falls back to client-side synonym search when Name filter returns nothing."""
        filter_path = MagicMock()
        filter_path.entities.return_value.fetch.return_value = []
        mock_catalog._mock_path.filter.return_value = filter_path
        mock_catalog._mock_path.entities.return_value.fetch.return_value = [
            {"RID": "1-AAA", "Name": "Brain", "Synonyms": ["Cerebrum", "Neural tissue"]}
        ]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["lookup_term"]("h", "1", "vocab", "Tissue", "Cerebrum")
            )
        assert result["term"]["Name"] == "Brain"

    async def test_lookup_term_not_found(self, ctx, mock_catalog):
        filter_path = MagicMock()
        filter_path.entities.return_value.fetch.return_value = []
        mock_catalog._mock_path.filter.return_value = filter_path
        mock_catalog._mock_path.entities.return_value.fetch.return_value = [
            {"RID": "1-AAA", "Name": "Brain", "Synonyms": []}
        ]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["lookup_term"]("h", "1", "vocab", "Tissue", "Kidney")
            )
        assert "error" in result

    async def test_add_term(self, ctx, mock_catalog):
        mock_catalog._mock_path.insert.return_value = [
            {"RID": "1-NEW", "Name": "Kidney", "ID": "kidney", "URI": "/vocab/kidney"}
        ]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_term"]("h", "1", "vocab", "Tissue", "Kidney", "A kidney")
            )
        assert result["status"] == "created"
        assert result["term"]["Name"] == "Kidney"
        mock_catalog._mock_path.insert.assert_called_once()
        call_args = mock_catalog._mock_path.insert.call_args
        inserted_row = call_args[0][0][0]
        assert inserted_row["Name"] == "Kidney"
        assert inserted_row["Description"] == "A kidney"
        # defaults set must include ID and URI
        defaults = call_args[1]["defaults"]
        assert "ID" in defaults
        assert "URI" in defaults

    async def test_add_term_with_synonyms(self, ctx, mock_catalog):
        mock_catalog._mock_path.insert.return_value = [{"RID": "1-NEW", "Name": "Kidney"}]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["add_term"](
                "h", "1", "vocab", "Tissue", "Kidney", "A kidney",
                synonyms=["Renal tissue", "Nephros"],
            )
        inserted_row = mock_catalog._mock_path.insert.call_args[0][0][0]
        assert inserted_row["Synonyms"] == ["Renal tissue", "Nephros"]

    async def test_update_term_description(self, ctx, mock_catalog):
        filter_path = MagicMock()
        filter_path.entities.return_value.fetch.return_value = [{"RID": "1-AAA", "Name": "Brain"}]
        mock_catalog._mock_path.filter.return_value = filter_path
        mock_catalog._mock_path.update.return_value = [{"RID": "1-AAA", "Name": "Brain"}]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["update_term"](
                    "h", "1", "vocab", "Tissue", "Brain", description="Updated desc"
                )
            )
        assert result["status"] == "updated"
        update_row = mock_catalog._mock_path.update.call_args[0][0][0]
        assert update_row["RID"] == "1-AAA"
        assert update_row["Description"] == "Updated desc"
        assert "Synonyms" not in update_row

    async def test_update_term_requires_at_least_one_field(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["update_term"]("h", "1", "vocab", "Tissue", "Brain")
            )
        assert "error" in result

    async def test_update_term_not_found(self, ctx, mock_catalog):
        filter_path = MagicMock()
        filter_path.entities.return_value.fetch.return_value = []
        mock_catalog._mock_path.filter.return_value = filter_path
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["update_term"](
                    "h", "1", "vocab", "Tissue", "Unknown", description="x"
                )
            )
        assert "error" in result

    async def test_delete_term(self, ctx, mock_catalog):
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["delete_term"]("h", "1", "vocab", "Tissue", "Brain")
            )
        assert result["status"] == "deleted"
        assert result["name"] == "Brain"
        mock_catalog._mock_path.filter.return_value.delete.assert_called_once()

    async def test_add_term_blocked(self, disabled_ctx):
        from deriva_mcp_core.tools import vocabulary

        vocabulary.register(disabled_ctx)
        tools = disabled_ctx._mcp.tools
        result = json.loads(await tools["add_term"]("h", "1", "v", "T", "N", "D"))
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_delete_term_blocked(self, disabled_ctx):
        from deriva_mcp_core.tools import vocabulary

        vocabulary.register(disabled_ctx)
        tools = disabled_ctx._mcp.tools
        result = json.loads(await tools["delete_term"]("h", "1", "v", "T", "Brain"))
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_list_vocabulary_terms_error(self, ctx, mock_catalog):
        mock_catalog.getPathBuilder.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["list_vocabulary_terms"]("h", "1", "vocab", "Tissue")
            )
        assert "error" in result

    async def test_lookup_term_synonym_json_string(self, ctx, mock_catalog):
        """Synonyms stored as JSON string are parsed before matching."""
        filter_path = MagicMock()
        filter_path.entities.return_value.fetch.return_value = []
        mock_catalog._mock_path.filter.return_value = filter_path
        mock_catalog._mock_path.entities.return_value.fetch.return_value = [
            {"RID": "1-AAA", "Name": "Brain", "Synonyms": '["Cerebrum", "Neural tissue"]'}
        ]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["lookup_term"]("h", "1", "vocab", "Tissue", "Cerebrum")
            )
        assert result["term"]["Name"] == "Brain"

    async def test_lookup_term_error(self, ctx, mock_catalog):
        mock_catalog.getPathBuilder.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["lookup_term"]("h", "1", "vocab", "Tissue", "Brain")
            )
        assert "error" in result

    async def test_add_term_error(self, ctx, mock_catalog):
        mock_catalog._mock_path.insert.side_effect = RuntimeError("conflict")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.vocabulary.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["add_term"]("h", "1", "vocab", "Tissue", "Kidney", "A kidney")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "vocabulary_add_term_failed"

    async def test_update_term_with_synonyms(self, ctx, mock_catalog):
        """update_term with synonyms= updates the Synonyms field."""
        filter_path = MagicMock()
        filter_path.entities.return_value.fetch.return_value = [{"RID": "1-AAA", "Name": "Brain"}]
        mock_catalog._mock_path.filter.return_value = filter_path
        mock_catalog._mock_path.update.return_value = [{"RID": "1-AAA", "Name": "Brain"}]
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["update_term"](
                    "h", "1", "vocab", "Tissue", "Brain",
                    synonyms=["Cerebrum", "Neural tissue"],
                )
            )
        assert result["status"] == "updated"
        update_row = mock_catalog._mock_path.update.call_args[0][0][0]
        assert update_row["Synonyms"] == ["Cerebrum", "Neural tissue"]

    async def test_update_term_error(self, ctx, mock_catalog):
        mock_catalog.getPathBuilder.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.vocabulary.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["update_term"](
                    "h", "1", "vocab", "Tissue", "Brain", description="x"
                )
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "vocabulary_update_term_failed"

    async def test_delete_term_error(self, ctx, mock_catalog):
        mock_catalog.getPathBuilder.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.vocabulary.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["delete_term"]("h", "1", "vocab", "Tissue", "Brain")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "vocabulary_delete_term_failed"


# ---------------------------------------------------------------------------
# annotation tools
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_model():
    """Mock for catalog.getCatalogModel() -- used by annotation and schema (DDL) tools."""
    model = MagicMock()
    mock_col = MagicMock()
    mock_col.annotations = {}
    mock_col.type = MagicMock()
    mock_col.type.typename = "text"
    mock_col.name = "Name"

    mock_table = MagicMock()
    mock_table.annotations = {}
    mock_table.foreign_keys = []
    mock_table.referenced_by = []
    mock_table.columns.__getitem__ = MagicMock(return_value=mock_col)
    mock_table.columns.__iter__ = MagicMock(return_value=iter([mock_col]))

    model.schemas.__getitem__.return_value.tables.__getitem__.return_value = mock_table
    return model, mock_table, mock_col


class TestAnnotationTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import annotation

        annotation.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        return patch("deriva_mcp_core.tools.annotation.get_catalog", return_value=mock_catalog)

    # -- read tools --

    async def test_get_table_annotations_empty(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_table_annotations"]("h", "1", "public", "MyTable")
            )
        assert result["schema"] == "public"
        assert result["table"] == "MyTable"
        assert result["display"] is None
        assert result["visible_columns"] is None

    async def test_get_table_annotations_with_data(self, ctx, mock_catalog, mock_model):
        _DISPLAY_TAG = "tag:isrd.isi.edu,2015:display"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_DISPLAY_TAG: {"name": "My Table"}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_table_annotations"]("h", "1", "public", "MyTable")
            )
        assert result["display"] == {"name": "My Table"}

    async def test_get_column_annotations(self, ctx, mock_catalog, mock_model):
        model, _, mock_col = mock_model
        mock_col.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_column_annotations"]("h", "1", "public", "MyTable", "Name")
            )
        assert result["column"] == "Name"
        assert result["display"] is None
        assert result["column_display"] is None

    async def test_list_foreign_keys_empty(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_table.foreign_keys = []
        mock_table.referenced_by = []
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["list_foreign_keys"]("h", "1", "public", "MyTable")
            )
        assert result["outbound"] == []
        assert result["inbound"] == []

    async def test_get_handlebars_template_variables(self, ctx, mock_catalog, mock_model):
        model, mock_table, mock_col = mock_model
        mock_col.name = "Title"
        mock_col.type.typename = "text"
        mock_table.columns.__iter__ = MagicMock(return_value=iter([mock_col]))
        mock_table.foreign_keys = []
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_handlebars_template_variables"]("h", "1", "public", "MyTable")
            )
        col_names = [c["name"] for c in result["columns"]]
        assert "Title" in col_names
        assert "{{{Title}}}" in [c["template"] for c in result["columns"]]
        assert "special_variables" in result

    # -- write tools --

    async def test_set_display_annotation_on_table(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_display_annotation"](
                    "h", "1", "public", "MyTable", {"name": "Display Name"}
                )
            )
        assert result["status"] == "applied"
        model.apply.assert_called_once()

    async def test_set_display_annotation_remove(self, ctx, mock_catalog, mock_model):
        _DISPLAY_TAG = "tag:isrd.isi.edu,2015:display"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_DISPLAY_TAG: {"name": "old"}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_display_annotation"]("h", "1", "public", "MyTable", None)
            )
        assert result["status"] == "applied"
        assert _DISPLAY_TAG not in mock_table.annotations

    async def test_set_table_display_name(self, ctx, mock_catalog, mock_model):
        _DISPLAY_TAG = "tag:isrd.isi.edu,2015:display"
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_table_display_name"]("h", "1", "public", "MyTable", "My Table")
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_DISPLAY_TAG]["name"] == "My Table"
        model.apply.assert_called_once()

    async def test_set_table_display_name_preserves_other_props(
        self, ctx, mock_catalog, mock_model
    ):
        _DISPLAY_TAG = "tag:isrd.isi.edu,2015:display"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_DISPLAY_TAG: {"comment": "A tooltip", "name": "old"}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["set_table_display_name"]("h", "1", "public", "MyTable", "New Name")
        assert mock_table.annotations[_DISPLAY_TAG]["comment"] == "A tooltip"
        assert mock_table.annotations[_DISPLAY_TAG]["name"] == "New Name"

    async def test_set_visible_columns_full_replace(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        vc = {"*": ["RID", "Name"]}
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_visible_columns"]("h", "1", "public", "MyTable", vc)
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_VC_TAG] == vc

    async def test_add_visible_column(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VC_TAG: {"*": ["RID", "Name"]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_visible_column"]("h", "1", "public", "MyTable", "*", "Status")
            )
        assert result["status"] == "applied"
        assert "Status" in mock_table.annotations[_VC_TAG]["*"]

    async def test_add_visible_column_no_existing_annotation(self, ctx, mock_catalog, mock_model):
        """Creates the annotation if it does not exist yet."""
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_visible_column"]("h", "1", "public", "MyTable", "*", "Name")
            )
        assert result["status"] == "applied"
        assert "Name" in mock_table.annotations[_VC_TAG]["*"]

    async def test_remove_visible_column_by_value(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VC_TAG: {"*": ["RID", "Name", "Status"]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_column"]("h", "1", "public", "MyTable", "*", "Name")
            )
        assert result["status"] == "applied"
        assert "Name" not in mock_table.annotations[_VC_TAG]["*"]
        assert "RID" in mock_table.annotations[_VC_TAG]["*"]

    async def test_set_visible_foreign_keys(self, ctx, mock_catalog, mock_model):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        vfk = {"*": [["public", "Dataset_File_fkey"]]}
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_visible_foreign_keys"]("h", "1", "public", "MyTable", vfk)
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_VFK_TAG] == vfk

    async def test_set_row_name_pattern(self, ctx, mock_catalog, mock_model):
        _TD_TAG = "tag:isrd.isi.edu,2016:table-display"
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_row_name_pattern"](
                    "h", "1", "public", "MyTable", "{{{Name}}}"
                )
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_TD_TAG]["row_name"]["row_markdown_pattern"] == "{{{Name}}}"

    async def test_annotation_write_calls_fire_schema_change(
        self, ctx, mock_catalog, mock_model
    ):
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.fire_schema_change"
        ) as mock_fire:
            await tools["set_table_display_name"]("h", "1", "public", "MyTable", "X")
        mock_fire.assert_called_once_with("h", "1")

    async def test_set_display_annotation_blocked(self, disabled_ctx, mock_catalog, mock_model):
        model, _, _ = mock_model
        mock_catalog.getCatalogModel.return_value = model
        from deriva_mcp_core.tools import annotation

        annotation.register(disabled_ctx)
        tools = disabled_ctx._mcp.tools
        with patch("deriva_mcp_core.context.get_catalog", return_value=mock_catalog):
            result = json.loads(
                await tools["set_display_annotation"]("h", "1", "s", "t", {"name": "x"})
            )
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_set_display_annotation_failure_emits_audit(
        self, ctx, mock_catalog, mock_model
    ):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_display_annotation"]("h", "1", "s", "t", {"name": "x"})
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_display_failed"
        assert mock_audit.call_args[1]["error_type"] == "RuntimeError"

    async def test_set_visible_columns_failure_emits_audit(
        self, ctx, mock_catalog, mock_model
    ):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_visible_columns"]("h", "1", "s", "t", {"*": ["RID"]})
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_visible_columns_failed"

    async def test_add_visible_column_failure_emits_audit(
        self, ctx, mock_catalog, mock_model
    ):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["add_visible_column"]("h", "1", "s", "t", "*", "Name")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_add_visible_column_failed"

    # -- read tool error paths --

    async def test_get_table_annotations_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_table_annotations"]("h", "1", "public", "MyTable")
            )
        assert "error" in result

    async def test_get_column_annotations_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_column_annotations"]("h", "1", "public", "MyTable", "Name")
            )
        assert "error" in result

    async def test_list_foreign_keys_with_fks(self, ctx, mock_catalog, mock_model):
        """list_foreign_keys returns outbound and inbound FK data."""
        model, mock_table, _ = mock_model

        out_col = MagicMock()
        out_col.name = "DatasetType"
        ref_col = MagicMock()
        ref_col.name = "RID"
        out_fk = MagicMock()
        out_fk.constraint_schema.name = "public"
        out_fk.constraint_name = "Dataset_DatasetType_fkey"
        out_fk.columns = [out_col]
        out_fk.pk_table.schema.name = "vocab"
        out_fk.pk_table.name = "DatasetType"
        out_fk.referenced_columns = [ref_col]
        mock_table.foreign_keys = [out_fk]

        in_col = MagicMock()
        in_col.name = "Dataset"
        in_ref_col = MagicMock()
        in_ref_col.name = "RID"
        in_fk = MagicMock()
        in_fk.constraint_schema.name = "public"
        in_fk.constraint_name = "File_Dataset_fkey"
        in_fk.table.schema.name = "public"
        in_fk.table.name = "File"
        in_fk.columns = [in_col]
        in_fk.referenced_columns = [in_ref_col]
        mock_table.referenced_by = [in_fk]

        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["list_foreign_keys"]("h", "1", "public", "Dataset")
            )
        assert len(result["outbound"]) == 1
        assert result["outbound"][0]["constraint_name"] == ["public", "Dataset_DatasetType_fkey"]
        assert result["outbound"][0]["from_columns"] == ["DatasetType"]
        assert len(result["inbound"]) == 1
        assert result["inbound"][0]["from_table"] == "File"

    async def test_list_foreign_keys_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["list_foreign_keys"]("h", "1", "public", "MyTable")
            )
        assert "error" in result

    async def test_get_handlebars_with_foreign_keys(self, ctx, mock_catalog, mock_model):
        """get_handlebars_template_variables includes FK path variables."""
        model, mock_table, mock_col = mock_model
        mock_col.name = "Name"
        mock_col.type.typename = "text"
        mock_table.columns.__iter__ = MagicMock(return_value=iter([mock_col]))

        fk_col = MagicMock()
        fk_col.name = "DatasetType"
        pk_col = MagicMock()
        pk_col.name = "RID"
        fk = MagicMock()
        fk.constraint_schema.name = "public"
        fk.constraint_name = "Dataset_Type_fkey"
        fk.columns = [fk_col]
        fk.pk_table.name = "DatasetType"
        fk.pk_table.columns = [pk_col]
        mock_table.foreign_keys = [fk]

        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_handlebars_template_variables"]("h", "1", "public", "Dataset")
            )
        assert len(result["foreign_keys"]) == 1
        fk_var = result["foreign_keys"][0]
        assert fk_var["to_table"] == "DatasetType"
        assert "row_name_template" in fk_var

    async def test_get_handlebars_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("boom")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["get_handlebars_template_variables"]("h", "1", "public", "MyTable")
            )
        assert "error" in result

    # -- write tool column branch and error paths --

    async def test_set_display_annotation_on_column(self, ctx, mock_catalog, mock_model):
        _DISPLAY_TAG = "tag:isrd.isi.edu,2015:display"
        model, mock_table, mock_col = mock_model
        mock_col.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_display_annotation"](
                    "h", "1", "public", "MyTable", {"name": "My Col"}, column="Name"
                )
            )
        assert result["status"] == "applied"
        assert mock_col.annotations[_DISPLAY_TAG] == {"name": "My Col"}

    async def test_set_table_display_name_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_table_display_name"]("h", "1", "s", "t", "Name")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_table_display_name_failed"

    async def test_set_row_name_pattern_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_row_name_pattern"]("h", "1", "s", "t", "{{{Name}}}")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_row_name_pattern_failed"

    # -- set_column_display_name (untested tool) --

    async def test_set_column_display_name(self, ctx, mock_catalog, mock_model):
        _DISPLAY_TAG = "tag:isrd.isi.edu,2015:display"
        model, _, mock_col = mock_model
        mock_col.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_column_display_name"](
                    "h", "1", "public", "MyTable", "Name", "Display Name"
                )
            )
        assert result["status"] == "applied"
        assert mock_col.annotations[_DISPLAY_TAG]["name"] == "Display Name"

    async def test_set_column_display_name_preserves_other_props(
        self, ctx, mock_catalog, mock_model
    ):
        _DISPLAY_TAG = "tag:isrd.isi.edu,2015:display"
        model, _, mock_col = mock_model
        mock_col.annotations = {_DISPLAY_TAG: {"comment": "tooltip", "name": "old"}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            await tools["set_column_display_name"](
                "h", "1", "public", "MyTable", "Name", "New Name"
            )
        assert mock_col.annotations[_DISPLAY_TAG]["comment"] == "tooltip"
        assert mock_col.annotations[_DISPLAY_TAG]["name"] == "New Name"

    async def test_set_column_display_name_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_column_display_name"]("h", "1", "s", "t", "col", "Name")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_column_display_name_failed"

    # -- set_visible_columns null branch --

    async def test_set_visible_columns_remove(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VC_TAG: {"*": ["RID", "Name"]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_visible_columns"]("h", "1", "public", "MyTable", None)
            )
        assert result["status"] == "applied"
        assert _VC_TAG not in mock_table.annotations

    # -- add_visible_column position branch --

    async def test_add_visible_column_at_position(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VC_TAG: {"*": ["RID", "Name"]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_visible_column"](
                    "h", "1", "public", "MyTable", "*", "Status", position=1
                )
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_VC_TAG]["*"][1] == "Status"

    # -- remove_visible_column branches --

    async def test_remove_visible_column_no_annotation(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_column"]("h", "1", "public", "MyTable", "*", "Name")
            )
        assert "error" in result

    async def test_remove_visible_column_by_index(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VC_TAG: {"*": ["RID", "Name", "Status"]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_column"]("h", "1", "public", "MyTable", "*", 1)
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_VC_TAG]["*"] == ["RID", "Status"]

    async def test_remove_visible_column_index_out_of_range(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VC_TAG: {"*": ["RID"]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_column"]("h", "1", "public", "MyTable", "*", 5)
            )
        assert "error" in result

    async def test_remove_visible_column_not_found(self, ctx, mock_catalog, mock_model):
        _VC_TAG = "tag:isrd.isi.edu,2016:visible-columns"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VC_TAG: {"*": ["RID", "Name"]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_column"](
                    "h", "1", "public", "MyTable", "*", "Missing"
                )
            )
        assert "error" in result

    async def test_remove_visible_column_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["remove_visible_column"]("h", "1", "s", "t", "*", "Name")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_remove_visible_column_failed"

    # -- set_visible_foreign_keys null and error --

    async def test_set_visible_foreign_keys_remove(self, ctx, mock_catalog, mock_model):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VFK_TAG: {"*": [["public", "File_Dataset_fkey"]]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_visible_foreign_keys"]("h", "1", "public", "Dataset", None)
            )
        assert result["status"] == "applied"
        assert _VFK_TAG not in mock_table.annotations

    async def test_set_visible_foreign_keys_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_visible_foreign_keys"]("h", "1", "s", "t", {"*": []})
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_visible_foreign_keys_failed"

    # -- add_visible_foreign_key (untested tool) --

    async def test_add_visible_foreign_key(self, ctx, mock_catalog, mock_model):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VFK_TAG: {"*": [["public", "existing_fkey"]]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_visible_foreign_key"](
                    "h", "1", "public", "Dataset", "*", ["public", "new_fkey"]
                )
            )
        assert result["status"] == "applied"
        assert ["public", "new_fkey"] in mock_table.annotations[_VFK_TAG]["*"]

    async def test_add_visible_foreign_key_at_position(self, ctx, mock_catalog, mock_model):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VFK_TAG: {"*": [["public", "fk_a"], ["public", "fk_b"]]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_visible_foreign_key"](
                    "h", "1", "public", "Dataset", "*", ["public", "fk_new"], position=1
                )
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_VFK_TAG]["*"][1] == ["public", "fk_new"]

    async def test_add_visible_foreign_key_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["add_visible_foreign_key"](
                    "h", "1", "s", "t", "*", ["public", "fk"]
                )
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_add_visible_foreign_key_failed"

    # -- remove_visible_foreign_key (untested tool) --

    async def test_remove_visible_foreign_key_by_value(self, ctx, mock_catalog, mock_model):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {
            _VFK_TAG: {"*": [["public", "fk_a"], ["public", "fk_b"]]}
        }
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_foreign_key"](
                    "h", "1", "public", "Dataset", "*", ["public", "fk_a"]
                )
            )
        assert result["status"] == "applied"
        assert ["public", "fk_a"] not in mock_table.annotations[_VFK_TAG]["*"]
        assert ["public", "fk_b"] in mock_table.annotations[_VFK_TAG]["*"]

    async def test_remove_visible_foreign_key_by_index(self, ctx, mock_catalog, mock_model):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VFK_TAG: {"*": [["public", "fk_a"], ["public", "fk_b"]]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_foreign_key"](
                    "h", "1", "public", "Dataset", "*", 0
                )
            )
        assert result["status"] == "applied"
        assert len(mock_table.annotations[_VFK_TAG]["*"]) == 1

    async def test_remove_visible_foreign_key_no_annotation(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_foreign_key"](
                    "h", "1", "public", "Dataset", "*", ["public", "fk"]
                )
            )
        assert "error" in result

    async def test_remove_visible_foreign_key_index_out_of_range(
        self, ctx, mock_catalog, mock_model
    ):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VFK_TAG: {"*": [["public", "fk_a"]]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_foreign_key"](
                    "h", "1", "public", "Dataset", "*", 5
                )
            )
        assert "error" in result

    async def test_remove_visible_foreign_key_not_found(self, ctx, mock_catalog, mock_model):
        _VFK_TAG = "tag:isrd.isi.edu,2016:visible-foreign-keys"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_VFK_TAG: {"*": [["public", "fk_a"]]}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["remove_visible_foreign_key"](
                    "h", "1", "public", "Dataset", "*", ["public", "fk_missing"]
                )
            )
        assert "error" in result

    async def test_remove_visible_foreign_key_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["remove_visible_foreign_key"](
                    "h", "1", "s", "t", "*", ["public", "fk"]
                )
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_remove_visible_foreign_key_failed"

    # -- set_table_display (untested tool) --

    async def test_set_table_display(self, ctx, mock_catalog, mock_model):
        _TD_TAG = "tag:isrd.isi.edu,2016:table-display"
        model, mock_table, _ = mock_model
        mock_table.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        td = {"row_name": {"row_markdown_pattern": "{{{Name}}}"}}
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_table_display"]("h", "1", "public", "MyTable", td)
            )
        assert result["status"] == "applied"
        assert mock_table.annotations[_TD_TAG] == td

    async def test_set_table_display_remove(self, ctx, mock_catalog, mock_model):
        _TD_TAG = "tag:isrd.isi.edu,2016:table-display"
        model, mock_table, _ = mock_model
        mock_table.annotations = {_TD_TAG: {"row_name": {}}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_table_display"]("h", "1", "public", "MyTable", None)
            )
        assert result["status"] == "applied"
        assert _TD_TAG not in mock_table.annotations

    async def test_set_table_display_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_table_display"]("h", "1", "s", "t", {"row_name": {}})
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_table_display_failed"

    # -- set_column_display (untested tool) --

    async def test_set_column_display(self, ctx, mock_catalog, mock_model):
        _CD_TAG = "tag:isrd.isi.edu,2016:column-display"
        model, _, mock_col = mock_model
        mock_col.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        cd = {"*": {"markdown_pattern": "**{{{_value}}}**"}}
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_column_display"]("h", "1", "public", "MyTable", "Name", cd)
            )
        assert result["status"] == "applied"
        assert mock_col.annotations[_CD_TAG] == cd

    async def test_set_column_display_remove(self, ctx, mock_catalog, mock_model):
        _CD_TAG = "tag:isrd.isi.edu,2016:column-display"
        model, _, mock_col = mock_model
        mock_col.annotations = {_CD_TAG: {"*": {}}}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_column_display"]("h", "1", "public", "MyTable", "Name", None)
            )
        assert result["status"] == "applied"
        assert _CD_TAG not in mock_col.annotations

    async def test_set_column_display_error(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_column_display"]("h", "1", "s", "t", "col", {"*": {}})
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_set_column_display_failed"

    # -- apply_navbar_annotations --

    async def test_apply_navbar_annotations_basic(self, ctx, mock_catalog, mock_model):
        _CC = "tag:misd.isi.edu,2015:chaise-config"
        _DT = "tag:isrd.isi.edu,2015:display"
        model, _, _ = mock_model
        model.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ):
            result = json.loads(
                await tools["apply_navbar_annotations"]("h", "1", "My Project", "My Title")
            )
        assert result["status"] == "applied"
        assert model.annotations[_CC]["navbarBrandText"] == "My Project"
        assert model.annotations[_CC]["headTitle"] == "My Title"
        assert model.annotations[_CC]["deleteRecord"] is True
        assert model.annotations[_CC]["systemColumnsDisplayEntry"] == ["RID"]
        assert model.annotations[_DT] == {"name_style": {"underline_space": True}}
        assert "navbarMenu" not in model.annotations[_CC]

    async def test_apply_navbar_annotations_default_table(self, ctx, mock_catalog, mock_model):
        _CC = "tag:misd.isi.edu,2015:chaise-config"
        model, _, _ = mock_model
        model.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ):
            await tools["apply_navbar_annotations"](
                "h", "1", default_table={"schema": "isa", "table": "Dataset"}
            )
        assert model.annotations[_CC]["defaultTable"] == {"schema": "isa", "table": "Dataset"}

    async def test_apply_navbar_annotations_navbar_menu(self, ctx, mock_catalog, mock_model):
        _CC = "tag:misd.isi.edu,2015:chaise-config"
        model, _, _ = mock_model
        model.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        menu = {"newTab": False, "children": [{"name": "Data", "url": "/chaise/recordset/#1/isa:Dataset"}]}
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ):
            await tools["apply_navbar_annotations"]("h", "1", navbar_menu=menu)
        assert model.annotations[_CC]["navbarMenu"] == menu

    async def test_apply_navbar_annotations_auto_schema_menu(self, ctx, mock_catalog, mock_model):
        _CC = "tag:misd.isi.edu,2015:chaise-config"
        model, _, _ = mock_model
        model.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        mock_catalog.get.side_effect = None
        mock_catalog.get.return_value.json.return_value = {
            "schemas": {
                "public": {"tables": {"ERMrest_Client": {}}},
                "isa": {"tables": {"Dataset": {}, "Sample": {}}},
            }
        }
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ):
            await tools["apply_navbar_annotations"]("h", "1", auto_schema_menu=True)
        menu = model.annotations[_CC]["navbarMenu"]
        assert menu["newTab"] is False
        assert len(menu["children"]) == 1  # public excluded
        assert menu["children"][0]["name"] == "isa"
        table_names = [c["name"] for c in menu["children"][0]["children"]]
        assert table_names == ["Dataset", "Sample"]

    async def test_apply_navbar_annotations_no_system_columns(self, ctx, mock_catalog, mock_model):
        _CC = "tag:misd.isi.edu,2015:chaise-config"
        model, _, _ = mock_model
        model.annotations = {}
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ):
            await tools["apply_navbar_annotations"]("h", "1", show_system_columns=False)
        assert "systemColumnsDisplayEntry" not in model.annotations[_CC]

    async def test_apply_navbar_annotations_error_emits_audit(self, ctx, mock_catalog):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.annotation.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["apply_navbar_annotations"]("h", "1")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "annotation_apply_navbar_failed"


# ---------------------------------------------------------------------------
# schema tools (DDL)
# ---------------------------------------------------------------------------


class TestSchemaTools:
    def _register(self, ctx):
        from deriva_mcp_core.tools import schema

        schema.register(ctx)
        return ctx._mcp.tools

    def _patch_server(self, mock_catalog):
        return patch("deriva_mcp_core.tools.schema.get_catalog", return_value=mock_catalog)

    async def test_create_table(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_table.name = "NewTable"
        rid_col = MagicMock()
        rid_col.name = "RID"
        mock_table.columns = [rid_col]
        mock_catalog.getCatalogModel.return_value = model
        model.schemas.__getitem__.return_value.create_table.return_value = mock_table
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["create_table"](
                    "h", "1", "public", "NewTable",
                    columns=[{"name": "Title", "type": "text"}],
                )
            )
        assert result["status"] == "created"
        assert result["table_name"] == "NewTable"
        model.schemas.__getitem__.return_value.create_table.assert_called_once()

    async def test_create_table_with_foreign_key(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_table.name = "NewTable"
        mock_table.columns = []  # no columns to iterate
        mock_catalog.getCatalogModel.return_value = model
        model.schemas.__getitem__.return_value.create_table.return_value = mock_table
        tools = self._register(ctx)
        fk_def = {
            "column": "Dataset_RID",
            "referenced_schema": "public",
            "referenced_table": "Dataset",
        }
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["create_table"](
                    "h", "1", "public", "NewTable",
                    columns=[{"name": "Dataset_RID", "type": "text", "nullok": False}],
                    foreign_keys=[fk_def],
                )
            )
        assert result["status"] == "created"

    async def test_create_table_invalid_type_falls_back_to_text(
        self, ctx, mock_catalog, mock_model
    ):
        """Unknown type silently falls back to text (no error)."""
        model, mock_table, _ = mock_model
        mock_table.name = "T"
        mock_table.columns = []
        mock_catalog.getCatalogModel.return_value = model
        model.schemas.__getitem__.return_value.create_table.return_value = mock_table
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["create_table"](
                    "h", "1", "public", "T",
                    columns=[{"name": "x", "type": "notatype"}],
                )
            )
        assert result["status"] == "created"

    async def test_add_column(self, ctx, mock_catalog, mock_model):
        model, mock_table, mock_col = mock_model
        mock_col.name = "Status"
        mock_table.create_column.return_value = mock_col
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_column"]("h", "1", "public", "MyTable", "Status")
            )
        assert result["status"] == "created"
        assert result["column_name"] == "Status"
        mock_table.create_column.assert_called_once()

    async def test_add_column_invalid_type(self, ctx, mock_catalog, mock_model):
        model, _, _ = mock_model
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["add_column"]("h", "1", "public", "MyTable", "x", column_type="nope")
            )
        assert "error" in result

    async def test_set_table_description(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_table_description"]("h", "1", "public", "MyTable", "A dataset table")
            )
        assert result["status"] == "updated"
        assert result["description"] == "A dataset table"
        mock_table.alter.assert_called_once_with(comment="A dataset table")

    async def test_set_column_description(self, ctx, mock_catalog, mock_model):
        model, mock_table, mock_col = mock_model
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_column_description"](
                    "h", "1", "public", "MyTable", "Name", "The display name"
                )
            )
        assert result["status"] == "updated"
        mock_col.alter.assert_called_once_with(comment="The display name")

    async def test_set_column_nullok(self, ctx, mock_catalog, mock_model):
        model, mock_table, mock_col = mock_model
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog):
            result = json.loads(
                await tools["set_column_nullok"]("h", "1", "public", "MyTable", "Name", False)
            )
        assert result["status"] == "updated"
        assert result["nullok"] is False
        mock_col.alter.assert_called_once_with(nullok=False)

    async def test_ddl_fires_schema_change(self, ctx, mock_catalog, mock_model):
        model, mock_table, _ = mock_model
        mock_catalog.getCatalogModel.return_value = model
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.schema.fire_schema_change"
        ) as mock_fire:
            await tools["set_table_description"]("h", "1", "public", "MyTable", "desc")
        mock_fire.assert_called_once_with("h", "1")

    async def test_create_table_blocked(self, disabled_ctx, mock_catalog, mock_model):
        model, _, _ = mock_model
        mock_catalog.getCatalogModel.return_value = model
        from deriva_mcp_core.tools import schema

        schema.register(disabled_ctx)
        tools = disabled_ctx._mcp.tools
        with patch("deriva_mcp_core.context.get_catalog", return_value=mock_catalog):
            result = json.loads(
                await tools["create_table"]("h", "1", "public", "T")
            )
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_add_column_blocked(self, disabled_ctx, mock_catalog, mock_model):
        model, _, _ = mock_model
        mock_catalog.getCatalogModel.return_value = model
        from deriva_mcp_core.tools import schema

        schema.register(disabled_ctx)
        tools = disabled_ctx._mcp.tools
        with patch("deriva_mcp_core.context.get_catalog", return_value=mock_catalog):
            result = json.loads(
                await tools["add_column"]("h", "1", "public", "MyTable", "x")
            )
        assert "error" in result
        assert "disabled" in result["error"]

    async def test_create_table_failure_emits_audit(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.schema.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["create_table"]("h", "1", "public", "NewTable")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "schema_create_table_failed"
        assert mock_audit.call_args[1]["error_type"] == "RuntimeError"

    async def test_add_column_failure_emits_audit(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.schema.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["add_column"]("h", "1", "public", "MyTable", "Status")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "schema_add_column_failed"

    async def test_set_table_description_failure_emits_audit(
        self, ctx, mock_catalog, mock_model
    ):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.schema.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_table_description"]("h", "1", "public", "MyTable", "desc")
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "schema_set_table_description_failed"

    async def test_set_column_nullok_failure_emits_audit(self, ctx, mock_catalog, mock_model):
        mock_catalog.getCatalogModel.side_effect = RuntimeError("ERMrest error")
        tools = self._register(ctx)
        with self._patch_server(mock_catalog), patch(
            "deriva_mcp_core.tools.schema.audit_event"
        ) as mock_audit:
            result = json.loads(
                await tools["set_column_nullok"]("h", "1", "public", "MyTable", "Name", False)
            )
        assert "error" in result
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "schema_set_column_nullok_failed"
