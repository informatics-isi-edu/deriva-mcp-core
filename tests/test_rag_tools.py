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

    async def add(self, chunks: list[Chunk]) -> None:
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


def _make_settings(enabled: bool = True, auto_update: bool = False, auto_enrich: bool = True) -> MagicMock:
    settings = MagicMock()
    settings.enabled = enabled
    settings.vector_backend = "chroma"
    settings.auto_update = auto_update
    settings.auto_enrich = auto_enrich
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
        # The user's visibility-class hash must be registered in _user_schema_hashes
        # before rag_search will include schema results (correct ACL isolation behaviour).
        from deriva_mcp_core.rag import tools as rag_tools
        from deriva_mcp_core.rag.schema import schema_source_name

        my_hash = "aabbccdd1122"
        my_source = schema_source_name("localhost", "1", my_hash)
        other_source = schema_source_name("otherhost", "2", "deadbeef9999")
        mock_store.set_search_results(
            [
                SearchResult(text="mine", source=my_source, doc_type="schema", score=0.9, metadata={}),
                SearchResult(text="other", source=other_source, doc_type="schema", score=0.85, metadata={}),
                SearchResult(text="doc", source="deriva-py-docs:guide.md", doc_type="user-guide", score=0.8, metadata={}),
            ]
        )
        tools, _ = _register_rag(ctx, mock_store)
        # Register the caller's visibility class (resolve_user_identity returns "anonymous"
        # in tests since no contextvar is set).
        rag_tools._user_schema_hashes[("anonymous", "localhost", "1")] = my_hash[:16]
        try:
            result = json.loads(await tools["rag_search"]("q", hostname="localhost", catalog_id="1"))
            sources = [r["source"] for r in result]
            assert my_source in sources
            assert other_source not in sources
            assert "deriva-py-docs:guide.md" in sources
        finally:
            rag_tools._user_schema_hashes.pop(("anonymous", "localhost", "1"), None)

    async def test_schema_results_excluded_when_hash_not_registered(
        self, ctx, mock_store
    ):
        # When the caller's schema hash is not in _user_schema_hashes (schema not
        # yet indexed for this user), all schema results must be excluded to prevent
        # serving a different user's visibility class.
        from deriva_mcp_core.rag import tools as rag_tools
        from deriva_mcp_core.rag.schema import schema_source_name

        other_source = schema_source_name("localhost", "1", "aabbccdd1122")
        mock_store.set_search_results(
            [
                SearchResult(text="s", source=other_source, doc_type="schema", score=0.9, metadata={}),
                SearchResult(text="d", source="deriva-py-docs:guide.md", doc_type="user-guide", score=0.8, metadata={}),
            ]
        )
        tools, _ = _register_rag(ctx, mock_store)
        # Ensure no hash is registered for this user/catalog
        rag_tools._user_schema_hashes.pop(("anonymous", "localhost", "1"), None)
        result = json.loads(await tools["rag_search"]("q", hostname="localhost", catalog_id="1"))
        sources = [r["source"] for r in result]
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

    async def test_force_false_calls_ingest_with_force_false(self, ctx, mock_store):
        tools, docs_mgr = _register_rag(ctx, mock_store)
        await tools["rag_update_docs"]("deriva-py-docs")
        docs_mgr.ingest.assert_called_once()
        _, kwargs = docs_mgr.ingest.call_args
        assert kwargs.get("force") is False

    async def test_force_true_calls_ingest_with_force_true(self, ctx, mock_store):
        tools, docs_mgr = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_update_docs"]("deriva-py-docs", force=True))
        assert "updated" in result
        docs_mgr.ingest.assert_called_once()
        _, kwargs = docs_mgr.ingest.call_args
        assert kwargs.get("force") is True


# ---------------------------------------------------------------------------
# Tests: rag_ingest (async task submission)
# ---------------------------------------------------------------------------


