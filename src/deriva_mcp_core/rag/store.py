from __future__ import annotations

"""Vector store abstraction for the RAG subsystem.

VectorStore is a Protocol. Tool and RAG module code depends only on this
interface, never on a concrete backend. Two implementations are provided:

    ChromaVectorStore  -- embedded ChromaDB (default); zero additional services;
                          also supports ChromaDB server mode via chroma_url
    PgVectorStore      -- PostgreSQL with pgvector extension; recommended for
                          production multi-instance deployments

Backend is selected by RAGSettings.vector_backend and constructed once at
server startup. Plugin authors may supply their own VectorStore implementation
by passing it to PluginContext (see plugin authoring guide).
"""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import RAGSettings

logger = logging.getLogger(__name__)

# Embedding dimension for the default all-MiniLM-L6-v2 model
_EMBEDDING_DIM = 384


@dataclass
class Chunk:
    """A document fragment ready for vector store upsert."""

    text: str
    source: str
    doc_type: str
    section_heading: str = ""
    heading_hierarchy: list[str] = field(default_factory=list)
    chunk_index: int = 0
    url: str = ""  # optional record URL (e.g. Chaise link) for this chunk
    title: str = ""  # optional human-readable title for this chunk's record


@dataclass
class SearchResult:
    """A single result from a vector store similarity search."""

    text: str
    source: str
    doc_type: str
    score: float
    metadata: dict


@dataclass
class SourceStats:
    """Per-source statistics for rag_status."""

    chunk_count: int
    indexed_at: str | None  # ISO-8601 timestamp of most recent upsert, or None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _chunk_id(chunk: Chunk) -> str:
    return f"{chunk.source}:{chunk.chunk_index}"


def _chunk_metadata(chunk: Chunk, indexed_at: str) -> dict[str, Any]:
    return {
        "source": chunk.source,
        "doc_type": chunk.doc_type,
        "section_heading": chunk.section_heading,
        "heading_hierarchy": json.dumps(chunk.heading_hierarchy),
        "chunk_index": chunk.chunk_index,
        "indexed_at": indexed_at,
        "url": chunk.url,
        "title": chunk.title,
    }


def _to_chroma_where(where: dict | None) -> dict | None:
    """Translate a simple key=value dict to a Chroma where clause.

    Chroma requires explicit operator syntax: {"field": {"$eq": value}}.
    The shorthand {"field": value} is not valid in current Chroma versions.
    """
    if not where:
        return None
    items = list(where.items())
    if len(items) == 1:
        k, v = items[0]
        return {k: {"$eq": v}}
    return {"$and": [{k: {"$eq": v}} for k, v in items]}


# ---------------------------------------------------------------------------
# VectorStore protocol
# ---------------------------------------------------------------------------


