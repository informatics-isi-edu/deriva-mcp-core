"""Automated integration tests for the RAG subsystem.

These tests require the 'rag' optional dependencies (chromadb, asyncpg) and are
marked with @pytest.mark.rag. They run without any live external services:

    ChromaDB   -- embedded, uses a tmp_path directory (no server)
    PostgreSQL -- spawned in-process via testing.postgresql; skipped automatically
                  if initdb is not on PATH or pgvector extension is not installed

Run all RAG integration tests:
    uv run pytest -m rag

Run only the ChromaDB tests:
    uv run pytest -m rag -k "not Pgvector"

Notes:
    - ChromaDB downloads the all-MiniLM-L6-v2 embedding model on first run.
      Subsequent runs use the cached model (~25 MB, stored in ~/.cache).
    - pgvector tests require postgresql-<ver>-pgvector on the host system.
      Tests skip gracefully if PostgreSQL or pgvector is unavailable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip(
    "chromadb",
    reason="chromadb not installed; run: uv sync --extra rag",
)

pytestmark = pytest.mark.rag


# ---------------------------------------------------------------------------
# Shared schema fixtures
# ---------------------------------------------------------------------------

_SCHEMA_A: dict = {
    "schemas": {
        "public": {
            "comment": "Integration test schema A",
            "tables": {
                "Dataset": {
                    "kind": "table",
                    "comment": "Research datasets",
                    "column_definitions": [
                        {"name": "RID", "type": {"typename": "text"}, "nullok": False},
                        {
                            "name": "Title",
                            "type": {"typename": "text"},
                            "nullok": True,
                            "comment": "Dataset title",
                        },
                    ],
                    "foreign_keys": [],
                }
            },
        }
    }
}

_SCHEMA_B: dict = {
    "schemas": {
        "public": {
            "tables": {
                "Subject": {
                    "kind": "table",
                    "comment": "Study subjects",
                    "column_definitions": [
                        {"name": "RID", "type": {"typename": "text"}, "nullok": False},
                        {"name": "Name", "type": {"typename": "text"}, "nullok": True},
                    ],
                    "foreign_keys": [],
                }
            }
        }
    }
}


# ---------------------------------------------------------------------------
# ChromaVectorStore fixtures / helpers
# ---------------------------------------------------------------------------


def _chroma_settings(tmp_path: Any) -> MagicMock:
    s = MagicMock()
    s.enabled = True
    s.vector_backend = "chroma"
    s.chroma_url = None
    s.chroma_dir = str(tmp_path / "chroma")
    s.auto_update = False
    s.data_dir = str(tmp_path / "rag_data")
    return s


@pytest.fixture()
def chroma_store(tmp_path):
    from deriva_mcp_core.rag.store import ChromaVectorStore

    return ChromaVectorStore(_chroma_settings(tmp_path))


# ---------------------------------------------------------------------------
# ChromaVectorStore -- CRUD and search with embedded ChromaDB
# ---------------------------------------------------------------------------


class TestChromaVectorStore:
    async def test_upsert_and_has_source(self, chroma_store):
        from deriva_mcp_core.rag.store import Chunk

        await chroma_store.upsert(
            [
                Chunk(
                    text="ERMrest provides a RESTful catalog API for DERIVA.",
                    source="test-source:doc.md",
                    doc_type="user-guide",
                    chunk_index=0,
                )
            ]
        )
        assert await chroma_store.has_source("test-source:doc.md")
        assert not await chroma_store.has_source("nonexistent:doc.md")

    async def test_search_returns_semantically_relevant_result(self, chroma_store):
        from deriva_mcp_core.rag.store import Chunk

        await chroma_store.upsert(
            [
                Chunk(
                    text=(
                        "ERMrest path syntax uses colons to separate schema"
                        " and table names in URL paths."
                    ),
                    source="ermrest-docs:path.md",
                    doc_type="user-guide",
                    chunk_index=0,
                ),
                Chunk(
                    text=(
                        "The Chaise interface allows users to browse and"
                        " edit data in DERIVA catalogs."
                    ),
                    source="chaise-docs:guide.md",
                    doc_type="user-guide",
                    chunk_index=0,
                ),
            ]
        )
        results = await chroma_store.search("how does ermrest path syntax work", limit=2)
        assert results
        combined = " ".join(r.text.lower() for r in results)
        assert "ermrest" in combined or "path" in combined or "schema" in combined

    async def test_delete_source_removes_chunks(self, chroma_store):
        from deriva_mcp_core.rag.store import Chunk

        source = "delete-me:doc.md"
        await chroma_store.upsert(
            [Chunk(text="Temporary.", source=source, doc_type="user-guide", chunk_index=0)]
        )
        assert await chroma_store.has_source(source)
        await chroma_store.delete_source(source)
        assert not await chroma_store.has_source(source)

    async def test_source_stats_counts_chunks_and_records_timestamp(self, chroma_store):
        from deriva_mcp_core.rag.store import Chunk

        source = "stats-test:doc.md"
        await chroma_store.upsert(
            [
                Chunk(text="Chunk one.", source=source, doc_type="user-guide", chunk_index=0),
                Chunk(text="Chunk two.", source=source, doc_type="user-guide", chunk_index=1),
            ]
        )
        stats = await chroma_store.source_stats()
        assert source in stats
        assert stats[source].chunk_count == 2
        assert stats[source].indexed_at is not None

    async def test_upsert_replaces_existing_source(self, chroma_store):
        """Upserting a source a second time must replace, not append, its chunks."""
        from deriva_mcp_core.rag.store import Chunk

        source = "replace-test:doc.md"
        await chroma_store.upsert(
            [
                Chunk(text="Old chunk 1.", source=source, doc_type="user-guide", chunk_index=0),
                Chunk(text="Old chunk 2.", source=source, doc_type="user-guide", chunk_index=1),
            ]
        )
        await chroma_store.upsert(
            [Chunk(text="New chunk.", source=source, doc_type="user-guide", chunk_index=0)]
        )
        stats = await chroma_store.source_stats()
        assert stats[source].chunk_count == 1

    async def test_search_with_doc_type_filter(self, chroma_store):
        from deriva_mcp_core.rag.store import Chunk

        await chroma_store.upsert(
            [
                Chunk(
                    text="Schema table definition for Dataset.",
                    source="schema:host:1:abc",
                    doc_type="schema",
                    chunk_index=0,
                ),
                Chunk(
                    text="User guide section about datasets.",
                    source="docs:guide.md",
                    doc_type="user-guide",
                    chunk_index=0,
                ),
            ]
        )
        results = await chroma_store.search(
            "dataset information", limit=5, where={"doc_type": "schema"}
        )
        assert results
        assert all(r.doc_type == "schema" for r in results)


# ---------------------------------------------------------------------------
# Schema indexing -- visibility class deduplication
# ---------------------------------------------------------------------------


class TestSchemaIndexing:
    async def test_index_schema_stores_chunks(self, chroma_store):
        from deriva_mcp_core.rag.schema import index_schema

        await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_A)
        stats = await chroma_store.source_stats()
        assert stats, "Expected at least one source after indexing"

    async def test_has_schema_false_before_indexing(self, chroma_store):
        from deriva_mcp_core.rag.schema import compute_schema_hash, has_schema

        h = compute_schema_hash(_SCHEMA_A)
        assert not await has_schema(chroma_store, "host.example.org", "1", h)

    async def test_has_schema_true_after_indexing(self, chroma_store):
        from deriva_mcp_core.rag.schema import compute_schema_hash, has_schema, index_schema

        h = compute_schema_hash(_SCHEMA_A)
        await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_A)
        assert await has_schema(chroma_store, "host.example.org", "1", h)

    async def test_reindexing_same_schema_replaces_not_duplicates(self, chroma_store):
        from deriva_mcp_core.rag.schema import (
            compute_schema_hash,
            index_schema,
            schema_source_name,
        )

        await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_A)
        count_first = (await chroma_store.source_stats())[
            schema_source_name("host.example.org", "1", compute_schema_hash(_SCHEMA_A))
        ].chunk_count

        await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_A)
        count_second = (await chroma_store.source_stats())[
            schema_source_name("host.example.org", "1", compute_schema_hash(_SCHEMA_A))
        ].chunk_count

        assert count_first == count_second

    async def test_different_schemas_produce_separate_sources(self, chroma_store):
        from deriva_mcp_core.rag.schema import (
            compute_schema_hash,
            index_schema,
            schema_source_name,
        )

        await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_A)
        await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_B)

        src_a = schema_source_name("host.example.org", "1", compute_schema_hash(_SCHEMA_A))
        src_b = schema_source_name("host.example.org", "1", compute_schema_hash(_SCHEMA_B))
        assert src_a != src_b

        stats = await chroma_store.source_stats()
        assert src_a in stats
        assert src_b in stats

    async def test_same_schema_different_catalogs_are_separate_sources(self, chroma_store):
        from deriva_mcp_core.rag.schema import (
            compute_schema_hash,
            index_schema,
            schema_source_name,
        )

        await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_A)
        await index_schema(chroma_store, "host.example.org", "2", _SCHEMA_A)

        h = compute_schema_hash(_SCHEMA_A)
        src1 = schema_source_name("host.example.org", "1", h)
        src2 = schema_source_name("host.example.org", "2", h)
        assert src1 != src2

        stats = await chroma_store.source_stats()
        assert src1 in stats
        assert src2 in stats


# ---------------------------------------------------------------------------
# on_catalog_connect lifecycle hook
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


class TestCatalogConnectHook:
    """Verifies that the RAG on_catalog_connect hook indexes schemas via real ChromaDB."""

    def _register_rag_with_store(self, ctx, store, tmp_path):
        """Call rag.register() patching settings and store injection."""
        from unittest.mock import patch

        mock_settings = MagicMock()
        mock_settings.enabled = True
        mock_settings.vector_backend = "chroma"
        mock_settings.auto_update = False
        mock_settings.data_dir = str(tmp_path / "rag_data")

        mock_docs_mgr = MagicMock()
        mock_docs_mgr.update = AsyncMock(return_value=0)

        with (
            patch("deriva_mcp_core.rag.config.RAGSettings", return_value=mock_settings),
            patch("deriva_mcp_core.rag.store.get_store", return_value=store),
            patch("deriva_mcp_core.rag.docs.RAGDocsManager", return_value=mock_docs_mgr),
        ):
            from deriva_mcp_core.rag import register

            register(ctx)

    async def test_hook_indexes_schema_on_first_connect(self, chroma_store, tmp_path):
        from deriva_mcp_core.plugin.api import PluginContext, _set_plugin_context
        from deriva_mcp_core.rag.schema import compute_schema_hash, has_schema

        ctx = PluginContext(_CapturingMCP())
        _set_plugin_context(ctx)
        try:
            self._register_rag_with_store(ctx, chroma_store, tmp_path)

            assert ctx._catalog_connect_hooks, "Expected hook to be registered"
            hook = ctx._catalog_connect_hooks[0]

            h = compute_schema_hash(_SCHEMA_A)
            assert not await has_schema(chroma_store, "host.example.org", "1", h)

            # Call hook directly -- same path taken by fire_catalog_connect tasks
            await hook("host.example.org", "1", h, _SCHEMA_A)

            assert await has_schema(chroma_store, "host.example.org", "1", h)
        finally:
            _set_plugin_context(None)

    async def test_hook_skips_already_indexed_schema(self, chroma_store, tmp_path):
        """Hook is idempotent: a second call for the same schema hash is a no-op."""
        from deriva_mcp_core.plugin.api import PluginContext, _set_plugin_context
        from deriva_mcp_core.rag.schema import (
            compute_schema_hash,
            index_schema,
            schema_source_name,
        )

        ctx = PluginContext(_CapturingMCP())
        _set_plugin_context(ctx)
        try:
            self._register_rag_with_store(ctx, chroma_store, tmp_path)
            hook = ctx._catalog_connect_hooks[0]

            # Pre-index so has_schema returns True
            await index_schema(chroma_store, "host.example.org", "1", _SCHEMA_A)
            h = compute_schema_hash(_SCHEMA_A)
            src = schema_source_name("host.example.org", "1", h)
            count_before = (await chroma_store.source_stats())[src].chunk_count

            await hook("host.example.org", "1", h, _SCHEMA_A)

            count_after = (await chroma_store.source_stats())[src].chunk_count
            assert count_before == count_after
        finally:
            _set_plugin_context(None)


# ---------------------------------------------------------------------------
# PgVectorStore -- requires testing.postgresql + pgvector extension
# ---------------------------------------------------------------------------

_pg_instance = None

try:
    import testing.postgresql as _tpg

    _pg_instance = _tpg.Postgresql()
except Exception:
    pass  # initdb not on PATH or PostgreSQL not installed; pg tests will skip


def _make_pg_store():
    from deriva_mcp_core.rag.store import PgVectorStore

    settings = MagicMock()
    settings.pg_dsn = _pg_instance.url()
    return PgVectorStore(settings)


@pytest.fixture()
async def pg_store():
    if _pg_instance is None:
        pytest.skip("testing.postgresql not available (PostgreSQL not found)")

    store = _make_pg_store()
    try:
        # Trigger pool creation; skip if pgvector extension is missing
        await store._ensure_pool()
    except Exception as exc:
        msg = str(exc).lower()
        if "vector" in msg or "extension" in msg or "pgvector" in msg:
            pytest.skip(f"pgvector extension not available: {exc}")
        raise
    yield store
    if store._pool:
        await store._pool.close()


class TestPgVectorStore:
    async def test_upsert_and_has_source(self, pg_store):
        from deriva_mcp_core.rag.store import Chunk

        source = "pg-test:doc.md"
        await pg_store.delete_source(source)

        await pg_store.upsert(
            [
                Chunk(
                    text="ERMrest provides a RESTful catalog API.",
                    source=source,
                    doc_type="user-guide",
                    chunk_index=0,
                )
            ]
        )
        assert await pg_store.has_source(source)
        assert not await pg_store.has_source("pg-nonexistent:doc.md")

    async def test_search_returns_results(self, pg_store):
        from deriva_mcp_core.rag.store import Chunk

        source = "pg-search-test:doc.md"
        await pg_store.delete_source(source)

        await pg_store.upsert(
            [
                Chunk(
                    text=("ERMrest path syntax uses colons to separate schema and table names."),
                    source=source,
                    doc_type="user-guide",
                    chunk_index=0,
                )
            ]
        )
        results = await pg_store.search("ermrest catalog path syntax", limit=5)
        assert results
        combined = " ".join(r.text.lower() for r in results)
        assert "ermrest" in combined or "path" in combined or "schema" in combined

    async def test_delete_source(self, pg_store):
        from deriva_mcp_core.rag.store import Chunk

        source = "pg-delete-test:doc.md"
        await pg_store.upsert(
            [Chunk(text="To be deleted.", source=source, doc_type="user-guide", chunk_index=0)]
        )
        assert await pg_store.has_source(source)
        await pg_store.delete_source(source)
        assert not await pg_store.has_source(source)

    async def test_source_stats(self, pg_store):
        from deriva_mcp_core.rag.store import Chunk

        source = "pg-stats-test:doc.md"
        await pg_store.delete_source(source)

        await pg_store.upsert(
            [
                Chunk(text="Chunk 1.", source=source, doc_type="user-guide", chunk_index=0),
                Chunk(text="Chunk 2.", source=source, doc_type="user-guide", chunk_index=1),
            ]
        )
        stats = await pg_store.source_stats()
        assert source in stats
        assert stats[source].chunk_count == 2
        assert stats[source].indexed_at is not None

    async def test_upsert_replaces_existing_source(self, pg_store):
        from deriva_mcp_core.rag.store import Chunk

        source = "pg-replace-test:doc.md"
        await pg_store.delete_source(source)

        await pg_store.upsert(
            [
                Chunk(text="Old 1.", source=source, doc_type="user-guide", chunk_index=0),
                Chunk(text="Old 2.", source=source, doc_type="user-guide", chunk_index=1),
            ]
        )
        await pg_store.upsert(
            [Chunk(text="New.", source=source, doc_type="user-guide", chunk_index=0)]
        )
        stats = await pg_store.source_stats()
        assert stats[source].chunk_count == 1

    async def test_schema_indexing_with_pgvector(self, pg_store):
        """End-to-end: index a schema via the schema module into pgvector."""
        from deriva_mcp_core.rag.schema import compute_schema_hash, has_schema, index_schema

        h = compute_schema_hash(_SCHEMA_A)
        # Clean slate
        from deriva_mcp_core.rag.schema import schema_source_name

        await pg_store.delete_source(schema_source_name("host.example.org", "1", h))

        assert not await has_schema(pg_store, "host.example.org", "1", h)
        await index_schema(pg_store, "host.example.org", "1", _SCHEMA_A)
        assert await has_schema(pg_store, "host.example.org", "1", h)
