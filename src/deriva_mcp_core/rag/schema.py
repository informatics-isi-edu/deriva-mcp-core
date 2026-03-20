from __future__ import annotations

"""Catalog schema indexing for the RAG subsystem.

Fetches a catalog's /schema response, serializes it to structured Markdown,
chunks it, and upserts into the vector store under a visibility-class source name.

Visibility class isolation:
    A SHA-256 fingerprint of the /schema JSON response is used as a visibility
    class key. Two users whose effective ACLs produce identical /schema responses
    share one index entry. A restricted user gets their own index reflecting only
    what they can see. Source name format:
        schema:{hostname}:{catalog_id}:{schema_hash[:16]}

Schema serialization covers:
    - Schema names and comments
    - Table names, types (vocabulary/asset/association), and comments
    - Columns: name, type, nullability, comment
    - Foreign key relationships (source columns -> target table)
    - Vocabulary terms: name, synonyms, URI (for vocabulary-typed tables)

Key functions:
    index_schema(hostname, catalog_id, schema_json)  -- serialize, chunk, upsert
    schema_hash(schema_json) -> str                  -- visibility class fingerprint
    has_schema(hostname, catalog_id, schema_hash) -> bool  -- skip if unchanged
"""

import hashlib
import json
from typing import TYPE_CHECKING

from .chunker import chunk_markdown

if TYPE_CHECKING:
    from .store import VectorStore

# Source name prefix for all schema visibility classes
_SOURCE_PREFIX = "schema"


def compute_schema_hash(schema_json: dict) -> str:
    """Return a SHA-256 hex digest of the canonical schema JSON.

    Args:
        schema_json: Full /schema response dict.

    Returns:
        64-character hex string (SHA-256).
    """
    canonical = json.dumps(schema_json, sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def schema_source_name(hostname: str, catalog_id: str, schema_hash: str) -> str:
    """Build the source name for a schema visibility class."""
    return f"{_SOURCE_PREFIX}:{hostname}:{catalog_id}:{schema_hash[:16]}"


def schema_to_markdown(hostname: str, catalog_id: str, schema_json: dict) -> str:
    """Serialize a catalog /schema response to structured Markdown.

    Covers: schema names/comments, table names/types/comments, columns
    (name, type, nullability, comment), and foreign key relationships.

    Args:
        hostname: DERIVA server hostname.
        catalog_id: Catalog ID or alias.
        schema_json: Full /schema response dict.

    Returns:
        Markdown string suitable for chunking and indexing.
    """
    lines: list[str] = [f"# Catalog: {hostname} / {catalog_id}", ""]

    for schema_name, schema_doc in schema_json.get("schemas", {}).items():
        if schema_name.startswith("_"):
            continue  # skip system schemas (_ermrest, _acl_admin)
        comment = schema_doc.get("comment") or ""
        lines.append(f"## Schema: {schema_name}")
        if comment:
            lines.append(f"{comment}")
        lines.append("")

        for table_name, table_doc in schema_doc.get("tables", {}).items():
            kind = table_doc.get("kind", "table")
            t_comment = table_doc.get("comment") or ""
            lines.append(f"### Table: {schema_name}:{table_name} ({kind})")
            if t_comment:
                lines.append(f"{t_comment}")
            lines.append("")

            cols = table_doc.get("column_definitions", [])
            if cols:
                lines.append("**Columns:**")
                for col in cols:
                    col_name = col.get("name", "")
                    typename = col.get("type", {}).get("typename", "unknown")
                    nullok = col.get("nullok", True)
                    nullable = "" if nullok else " NOT NULL"
                    col_comment = col.get("comment") or ""
                    col_line = f"- `{col_name}` ({typename}{nullable})"
                    if col_comment:
                        col_line += f" -- {col_comment}"
                    lines.append(col_line)
                lines.append("")

            fks = table_doc.get("foreign_keys", [])
            if fks:
                lines.append("**Foreign keys:**")
                for fk in fks:
                    fk_cols = [c["column_name"] for c in fk.get("foreign_key_columns", [])]
                    ref_cols = fk.get("referenced_columns", [])
                    if ref_cols:
                        ref_schema = ref_cols[0].get("schema_name", "")
                        ref_table = ref_cols[0].get("table_name", "")
                        lines.append(
                            f"- {', '.join(fk_cols)} -> {ref_schema}:{ref_table}"
                        )
                lines.append("")

    return "\n".join(lines)


async def index_schema(
    store: "VectorStore",
    hostname: str,
    catalog_id: str,
    schema_json: dict,
) -> None:
    """Serialize schema to Markdown, chunk, and upsert into the vector store.

    Uses a visibility-class source name derived from the schema hash so that
    users with different effective ACLs get separate index entries.

    Args:
        store: VectorStore to upsert into.
        hostname: DERIVA server hostname.
        catalog_id: Catalog ID or alias.
        schema_json: Full /schema response dict.
    """
    h = compute_schema_hash(schema_json)
    source = schema_source_name(hostname, catalog_id, h)
    md = schema_to_markdown(hostname, catalog_id, schema_json)
    chunks = chunk_markdown(md, source=source, doc_type="schema")
    if chunks:
        await store.upsert(chunks)


async def has_schema(
    store: "VectorStore",
    hostname: str,
    catalog_id: str,
    schema_hash: str,
) -> bool:
    """Return True if the schema visibility class is already indexed.

    Args:
        store: VectorStore to query.
        hostname: DERIVA server hostname.
        catalog_id: Catalog ID or alias.
        schema_hash: Full SHA-256 hash from compute_schema_hash().
    """
    source = schema_source_name(hostname, catalog_id, schema_hash)
    return await store.has_source(source)