class VectorStore:
    """Protocol for vector store backends.

    Both ChromaVectorStore and PgVectorStore implement this interface.
    Tool code and the RAG manager depend only on these methods.
    """

    async def upsert(self, chunks: list[Chunk]) -> None:
        """Replace all existing chunks for each source present in chunks,
        then insert the new chunks. Chunks for the same source are replaced
        atomically (delete-then-add)."""
        raise NotImplementedError

    async def add(self, chunks: list[Chunk]) -> None:
        """Append chunks without deleting existing data for the source.

        Use this when the caller manages deletion separately (e.g. delete_source
        once upfront, then add in batches). Unlike upsert, no delete is performed.
        """
        raise NotImplementedError

    async def search(
        self,
        query: str,
        limit: int = 10,
        where: dict | None = None,
    ) -> list[SearchResult]:
        """Semantic search. where is a simple {key: value} equality filter."""
        raise NotImplementedError

    async def delete_source(self, source: str) -> None:
        """Delete all chunks for the given source."""
        raise NotImplementedError

    async def has_source(self, source: str) -> bool:
        """Return True if at least one chunk exists for the given source."""
        raise NotImplementedError

    async def source_stats(self) -> dict[str, SourceStats]:
        """Return per-source statistics keyed by source name."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ChromaVectorStore
# ---------------------------------------------------------------------------


class ChromaVectorStore(VectorStore):
    """VectorStore backed by ChromaDB (embedded or HTTP server)."""

    _COLLECTION = "deriva_rag"

    def __init__(self, settings: RAGSettings) -> None:
        self._settings = settings
        self._client: Any = None
        self._collection: Any = None
        self._init_lock = threading.Lock()

    def _ensure_client(self) -> Any:
        # IMPORTANT: self._client is assigned LAST (after self._collection) so
        # that the guard below is only True when initialization is fully complete.
        # Assigning _client earlier would allow a concurrent thread to slip through
        # the guard and return self._collection while it is still None.
        if self._client is not None:
            return self._collection
        with self._init_lock:
            # Double-checked locking: re-test after acquiring to avoid a race
            # where multiple asyncio.to_thread workers all see _client=None
            # simultaneously and each try to create the client + download the EF.
            if self._client is not None:
                return self._collection
            import os

            import chromadb

            if self._settings.chroma_url:  # pragma: no cover
                from urllib.parse import urlparse

                parsed = urlparse(self._settings.chroma_url)
                _client = chromadb.HttpClient(
                    host=parsed.hostname or "localhost",
                    port=parsed.port or 8000,
                )
            else:
                path = os.path.expanduser(self._settings.chroma_dir)
                try:
                    _client = chromadb.PersistentClient(path=path)
                except (ValueError, AttributeError, KeyError):
                    # Storage format incompatible with current ChromaDB version
                    # (e.g. RustBindingsAPI migration). Wipe and recreate --
                    # RAG data is re-crawlable so this is safe.
                    import shutil

                    logger.warning(
                        "ChromaDB storage at %s is corrupt or incompatible; "
                        "removing and reinitializing",
                        path,
                    )
                    shutil.rmtree(path, ignore_errors=True)
                    os.makedirs(path, exist_ok=True)
                    # Clear ChromaDB's in-process SharedSystemClient cache so
                    # the retry does not hit a stale entry for this path.
                    try:
                        from chromadb.api.shared_system_client import SharedSystemClient
                        SharedSystemClient._identifier_to_system.pop(path, None)
                    except Exception:
                        pass
                    _client = chromadb.PersistentClient(path=path)

            # Build an EF with a custom download path so the ONNX model lands
            # on the persistent volume instead of the container-ephemeral ~/.cache.
            # DOWNLOAD_PATH is a class attribute; overriding on the instance takes
            # precedence in _download_model_if_not_exists (uses self.DOWNLOAD_PATH).
            from pathlib import Path

            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

            ef = ONNXMiniLM_L6_V2()
            ef.DOWNLOAD_PATH = (  # type: ignore[attr-defined]
                Path(os.path.expanduser(self._settings.chroma_cache_dir))
                / "onnx_models"
                / ef.MODEL_NAME
            )

            # Assign collection first, then client.  The outer guard checks
            # self._client, so once _client is set the fast path returns
            # self._collection -- which must already be fully initialized.
            self._collection = _client.get_or_create_collection(
                name=self._COLLECTION,
                metadata={"hnsw:space": "cosine"},
                embedding_function=ef,
            )
            # Pre-warm: download the model now, under the lock, so that
            # concurrent workers blocked here find it already cached on disk
            # when they are eventually released.
            ef._download_model_if_not_exists()
            self._client = _client  # set last -- signals initialization complete
            return self._collection

    async def upsert(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        collection = await asyncio.to_thread(self._ensure_client)
        indexed_at = _now_iso()

        # Group by source and replace each source atomically
        sources = {c.source for c in chunks}
        for source in sources:
            source_chunks = [c for c in chunks if c.source == source]
            ids = [_chunk_id(c) for c in source_chunks]
            documents = [c.text for c in source_chunks]
            metadatas = [_chunk_metadata(c, indexed_at) for c in source_chunks]

            def _do_upsert(col: Any, src: str, i: list, d: list, m: list) -> None:
                try:
                    col.delete(where={"source": src})
                except Exception:
                    pass  # source may not exist yet
                col.add(ids=i, documents=d, metadatas=m)

            await asyncio.to_thread(_do_upsert, collection, source, ids, documents, metadatas)

    async def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        collection = await asyncio.to_thread(self._ensure_client)
        indexed_at = _now_iso()
        sources = {c.source for c in chunks}
        for source in sources:
            source_chunks = [c for c in chunks if c.source == source]
            ids = [_chunk_id(c) for c in source_chunks]
            documents = [c.text for c in source_chunks]
            metadatas = [_chunk_metadata(c, indexed_at) for c in source_chunks]
            await asyncio.to_thread(collection.add, ids=ids, documents=documents, metadatas=metadatas)

    async def search(
        self,
        query: str,
        limit: int = 10,
        where: dict | None = None,
    ) -> list[SearchResult]:
        collection = await asyncio.to_thread(self._ensure_client)
        chroma_where = _to_chroma_where(where)

        def _do_query() -> Any:
            kwargs: dict[str, Any] = {
                "query_texts": [query],
                "n_results": limit,
                "include": ["documents", "metadatas", "distances"],
            }
            if chroma_where:
                kwargs["where"] = chroma_where
            return collection.query(**kwargs)

        result = await asyncio.to_thread(_do_query)
        results: list[SearchResult] = []
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            results.append(
                SearchResult(
                    text=doc,
                    source=meta.get("source", ""),
                    doc_type=meta.get("doc_type", ""),
                    score=float(1.0 - dist),  # cosine distance -> similarity
                    metadata=meta,
                )
            )
        return results

    async def delete_source(self, source: str) -> None:
        collection = await asyncio.to_thread(self._ensure_client)

        def _do_delete() -> None:
            try:
                collection.delete(where={"source": source})
            except Exception:
                pass

        await asyncio.to_thread(_do_delete)

    async def has_source(self, source: str) -> bool:
        collection = await asyncio.to_thread(self._ensure_client)

        def _do_get() -> list:
            result = collection.get(where={"source": source}, limit=1, include=[])
            return result.get("ids", [])

        ids = await asyncio.to_thread(_do_get)
        return len(ids) > 0

    async def source_stats(self) -> dict[str, SourceStats]:
        collection = await asyncio.to_thread(self._ensure_client)

        def _do_get_all() -> Any:
            return collection.get(include=["metadatas"])

        result = await asyncio.to_thread(_do_get_all)
        stats: dict[str, dict] = {}  # source -> {count, max_indexed_at}
        for meta in result.get("metadatas", []):
            src = meta.get("source", "")
            ts = meta.get("indexed_at")
            if src not in stats:
                stats[src] = {"count": 0, "indexed_at": None}
            stats[src]["count"] += 1
            if ts and (stats[src]["indexed_at"] is None or ts > stats[src]["indexed_at"]):
                stats[src]["indexed_at"] = ts
        return {
            src: SourceStats(chunk_count=v["count"], indexed_at=v["indexed_at"])
            for src, v in stats.items()
        }


# ---------------------------------------------------------------------------
# PgVectorStore
# ---------------------------------------------------------------------------


_CREATE_EXTENSION = "CREATE EXTENSION IF NOT EXISTS vector;"
_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS deriva_rag_chunks (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    doc_type    TEXT NOT NULL,
    text        TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{{}}',
    embedding   vector({_EMBEDDING_DIM}) NOT NULL,
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS deriva_rag_source_idx
    ON deriva_rag_chunks(source);
CREATE INDEX IF NOT EXISTS deriva_rag_embedding_idx
    ON deriva_rag_chunks USING hnsw (embedding vector_cosine_ops);
"""


