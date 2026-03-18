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

from __future__ import annotations

# TODO (Phase 4): implement index_schema(), schema_hash(), has_schema(),
#                 schema_to_markdown()