class TestRagIngest:
    @pytest.fixture()
    def task_ctx(self, capturing_mcp):
        from deriva_mcp_core.tasks.manager import TaskManager
        mgr = TaskManager()
        _ctx = PluginContext(capturing_mcp, task_manager=mgr)
        _set_plugin_context(_ctx)
        yield _ctx, mgr
        _set_plugin_context(None)

    async def test_unknown_source_returns_error(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_ingest"]("nonexistent-source"))
        assert "error" in result

    async def test_submits_task_and_returns_task_id(self, task_ctx, mock_store):
        import asyncio
        _ctx, mgr = task_ctx
        tools, docs_mgr = _register_rag(_ctx, mock_store)
        with patch("deriva_mcp_core.context._current_user_id") as mock_uid, \
             patch("deriva_mcp_core.context._current_bearer_token") as mock_tok:
            mock_uid.get.return_value = "alice"
            mock_tok.get.return_value = "tok"
            result = json.loads(await tools["rag_ingest"]("deriva-py-docs"))
        assert result["status"] == "submitted"
        assert "task_id" in result
        await asyncio.sleep(0.1)
        record = mgr.get(result["task_id"], "alice")
        assert record is not None
        assert record.state == "completed"
        assert "deriva-py-docs" in record.result["ingested"]

    async def test_ingest_always_calls_force_true(self, task_ctx, mock_store):
        import asyncio
        _ctx, mgr = task_ctx
        tools, docs_mgr = _register_rag(_ctx, mock_store)
        with patch("deriva_mcp_core.context._current_user_id") as mock_uid, \
             patch("deriva_mcp_core.context._current_bearer_token") as mock_tok:
            mock_uid.get.return_value = "alice"
            mock_tok.get.return_value = "tok"
            await tools["rag_ingest"]("deriva-py-docs")
            await asyncio.sleep(0.1)
        docs_mgr.ingest.assert_called_once()
        _, kwargs = docs_mgr.ingest.call_args
        assert kwargs.get("force") is True

    async def test_submit_error_returns_error(self, capturing_mcp, mock_store):
        _ctx = PluginContext(capturing_mcp, task_manager=None)
        _set_plugin_context(_ctx)
        tools, _ = _register_rag(_ctx, mock_store)
        with patch("deriva_mcp_core.context._current_user_id") as mock_uid, \
             patch("deriva_mcp_core.context._current_bearer_token") as mock_tok:
            mock_uid.get.return_value = "alice"
            mock_tok.get.return_value = None
            result = json.loads(await tools["rag_ingest"]("deriva-py-docs"))
        assert "error" in result
        _set_plugin_context(None)


# ---------------------------------------------------------------------------
# Tests: rag_status
# ---------------------------------------------------------------------------


class TestRagStatus:
    async def test_status_includes_backend(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_status"]())
        assert result["enabled"] is True
        assert result["vector_backend"] == "chroma"
        assert "indexed_sources" in result
        assert "available_to_ingest" in result

    async def test_status_shows_indexed_sources(self, ctx, mock_store):
        mock_store.chunks = [
            Chunk(text="t", source="myrepo:file.md", doc_type="user-guide", chunk_index=0),
        ]
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_status"]())
        assert "myrepo:file.md" in result["indexed_sources"]
        assert result["indexed_sources"]["myrepo:file.md"]["chunk_count"] == 1

    async def test_status_lists_unindexed_registered_sources(self, ctx, mock_store):
        # Built-in sources are registered but the mock store has no chunks for them.
        # They must appear in available_to_ingest, not in indexed_sources.
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_status"]())
        assert "deriva-py-docs" in result["available_to_ingest"]
        assert "deriva-py-docs" not in result["indexed_sources"]

    async def test_status_removes_indexed_source_from_available(self, ctx, mock_store):
        # Indexed sources use compound keys ("source-name:path"). The source name
        # prefix must be extracted so the source drops out of available_to_ingest.
        mock_store.chunks = [
            Chunk(text="t", source="deriva-py-docs:docs/README.md", doc_type="user-guide", chunk_index=0),
            Chunk(text="t2", source="deriva-py-docs:docs/BUILD.md", doc_type="user-guide", chunk_index=0),
        ]
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_status"]())
        assert "deriva-py-docs" not in result["available_to_ingest"]

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

        with patch("deriva_mcp_core.rag.tools.get_catalog", return_value=mock_catalog):
            tools, _ = _register_rag(ctx, mock_store)
            result = json.loads(await tools["rag_index_schema"]("host.example.org", "1"))

        assert result["status"] == "indexed"
        assert result["hostname"] == "host.example.org"
        assert "schema_hash" in result
        assert len(result["schema_hash"]) == 16

    async def test_error_returns_error_key(self, ctx, mock_store):
        with patch(
            "deriva_mcp_core.rag.tools.get_catalog", side_effect=RuntimeError("no cred")
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

        with (
            patch("deriva_mcp_core.rag.tools.get_catalog", return_value=mock_catalog),
            patch("deriva_mcp_core.rag.tools.get_request_user_id", return_value="user@test.org"),
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

        with (
            patch("deriva_mcp_core.rag.tools.get_catalog", return_value=mock_catalog),
            patch("deriva_mcp_core.rag.tools.get_request_user_id", return_value="user@test.org"),
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
            "deriva_mcp_core.rag.tools.get_catalog", side_effect=RuntimeError("no cred")
        ):
            tools, _ = _register_rag(ctx, mock_store)
            result = json.loads(
                await tools["rag_index_table"]("host.example.org", "1", "public", "Item")
            )
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests: rag_import_chunks
# ---------------------------------------------------------------------------


class TestRagImportChunks:
    async def test_import_basic_chunks(self, ctx, mock_store, tmp_path):
        chunks = [
            {"text": "Chunk one text here.", "source": "mysrc:file.md", "doc_type": "user-guide"},
            {"text": "Chunk two text here.", "source": "mysrc:file.md", "doc_type": "user-guide"},
        ]
        chunk_file = tmp_path / "chunks.json"
        chunk_file.write_text(json.dumps(chunks))

        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_import_chunks"](str(chunk_file)))
        assert result["status"] == "imported"
        assert result["chunk_count"] == 2
        assert len(mock_store.chunks) == 2

    async def test_import_overrides_source_and_doc_type(self, ctx, mock_store, tmp_path):
        chunks = [{"text": "Override me.", "source": "original", "doc_type": "old"}]
        chunk_file = tmp_path / "chunks.json"
        chunk_file.write_text(json.dumps(chunks))

        tools, _ = _register_rag(ctx, mock_store)
        await tools["rag_import_chunks"](
            str(chunk_file), source_name="new-source", doc_type="new-type"
        )
        assert mock_store.chunks[0].source == "new-source"
        assert mock_store.chunks[0].doc_type == "new-type"

    async def test_import_replace_deletes_existing(self, ctx, mock_store, tmp_path):
        from deriva_mcp_core.rag.store import Chunk
        existing = Chunk(
            text="Old chunk", source="my-source:file.md",
            doc_type="user-guide", section_heading="", heading_hierarchy=[], chunk_index=0,
        )
        mock_store.chunks.append(existing)

        chunks = [{"text": "New chunk.", "source": "my-source:file.md", "doc_type": "user-guide"}]
        chunk_file = tmp_path / "chunks.json"
        chunk_file.write_text(json.dumps(chunks))

        tools, _ = _register_rag(ctx, mock_store)
        await tools["rag_import_chunks"](
            str(chunk_file), source_name="my-source:file.md", replace=True
        )
        assert len(mock_store.chunks) == 1
        assert mock_store.chunks[0].text == "New chunk."

    async def test_import_replace_requires_source_name(self, ctx, mock_store, tmp_path):
        chunks = [{"text": "text"}]
        chunk_file = tmp_path / "chunks.json"
        chunk_file.write_text(json.dumps(chunks))

        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_import_chunks"](str(chunk_file), replace=True))
        assert "error" in result

    async def test_import_skips_empty_text(self, ctx, mock_store, tmp_path):
        chunks = [{"text": ""}, {"text": "Good chunk here."}]
        chunk_file = tmp_path / "chunks.json"
        chunk_file.write_text(json.dumps(chunks))

        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_import_chunks"](str(chunk_file)))
        assert result["chunk_count"] == 1

    async def test_import_nonexistent_file(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_import_chunks"]("/no/such/file.json"))
        assert "error" in result

    async def test_import_non_array_file(self, ctx, mock_store, tmp_path):
        chunk_file = tmp_path / "bad.json"
        chunk_file.write_text(json.dumps({"not": "an array"}))
        tools, _ = _register_rag(ctx, mock_store)
        result = json.loads(await tools["rag_import_chunks"](str(chunk_file)))
        assert "error" in result

    def test_rag_import_chunks_registered(self, ctx, mock_store):
        tools, _ = _register_rag(ctx, mock_store)
        assert "rag_import_chunks" in tools


# ---------------------------------------------------------------------------
# Tests: rag_ingest_datasets task result format
# ---------------------------------------------------------------------------


class TestRagIngestDatasets:
    """Verify that rag_ingest_datasets returns structured per-source stats."""

    @pytest.fixture()
    def task_ctx(self, capturing_mcp):
        from deriva_mcp_core.tasks.manager import TaskManager
        mgr = TaskManager()
        _ctx = PluginContext(capturing_mcp, task_manager=mgr)
        _set_plugin_context(_ctx)
        yield _ctx, mgr
        _set_plugin_context(None)

    def _make_mock_catalog(self, rows: list[dict]):
        mock_catalog = MagicMock()
        resp = MagicMock()
        resp.json.return_value = rows
        mock_catalog.get.return_value = resp
        return mock_catalog

    async def test_result_has_structured_stats(self, task_ctx, mock_store):
        """Completed enricher run reports rows_fetched, failed, chunks -- not a bare count."""
        import asyncio
        _ctx, mgr = task_ctx

        async def enricher(row, catalog):
            return f"## Dataset {row['RID']}\n\nSome content about this dataset."

        _ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=enricher,
            hostname="host.example.org",
            catalog_id="1",
        )

        tools, _ = _register_rag(_ctx, mock_store)
        mock_catalog = self._make_mock_catalog([{"RID": "A"}, {"RID": "B"}])

        with patch("deriva_mcp_core.context._current_user_id") as mock_uid, \
             patch("deriva_mcp_core.context._current_bearer_token") as mock_tok, \
             patch("deriva_mcp_core.rag.tools.get_catalog", return_value=mock_catalog):
            mock_uid.get.return_value = "alice"
            mock_tok.get.return_value = "tok"
            submit_result = json.loads(await tools["rag_ingest_datasets"]("host.example.org", "1"))
            assert submit_result["status"] == "submitted"
            task_id = submit_result["task_id"]
            await asyncio.sleep(0.1)

        record = mgr.get(task_id, "alice")
        assert record is not None
        assert record.state == "completed"

        src = "enriched:host.example.org:1:isa:dataset"
        per_source = record.result["enriched"][src]
        assert per_source["rows_fetched"] == 2
        assert per_source["failed"] == 0
        assert per_source["chunks"] > 0

    async def test_no_matching_indexer_returns_error(self, task_ctx, mock_store):
        """When no indexer matches the catalog, return an error immediately (no task)."""
        _ctx, _ = task_ctx
        tools, _ = _register_rag(_ctx, mock_store)
        result = json.loads(await tools["rag_ingest_datasets"]("other.host.org", "1"))
        assert "error" in result
        assert "task_id" not in result

    async def test_source_name_filter_no_match_returns_error(self, task_ctx, mock_store):
        """Explicit source_name that matches no indexer returns an error."""
        _ctx, _ = task_ctx

        async def enricher(row, catalog):
            return "text"

        _ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=enricher,
            hostname="host.example.org",
            catalog_id="1",
        )

        tools, _ = _register_rag(_ctx, mock_store)
        result = json.loads(await tools["rag_ingest_datasets"](
            "host.example.org", "1",
            source_name="enriched:host.example.org:1:isa:othertable",
        ))
        assert "error" in result

    async def test_all_rows_fail_produces_zero_chunks(self, task_ctx, mock_store):
        """When the enricher raises on every row the chunk count is 0 with failed > 0."""
        import asyncio
        _ctx, mgr = task_ctx

        async def bad_enricher(row, catalog):
            raise RuntimeError("enricher exploded")

        _ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=bad_enricher,
            hostname="host.example.org",
            catalog_id="1",
        )

        tools, _ = _register_rag(_ctx, mock_store)
        mock_catalog = self._make_mock_catalog([{"RID": "X"}])

        with patch("deriva_mcp_core.context._current_user_id") as mock_uid, \
             patch("deriva_mcp_core.context._current_bearer_token") as mock_tok, \
             patch("deriva_mcp_core.rag.tools.get_catalog", return_value=mock_catalog):
            mock_uid.get.return_value = "alice"
            mock_tok.get.return_value = "tok"
            submit_result = json.loads(await tools["rag_ingest_datasets"]("host.example.org", "1"))
            task_id = submit_result["task_id"]
            await asyncio.sleep(0.1)

        record = mgr.get(task_id, "alice")
        assert record is not None
        assert record.state == "completed"

        src = "enriched:host.example.org:1:isa:dataset"
        per_source = record.result["enriched"][src]
        assert per_source["rows_fetched"] == 1
        assert per_source["failed"] == 1
        assert per_source["chunks"] == 0


# ---------------------------------------------------------------------------
# Tests: _run_dataset_enricher URL generation
# ---------------------------------------------------------------------------


async def _fire_enricher(ctx, mock_store, mock_catalog):
    """Register RAG, fire on_catalog_connect, wait for background tasks to run.

    has_schema and index_schema run against mock_store (no real DERIVA calls).
    get_catalog is patched to return mock_catalog so enricher URL can be captured.
    """
    import asyncio

    _register_rag(ctx, mock_store)

    with patch("deriva_mcp_core.rag.tools.get_catalog", return_value=mock_catalog):
        from deriva_mcp_core.plugin.api import fire_catalog_connect
        fire_catalog_connect("host.example.org", "1", "abc123", {"schemas": {}})
        await asyncio.sleep(0.1)


class TestDatasetEnricherUrl:
    """Verify that _run_dataset_enricher builds correct ERMrest URLs.

    The dataset fetch must use ERMrest path predicates (/col=val) not query
    params (?col=val). Boolean filter values must be lowercase strings. The
    ?limit= query param must be appended when indexer.limit is set.
    """

    def _make_mock_catalog(self):
        captured: list[str] = []
        mock_catalog = MagicMock()

        def _get(url):
            captured.append(url)
            resp = MagicMock()
            resp.json.return_value = []
            return resp

        mock_catalog.get.side_effect = _get
        return mock_catalog, captured

    async def test_bool_filter_uses_path_predicate_lowercase(self, ctx, mock_store):
        """released=True filter must produce /released=true path segment, not query param."""
        async def enricher(row, catalog):
            return ""

        ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=enricher,
            filter={"released": True},
            hostname="host.example.org",
            catalog_id="1",
            auto_enrich=True,
        )

        mock_catalog, captured = self._make_mock_catalog()
        await _fire_enricher(ctx, mock_store, mock_catalog)

        dataset_urls = [u for u in captured if "isa:dataset" in u]
        assert dataset_urls, "No catalog.get call with isa:dataset found"
        url = dataset_urls[0]
        assert "/released=true" in url, f"Expected /released=true in URL, got: {url}"
        assert "?released" not in url, f"Unexpected query param format in URL: {url}"
        assert "released=True" not in url, f"Python bool True (uppercase) found in URL: {url}"

    async def test_limit_appended_as_query_param(self, ctx, mock_store):
        """When limit is set, ?limit=N must appear after any path predicates."""
        async def enricher(row, catalog):
            return ""

        ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=enricher,
            filter={"released": True},
            limit=50,
            hostname="host.example.org",
            catalog_id="1",
            auto_enrich=True,
        )

        mock_catalog, captured = self._make_mock_catalog()
        await _fire_enricher(ctx, mock_store, mock_catalog)

        dataset_urls = [u for u in captured if "isa:dataset" in u]
        assert dataset_urls, "No catalog.get call with isa:dataset found"
        url = dataset_urls[0]
        assert "?limit=50" in url, f"Expected ?limit=50 in URL, got: {url}"
        assert "/released=true" in url

    async def test_no_limit_no_query_param(self, ctx, mock_store):
        """When limit is None, no ?limit= query param must appear."""
        async def enricher(row, catalog):
            return ""

        ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=enricher,
            filter={"released": True},
            hostname="host.example.org",
            catalog_id="1",
            auto_enrich=True,
        )

        mock_catalog, captured = self._make_mock_catalog()
        await _fire_enricher(ctx, mock_store, mock_catalog)

        dataset_urls = [u for u in captured if "isa:dataset" in u]
        assert dataset_urls, "No catalog.get call with isa:dataset found"
        url = dataset_urls[0]
        assert "?limit" not in url, f"Unexpected ?limit in URL: {url}"

    async def test_chunk_indices_unique_across_rows(self, ctx, mock_store):
        """Each row produces chunks starting at index 0; the enricher must renumber
        them globally so no two chunks share the same (source, chunk_index) pair."""
        call_count = 0

        async def enricher(row, catalog):
            nonlocal call_count
            call_count += 1
            # Produce text that will yield multiple chunks per row
            return "\n\n".join(f"## Section {j}\n\n" + ("word " * 100) for j in range(3))

        ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=enricher,
            hostname="host.example.org",
            catalog_id="1",
            auto_enrich=True,
        )

        # Return two rows from the catalog so the enricher runs twice
        mock_catalog = MagicMock()
        mock_catalog.get.side_effect = lambda url: (
            type("R", (), {"json": lambda self: [{"RID": "1"}, {"RID": "2"}]})()
        )

        await _fire_enricher(ctx, mock_store, mock_catalog)

        assert call_count == 2, "Enricher should have been called once per row"
        ids = [f"{c.source}:{c.chunk_index}" for c in mock_store.chunks]
        assert len(ids) == len(set(ids)), f"Duplicate chunk IDs found: {ids}"

    async def test_hostname_scope_prevents_wrong_catalog(self, ctx, mock_store):
        """Enricher scoped to facebase.org must not fire for other hostnames."""
        async def enricher(row, catalog):
            return ""

        ctx.rag_dataset_indexer(
            schema="isa",
            table="dataset",
            enricher=enricher,
            hostname="www.facebase.org",
            catalog_id="1",
            auto_enrich=True,
        )

        mock_catalog, captured = self._make_mock_catalog()

        _register_rag(ctx, mock_store)
        import asyncio
        with patch("deriva_mcp_core.rag.tools.get_catalog", return_value=mock_catalog):
            from deriva_mcp_core.plugin.api import fire_catalog_connect
            fire_catalog_connect("other.server.org", "1", "abc123", {"schemas": {}})
            await asyncio.sleep(0.1)

        dataset_urls = [u for u in captured if "isa:dataset" in u]
        assert not dataset_urls, "Enricher fired for wrong hostname"