class PgVectorStore(VectorStore):  # pragma: no cover
    """VectorStore backed by PostgreSQL with the pgvector extension."""

    def __init__(self, settings: RAGSettings) -> None:
        self._settings = settings
        self._pool: Any = None
        self._ef: Any = None  # embedding function, lazy-loaded

    def _get_ef(self) -> Any:
        if self._ef is None:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            self._ef = DefaultEmbeddingFunction()
        return self._ef

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        import asyncpg

        self._pool = await asyncpg.create_pool(self._settings.pg_dsn)
        async with self._pool.acquire() as conn:
            await conn.execute(_CREATE_EXTENSION)
            await conn.execute(_CREATE_TABLE)
            await conn.execute(_CREATE_INDEXES)
        return self._pool

    async def upsert(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        pool = await self._ensure_pool()
        ef = self._get_ef()
        indexed_at = _now_iso()

        sources = {c.source for c in chunks}
        for source in sources:
            source_chunks = [c for c in chunks if c.source == source]
            texts = [c.text for c in source_chunks]
            embeddings = await asyncio.to_thread(ef, texts)

            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM deriva_rag_chunks WHERE source = $1", source)
                await conn.executemany(
                    """
                    INSERT INTO deriva_rag_chunks
                        (id, source, doc_type, text, metadata, embedding, indexed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    [
                        (
                            _chunk_id(c),
                            c.source,
                            c.doc_type,
                            c.text,
                            json.dumps(_chunk_metadata(c, indexed_at)),
                            list(map(float, embeddings[i])),
                            indexed_at,
                        )
                        for i, c in enumerate(source_chunks)
                    ],
                )

    async def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        pool = await self._ensure_pool()
        ef = self._get_ef()
        indexed_at = _now_iso()
        sources = {c.source for c in chunks}
        for source in sources:
            source_chunks = [c for c in chunks if c.source == source]
            texts = [c.text for c in source_chunks]
            embeddings = await asyncio.to_thread(ef, texts)
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO deriva_rag_chunks
                        (id, source, doc_type, text, metadata, embedding, indexed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (id) DO UPDATE SET
                        text = EXCLUDED.text,
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        indexed_at = EXCLUDED.indexed_at
                    """,
                    [
                        (
                            _chunk_id(c),
                            c.source,
                            c.doc_type,
                            c.text,
                            json.dumps(_chunk_metadata(c, indexed_at)),
                            list(map(float, embeddings[i])),
                            indexed_at,
                        )
                        for i, c in enumerate(source_chunks)
                    ],
                )

    async def search(
        self,
        query: str,
        limit: int = 10,
        where: dict | None = None,
    ) -> list[SearchResult]:
        pool = await self._ensure_pool()
        ef = self._get_ef()
        q_vec = (await asyncio.to_thread(ef, [query]))[0]

        clauses: list[str] = ["1=1"]
        params: list = [list(map(float, q_vec))]
        if where:
            for key, val in where.items():
                params.append(val)
                # key is always a literal from our own code -- safe to inline
                clauses.append(f"(metadata->>'{key}') = ${len(params)}")
        params.append(limit)

        sql = f"""
            SELECT text, source, doc_type, metadata,
                   1 - (embedding <=> $1::vector) AS score
            FROM deriva_rag_chunks
            WHERE {" AND ".join(clauses)}
            ORDER BY embedding <=> $1::vector
            LIMIT ${len(params)}
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [
            SearchResult(
                text=r["text"],
                source=r["source"],
                doc_type=r["doc_type"],
                score=float(r["score"]),
                metadata=json.loads(r["metadata"]),
            )
            for r in rows
        ]

    async def delete_source(self, source: str) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM deriva_rag_chunks WHERE source = $1", source)

    async def has_source(self, source: str) -> bool:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM deriva_rag_chunks WHERE source = $1 LIMIT 1", source
            )
        return row is not None

    async def source_stats(self) -> dict[str, SourceStats]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source,
                       COUNT(*) AS chunk_count,
                       MAX(indexed_at) AS indexed_at
                FROM deriva_rag_chunks
                GROUP BY source
                """
            )
        return {
            r["source"]: SourceStats(
                chunk_count=r["chunk_count"],
                indexed_at=r["indexed_at"].isoformat() if r["indexed_at"] else None,
            )
            for r in rows
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_store(settings: RAGSettings) -> VectorStore:
    """Construct the configured VectorStore implementation."""
    if settings.vector_backend == "pgvector":
        return PgVectorStore(settings)
    return ChromaVectorStore(settings)
