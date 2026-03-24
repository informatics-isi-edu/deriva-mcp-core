from __future__ import annotations

"""Unit tests for rag/schema.py -- schema serialization and indexing."""

from deriva_mcp_core.rag.schema import (
    compute_schema_hash,
    has_schema,
    index_schema,
    schema_source_name,
    schema_to_markdown,
)

# ---------------------------------------------------------------------------
# Minimal schema fixture
# ---------------------------------------------------------------------------

_SCHEMA_JSON = {
    "schemas": {
        "public": {
            "comment": "Public schema",
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
                    "keys": [],
                    "foreign_keys": [
                        {
                            "foreign_key_columns": [{"column_name": "Species"}],
                            "referenced_columns": [
                                {
                                    "schema_name": "vocab",
                                    "table_name": "Species",
                                    "column_name": "RID",
                                }
                            ],
                        }
                    ],
                }
            },
        },
        "_ermrest": {
            "comment": "System schema",
            "tables": {},
        },
    }
}


class TestComputeSchemaHash:
    def test_deterministic(self):
        h1 = compute_schema_hash(_SCHEMA_JSON)
        h2 = compute_schema_hash(_SCHEMA_JSON)
        assert h1 == h2

    def test_different_schemas_different_hashes(self):
        modified = {"schemas": {"public": {"tables": {}}}}
        assert compute_schema_hash(_SCHEMA_JSON) != compute_schema_hash(modified)

    def test_hash_length(self):
        h = compute_schema_hash(_SCHEMA_JSON)
        assert len(h) == 64  # SHA-256 hex


class TestSchemaSourceName:
    def test_format(self):
        h = "a" * 64
        name = schema_source_name("host.example.org", "1", h)
        assert name == "schema:host.example.org:1:" + "a" * 16

    def test_truncates_hash(self):
        h = "abcdef1234567890" * 4  # 64 chars
        name = schema_source_name("h", "1", h)
        # Only first 16 chars of hash
        assert name.endswith(h[:16])


class TestSchemaToMarkdown:
    def setup_method(self):
        self.md = schema_to_markdown("host.example.org", "1", _SCHEMA_JSON)

    def test_contains_hostname_and_catalog(self):
        assert "host.example.org" in self.md
        assert "/ 1" in self.md

    def test_system_schema_excluded(self):
        assert "_ermrest" not in self.md

    def test_public_schema_included(self):
        assert "Schema: public" in self.md
        assert "Public schema" in self.md

    def test_table_included(self):
        assert "Dataset" in self.md
        assert "Research datasets" in self.md

    def test_column_listed(self):
        assert "Title" in self.md
        assert "text" in self.md

    def test_foreign_key_listed(self):
        assert "Species" in self.md
        assert "vocab:Species" in self.md

    def test_not_null_shown(self):
        assert "NOT NULL" in self.md


# ---------------------------------------------------------------------------
# Async tests with mock store
# ---------------------------------------------------------------------------


class _MockStore:
    """Minimal in-memory VectorStore for testing."""

    def __init__(self):
        self.upserted: list = []
        self.sources: set = set()

    async def upsert(self, chunks):
        self.upserted.extend(chunks)
        for c in chunks:
            self.sources.add(c.source)

    async def has_source(self, source):
        return source in self.sources


class TestIndexSchema:
    async def test_upserts_chunks(self):
        store = _MockStore()
        await index_schema(store, "host.example.org", "1", _SCHEMA_JSON)
        assert store.upserted, "Expected chunks to be upserted"

    async def test_chunks_have_correct_doc_type(self):
        store = _MockStore()
        await index_schema(store, "host.example.org", "1", _SCHEMA_JSON)
        assert all(c.doc_type == "schema" for c in store.upserted)

    async def test_source_name_matches_hash(self):
        store = _MockStore()
        h = compute_schema_hash(_SCHEMA_JSON)
        expected_source = schema_source_name("host.example.org", "1", h)
        await index_schema(store, "host.example.org", "1", _SCHEMA_JSON)
        sources = {c.source for c in store.upserted}
        assert expected_source in sources


class TestHasSchema:
    async def test_returns_false_when_not_indexed(self):
        store = _MockStore()
        h = compute_schema_hash(_SCHEMA_JSON)
        result = await has_schema(store, "host.example.org", "1", h)
        assert result is False

    async def test_returns_true_after_indexing(self):
        store = _MockStore()
        h = compute_schema_hash(_SCHEMA_JSON)
        await index_schema(store, "host.example.org", "1", _SCHEMA_JSON)
        result = await has_schema(store, "host.example.org", "1", h)
        assert result is True
