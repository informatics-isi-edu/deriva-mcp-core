from __future__ import annotations

"""Generic per-user data indexing primitives for the RAG subsystem.

Provides a generic pipeline for indexing catalog table rows into the vector
store, keyed by user identity. Plugins (e.g., deriva-ml) extend the serialization
by implementing the RowSerializer protocol and calling index_table_data() directly.

Source naming: data:{hostname}:{catalog_id}:{user_id}

DERIVA system columns excluded from generic serialization:
    RID, RCT, RMT, RCB, RMB

Public API for plugin authors (import from deriva_mcp_core.rag.data):
    RowSerializer       -- Protocol for custom row-to-Markdown serialization
    index_table_data()  -- Core indexing primitive; callable from lifecycle hooks
    data_source_name()  -- Source name helper (consistent naming across plugins)
"""


import time
from typing import TYPE_CHECKING, Any
from .chunker import chunk_markdown
from .store import Chunk

if TYPE_CHECKING:
    from .store import VectorStore

# System columns excluded from generic row rendering
_SYSTEM_COLS: frozenset[str] = frozenset({"RID", "RCT", "RMT", "RCB", "RMB"})


class RowSerializer:
    """Protocol for custom table-row-to-Markdown serialization.

    Plugins implement this to provide richer rendering for domain-specific
    tables. Return None for any row the plugin does not handle; the generic
    serializer is used as a fallback.

    Example::

        class MySerializer:
            def serialize(self, table_name: str, row: dict) -> str | None:
                if table_name == "Dataset":
                    return _rich_dataset_markdown(row)
                return None
    """

    def serialize(self, table_name: str, row: dict) -> str | None:  # noqa: ARG002
        """Serialize a single row to Markdown, or return None for generic rendering."""
        return None


def data_source_name(hostname: str, catalog_id: str, user_id: str) -> str:
    """Build the canonical source name for per-user data indexes.

    Args:
        hostname: DERIVA server hostname.
        catalog_id: Catalog ID or alias.
        user_id: Subject identifier (sub) for the requesting user.

    Returns:
        Source name string used in vector store metadata.
    """
    return f"data:{hostname}:{catalog_id}:{user_id}"


def _generic_row_markdown(table_name: str, row: dict) -> str:
    """Render a single row as Markdown using generic column-value formatting."""
    rid = row.get("RID", "")
    header = f"## {table_name}: {rid}"
    lines = [header, ""]
    for col, val in row.items():
        if col in _SYSTEM_COLS or val is None:
            continue
        lines.append(f"**{col}:** {val}")
    return "\n".join(lines)


async def index_table_data(
    store: VectorStore,
    hostname: str,
    catalog_id: str,
    table_name: str,
    rows: list[dict[str, Any]],
    user_id: str,
    serializer: RowSerializer | None = None,
    ttl_seconds: int = 3600,
) -> None:
    """Serialize rows to Markdown chunks and upsert into the vector store.

    Applies per-source staleness detection: if the source was indexed within
    ttl_seconds and the row count has not changed, the upsert is skipped.

    Args:
        store: VectorStore to upsert into.
        hostname: DERIVA server hostname.
        catalog_id: Catalog ID or alias.
        table_name: Table being indexed.
        rows: List of row dicts from ERMRest.
        user_id: Subject identifier for the requesting user (scopes the index).
        serializer: Optional custom serializer; falls back to generic rendering.
        ttl_seconds: Skip reindex if source was indexed within this many seconds.
    """
    if not rows:
        return

    source = data_source_name(hostname, catalog_id, user_id)

    # Staleness check: if source exists and was indexed recently, skip
    if await store.has_source(source):
        stats = await store.source_stats()
        entry = stats.get(source)
        if entry and entry.indexed_at:
            try:
                import datetime

                ts = datetime.datetime.fromisoformat(entry.indexed_at)
                age = time.time() - ts.timestamp()
                if age < ttl_seconds and entry.chunk_count > 0:
                    return
            except Exception:
                pass  # malformed timestamp -- proceed with reindex

    docs: list[str] = []
    for row in rows:
        if serializer is not None:
            rendered = serializer.serialize(table_name, row)
        else:
            rendered = None
        if rendered is None:
            rendered = _generic_row_markdown(table_name, row)
        docs.append(rendered)

    chunks: list[Chunk] = []
    for doc in docs:
        row_chunks = chunk_markdown(
            doc,
            source=source,
            doc_type="data",
        )
        for c in row_chunks:
            chunks.append(
                Chunk(
                    text=c.text,
                    source=source,
                    doc_type="data",
                    section_heading=c.section_heading,
                    heading_hierarchy=c.heading_hierarchy,
                    chunk_index=len(chunks),
                )
            )

    if chunks:
        await store.upsert(chunks)
