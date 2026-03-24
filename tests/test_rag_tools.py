from __future__ import annotations

"""Unit tests for the RAG MCP tools registered by rag/__init__.py."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deriva_mcp_core.plugin.api import PluginContext, _set_plugin_context
from deriva_mcp_core.rag.store import Chunk, SearchResult, SourceStats

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class _CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def resource(self, *a: Any, **kw: Any):
        return lambda fn: fn

    def prompt(self, *a: Any, **kw: Any):
        return lambda fn: fn


class _MockStore:
    """In-memory VectorStore for testing."""

    def __init__(self):
        self.chunks: list[Chunk] = []
        self._search_results: list[SearchResult] = []

    def set_search_results(self, results: list[SearchResult]) -> None:
        self._search_results = results

    async def upsert(self, chunks: list[Chunk]) -> None:
        self.chunks.extend(chunks)

    async def search(
        self, query: str, limit: int = 10, where: dict | None = None
    ) -> list[SearchResult]:
        return self._search_results[:limit]

    async def delete_source(self, source: str) -> None:
        self.chunks = [c for c in self.chunks if c.source != source]

    async def has_source(self, source: str) -> bool:
        return any(c.source == source for c in self.chunks)

    async def source_stats(self) -> dict[str, SourceStats]:
        stats: dict[str, int] = {}
        for c in self.chunks:
            stats[c.source] = stats.get(c.source, 0) + 1
        return {
            src: SourceStats(chunk_count=count, indexed_at=None) for src, count in stats.items()
        }


@pytest.fixture()
def capturing_mcp():
    return _CapturingMCP()


@pytest.fixture()
def ctx(capturing_mcp):
    _ctx = PluginContext(capturing_mcp)
    _set_plugin_context(_ctx)
    yield _ctx
    _set_plugin_context(None)


@pytest.fixture()
def mock_store():
    return _MockStore()


def _make_settings(enabled: bool = True, auto_update: bool = False) -> MagicMock:
    settings = MagicMock()
    settings.enabled = enabled
    settings.vector_backend = "chroma"
    settings.auto_update = auto_update
    return settings


def _register_rag(ctx, store) -> tuple[dict[str, Any], MagicMock]:
    """Call rag.register() with RAG enabled, using a mock store.

    Patches are applied at the module where each name is defined (the correct
    target for lazy imports inside register()).

    Returns:
        (tools dict, mock docs_manager instance)
    """
    mock_docs_mgr = MagicMock()
    mock_docs_mgr.update = AsyncMock(return_value=0)
    mock_docs_mgr.ingest = AsyncMock(return_value=0)
    mock_docs_mgr_cls = MagicMock(return_value=mock_docs_mgr)

    with (
        patch("deriva_mcp_core.rag.config.RAGSettings") as mock_settings_cls,
        patch("deriva_mcp_core.rag.store.get_store", return_value=store),
        patch("deriva_mcp_core.rag.docs.RAGDocsManager", mock_docs_mgr_cls),
    ):
        mock_settings_cls.return_value = _make_settings()
        from deriva_mcp_core.rag import register

        register(ctx)

    return ctx._mcp.tools, mock_docs_mgr


# ---------------------------------------------------------------------------
# Tests: register() disabled
# ---------------------------------------------------------------------------


class TestRegisterDisabled:
    def test_no_tools_when_disabled(self, ctx):
        with patch("deriva_mcp_core.rag.config.RAGSettings") as mock_cls:
            mock_cls.return_value = _make_settings(enabled=False)
            from deriva_mcp_core.rag import register

            register(ctx)
        rag_tools = {k for k in ctx._mcp.tools if k.startswith("rag_")}
        assert not rag_tools

    def test_tools_registered_when_enabled(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        assert "rag_search" in tools
        assert "rag_update_docs" in tools
        assert "rag_index_schema" in tools
        assert "rag_index_table" in tools
        assert "rag_status" in tools


# ---------------------------------------------------------------------------
# Tests: rag_search
# ---------------------------------------------------------------------------


class TestRagSearch:
    async def test_returns_results(self, ctx, mock_store):
        mock_store.set_search_results(
            [
                SearchResult(
                    text="Some result text",
                    source="deriva-py-docs:docs/guide.md",
                    doc_type="user-guide",
                    score=0.92,
                    metadata={},
                )
            ]
        )
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_search"]("how to connect"))
        assert isinstance(result, list)
        assert result[0]["score"] == 0.92
        assert "Some result text" in result[0]["text"]

    async def test_empty_results(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_search"]("nothing here"))
        assert result == []

    async def test_error_returns_error_key(self, ctx, mock_store):
        async def _fail(*a, **kw):
            raise RuntimeError("boom")

        mock_store.search = _fail
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_search"]("query"))
        assert "error" in result

    async def test_doc_type_filter_passed_to_store(self, ctx, mock_store):
        captured_where: list = []

        async def _capturing_search(query, limit=10, where=None):
            captured_where.append(where)
            return []

        mock_store.search = _capturing_search
        tools, _ = _register_rag(ctx, mock_store)
        await tools["rag_search"]("q", doc_type="schema")
        assert captured_where[0] == {"doc_type": "schema"}

    async def test_hostname_catalog_does_not_inject_source_prefix_into_store(
        self, ctx, mock_store
    ):
        # Regression: hostname+catalog_id previously put a bogus "source_prefix"
        # key into the where dict, which caused Chroma to return zero results.
        captured_where: list = []

        async def _capturing_search(query, limit=10, where=None):
            captured_where.append(where)
            return []

        mock_store.search = _capturing_search
        tools, _ = _register_rag(ctx, mock_store)
        await tools["rag_search"]("q", hostname="localhost", catalog_id="1")
        assert captured_where[0] is None
        assert "source_prefix" not in (captured_where[0] or {})

    async def test_hostname_catalog_filters_out_other_catalog_schema_results(
        self, ctx, mock_store
    ):
        # Schema chunks from a different catalog should be excluded; doc chunks
        # and same-catalog schema chunks should be kept.
        from deriva_mcp_core.rag.schema import schema_source_name

        my_source = schema_source_name("localhost", "1", "aabbccdd1122")
        other_source = schema_source_name("otherhost", "2", "deadbeef9999")
        mock_store.set_search_results(
            [
                SearchResult(text="mine", source=my_source, doc_type="schema", score=0.9, metadata={}),
                SearchResult(text="other", source=other_source, doc_type="schema", score=0.85, metadata={}),
                SearchResult(text="doc", source="deriva-py-docs:guide.md", doc_type="user-guide", score=0.8, metadata={}),
            ]
        )
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_search"]("q", hostname="localhost", catalog_id="1"))
        sources = [r["source"] for r in result]
        assert my_source in sources
        assert other_source not in sources
        assert "deriva-py-docs:guide.md" in sources


# ---------------------------------------------------------------------------
# Tests: rag_update_docs
# ---------------------------------------------------------------------------


class TestRagUpdateDocs:
    async def test_unknown_source_returns_error(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_update_docs"]("nonexistent-source"))
        assert "error" in result

    async def test_no_source_updates_all_builtin(self, ctx, mock_store):
        tools, docs_mgr = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_update_docs"]())
        assert "updated" in result
        # Three builtin sources -> three entries
        assert len(result["updated"]) == 3

    async def test_named_source_updates_one(self, ctx, mock_store):
        tools, docs_mgr = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_update_docs"]("deriva-py-docs"))
        assert "updated" in result
        assert "deriva-py-docs" in result["updated"]
        assert len(result["updated"]) == 1


# ---------------------------------------------------------------------------
# Tests: rag_status
# ---------------------------------------------------------------------------


class TestRagStatus:
    async def test_status_includes_backend(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_status"]())
        assert result["enabled"] is True
        assert result["vector_backend"] == "chroma"
        assert "sources" in result

    async def test_status_shows_known_sources(self, ctx, mock_store):
        mock_store.chunks = [
            Chunk(text="t", source="myrepo:file.md", doc_type="user-guide", chunk_index=0),
        ]
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_status"]())
        assert "myrepo:file.md" in result["sources"]
        assert result["sources"]["myrepo:file.md"]["chunk_count"] == 1

    async def test_status_error_returns_error_key(self, ctx, mock_store):
        async def _fail() -> dict:
            raise RuntimeError("db down")

        mock_store.source_stats = _fail
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_status"]())
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests: rag_index_schema
# ---------------------------------------------------------------------------


class TestRagIndexSchema:
    async def test_indexes_and_returns_hash(self, ctx, mock_store):
        schema_json = {
            "schemas": {"public": {"tables": {"T": {"column_definitions": [], "foreign_keys": []}}}}
        }
        mock_catalog = MagicMock()
        mock_catalog.get.return_value.json.return_value = schema_json
        mock_server = MagicMock()
        mock_server.connect_ermrest.return_value = mock_catalog

        # Patch before _register_rag so the import inside register() binds the mock
        with patch("deriva_mcp_core.context.get_deriva_server", return_value=mock_server):
            tools, _ = _register_rag(ctx, mock_store)
            result = json.loads(await tools["rag_index_schema"]("host.example.org", "1"))

        assert result["status"] == "indexed"
        assert result["hostname"] == "host.example.org"
        assert "schema_hash" in result
        assert len(result["schema_hash"]) == 16

    async def test_error_returns_error_key(self, ctx, mock_store):
        with patch(
            "deriva_mcp_core.context.get_deriva_server", side_effect=RuntimeError("no cred")
        ):
            tools, _ = _register_rag(ctx, mock_store)
            result = json.loads(await tools["rag_index_schema"]("host.example.org", "1"))
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests: rag_index_table
# ---------------------------------------------------------------------------


class TestRagIndexTable:
    async def test_indexes_rows_and_returns_count(self, ctx, mock_store):
        rows = [
            {"RID": "1", "Name": "Alpha", "Description": "First item"},
            {"RID": "2", "Name": "Beta", "Description": "Second item"},
        ]
        mock_catalog = MagicMock()
        mock_catalog.get.return_value.json.return_value = rows
        mock_server = MagicMock()
        mock_server.connect_ermrest.return_value = mock_catalog

        with (
            patch("deriva_mcp_core.context.get_deriva_server", return_value=mock_server),
            patch("deriva_mcp_core.context.get_request_user_id", return_value="user@test.org"),
        ):
            tools, _ = _register_rag(ctx, mock_store)
            result = json.loads(
                await tools["rag_index_table"]("host.example.org", "1", "public", "Item")
            )

        assert result["status"] == "indexed"
        assert result["row_count"] == 2
        assert result["schema"] == "public"
        assert result["table"] == "Item"
        # Rows should have been upserted into the store
        assert len(mock_store.chunks) > 0

    async def test_empty_rows_produces_no_chunks(self, ctx, mock_store):
        mock_catalog = MagicMock()
        mock_catalog.get.return_value.json.return_value = []
        mock_server = MagicMock()
        mock_server.connect_ermrest.return_value = mock_catalog

        with (
            patch("deriva_mcp_core.context.get_deriva_server", return_value=mock_server),
            patch("deriva_mcp_core.context.get_request_user_id", return_value="user@test.org"),
        ):
            tools, _ = _register_rag(ctx, mock_store)
            result = json.loads(
                await tools["rag_index_table"]("host.example.org", "1", "public", "Empty")
            )

        assert result["status"] == "indexed"
        assert result["row_count"] == 0
        assert len(mock_store.chunks) == 0

    async def test_error_returns_error_key(self, ctx, mock_store):
        with patch(
            "deriva_mcp_core.context.get_deriva_server", side_effect=RuntimeError("no cred")
        ):
            tools, _ = _register_rag(ctx, mock_store)
            result = json.loads(
                await tools["rag_index_table"]("host.example.org", "1", "public", "Item")
            )
        assert "error" in result
