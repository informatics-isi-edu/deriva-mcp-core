from __future__ import annotations

"""MCP tool registration for the RAG subsystem.

register(ctx) is called from rag/__init__.py during server startup.
get_rag_store() exposes the active VectorStore to other tool modules
(e.g. entity.py uses it for RAG suggestions).
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from ..context import get_catalog, get_request_user_id

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

# Module-level reference to the active VectorStore. Set by register() when
# DERIVA_MCP_RAG_ENABLED=true. None when RAG is disabled or not yet started.
_rag_store: Any | None = None


def get_rag_store() -> Any | None:
    """Return the active VectorStore, or None if RAG is disabled."""
    return _rag_store


def register(ctx: PluginContext, env_file: str | None = None) -> None:
    """Register the RAG subsystem as a built-in plugin.

    When DERIVA_MCP_RAG_ENABLED is false (the default), this function returns
    immediately without registering any tools or hooks. This makes the RAG
    subsystem entirely opt-in -- deployments that do not need semantic search
    incur no overhead.

    Args:
        ctx: Plugin context to register tools and hooks against.
        env_file: Path to the env file resolved at server startup. Forwarded
            to RAGSettings so RAG variables in deriva-mcp.env are picked up.
    """
    from .config import RAGSettings

    settings = RAGSettings(_env_file=env_file)
    logger.info(
        "RAG subsystem: enabled=%s, vector_backend=%s, env_file=%s",
        settings.enabled,
        settings.vector_backend,
        env_file,
    )
    if not settings.enabled:
        return

    import urllib.parse

    from .data import index_table_data
    from .docs import BUILTIN_SOURCES, DocSource, RAGDocsManager
    from .schema import compute_schema_hash, has_schema, index_schema, schema_source_name
    from .store import get_store

    store = get_store(settings)
    docs_manager = RAGDocsManager(store, settings)

    global _rag_store
    _rag_store = store

    # Collect all documentation sources: built-ins + plugin-declared + runtime-added
    all_sources: list[DocSource] = list(BUILTIN_SOURCES)
    for decl in ctx._rag_sources:
        all_sources.append(
            DocSource(
                name=decl.name,
                owner=decl.repo_owner,
                repo=decl.repo_name,
                branch=decl.branch,
                path_prefix=decl.path_prefix,
                doc_type=decl.doc_type,
            )
        )

    # Load runtime-added sources (persisted across restarts via sources.json).
    # Plugin-declared sources take precedence on name conflict.
    _static_names = {s.name for s in all_sources}
    for runtime_src in docs_manager.load_runtime_sources():
        if runtime_src.name not in _static_names:
            all_sources.append(runtime_src)

    # Index sources not yet in the vector store at startup
    if settings.auto_update:
        import asyncio

        async def _startup_update() -> None:
            logger.info(
                "RAG startup crawl: updating %d source(s): %s",
                len(all_sources),
                ", ".join(s.name for s in all_sources),
            )
            failed = 0
            for src in all_sources:
                try:
                    await docs_manager.update(src)
                except Exception:
                    failed += 1
                    logger.warning("Startup doc update failed for %r", src.name, exc_info=True)
            logger.info(
                "RAG startup crawl complete: %d source(s) processed, %d failed",
                len(all_sources),
                failed,
            )

        try:
            asyncio.get_running_loop().create_task(_startup_update())
        except RuntimeError:
            pass  # no running loop -- startup crawl skipped (e.g., during tests)

    # ------------------------------------------------------------------
    # on_catalog_connect hook -- auto-index schema on first access
    # ------------------------------------------------------------------

    async def _handle_catalog_connect(
        hostname: str,
        catalog_id: str,
        schema_hash: str,
        schema_json: dict,
    ) -> None:
        try:
            if not await has_schema(store, hostname, catalog_id, schema_hash):
                await index_schema(store, hostname, catalog_id, schema_json)
        except Exception:
            logger.warning(
                "Schema auto-index failed for %s/%s", hostname, catalog_id, exc_info=True
            )

    ctx.on_catalog_connect(_handle_catalog_connect)

    # ------------------------------------------------------------------
    # MCP tools
    # ------------------------------------------------------------------

    @ctx.tool(mutates=False)
    async def rag_search(
        query: str,
        limit: int = 10,
        hostname: str | None = None,
        catalog_id: str | None = None,
        doc_type: str | None = None,
    ) -> str:
        """Semantic search across DERIVA documentation and catalog schemas.

        Searches indexed documentation (deriva-py, ermrest, chaise) and catalog
        schemas using vector similarity. When hostname and catalog_id are provided,
        schema results are scoped to the visibility class for that catalog.

        Args:
            query: Natural language search query.
            limit: Maximum number of results to return (default 10).
            hostname: Restrict schema search to this DERIVA server.
            catalog_id: Restrict schema search to this catalog.
            doc_type: Optional filter: "user-guide", "schema", or "data".
        """
        try:
            where: dict = {}
            if doc_type:
                where["doc_type"] = doc_type
            results = await store.search(query, limit=limit, where=where if where else None)
            if hostname and catalog_id:
                # Exclude schema chunks that belong to a different catalog.
                # Non-schema sources (user-guide, data) are always included.
                schema_prefix = schema_source_name(hostname, catalog_id, "")
                results = [
                    r for r in results
                    if not r.source.startswith("schema:") or r.source.startswith(schema_prefix)
                ]
            return json.dumps(
                [
                    {
                        "text": r.text,
                        "source": r.source,
                        "doc_type": r.doc_type,
                        "score": round(r.score, 4),
                    }
                    for r in results
                ]
            )
        except Exception as exc:
            logger.error("rag_search failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_update_docs(source_name: str | None = None) -> str:
        """Incrementally update indexed documentation (SHA delta).

        Crawls GitHub repositories and re-indexes only files whose SHA has
        changed since the last crawl. Safe to run frequently.

        Args:
            source_name: Specific source to update (e.g., "deriva-py-docs").
                If omitted, all sources are updated.
        """
        try:
            targets = (
                [s for s in all_sources if s.name == source_name] if source_name else all_sources
            )
            if source_name and not targets:
                return json.dumps({"error": f"Unknown source: {source_name!r}"})
            counts = {}
            for src in targets:
                counts[src.name] = await docs_manager.update(src)
            return json.dumps({"updated": counts})
        except Exception as exc:
            logger.error("rag_update_docs failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_index_schema(hostname: str, catalog_id: str) -> str:
        """Manually trigger schema reindexing for a catalog.

        Fetches the current /schema from the DERIVA server, serializes it to
        Markdown, and upserts into the vector store. Useful after schema
        changes that were not picked up automatically.

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
        """
        try:
            catalog = get_catalog(hostname, catalog_id)
            schema_json = catalog.get("/schema").json()
            schema_hash = compute_schema_hash(schema_json)
            await index_schema(store, hostname, catalog_id, schema_json)
            return json.dumps(
                {
                    "status": "indexed",
                    "hostname": hostname,
                    "catalog_id": catalog_id,
                    "schema_hash": schema_hash[:16],
                }
            )
        except Exception as exc:
            logger.error("rag_index_schema failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_index_table(
        hostname: str,
        catalog_id: str,
        schema: str,
        table: str,
    ) -> str:
        """Fetch all rows from a table and index them for semantic search.

        Retrieves rows via ERMRest and indexes them using the generic row
        serializer, scoped to the calling user's identity. Plugins with a
        custom serializer call index_table_data() directly rather than using
        this tool.

        Staleness check: if the source was indexed within the configured TTL
        the upsert is skipped and "status" is "fresh".

        Args:
            hostname: Hostname of the DERIVA server.
            catalog_id: Catalog ID or alias.
            schema: Schema name.
            table: Table name.
        """
        try:
            enc = lambda v: urllib.parse.quote(str(v), safe="")  # noqa: E731
            catalog = get_catalog(hostname, catalog_id)
            url = f"/entity/{enc(schema)}:{enc(table)}?limit=1000"
            rows = catalog.get(url).json()
            user_id = get_request_user_id()
            await index_table_data(store, hostname, catalog_id, table, rows, user_id)
            return json.dumps(
                {
                    "status": "indexed",
                    "hostname": hostname,
                    "catalog_id": catalog_id,
                    "schema": schema,
                    "table": table,
                    "row_count": len(rows),
                }
            )
        except Exception as exc:
            logger.error("rag_index_table failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_status() -> str:
        """Return RAG subsystem status: per-source chunk counts and timestamps.

        Returns a JSON object with a "sources" dict keyed by source name,
        each with "chunk_count" and "indexed_at" (ISO-8601 timestamp or null).
        """
        try:
            stats = await store.source_stats()
            return json.dumps(
                {
                    "enabled": True,
                    "vector_backend": settings.vector_backend,
                    "sources": {
                        name: {
                            "chunk_count": s.chunk_count,
                            "indexed_at": s.indexed_at,
                        }
                        for name, s in stats.items()
                    },
                }
            )
        except Exception as exc:
            logger.error("rag_status failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_ingest(source_name: str | None = None) -> str:
        """Force a full re-crawl and reindex of one or all documentation sources.

        Ignores SHA change detection and re-fetches every file. Use this when
        an incremental update missed a change or a full rebuild is needed.

        Args:
            source_name: Source to reingest (e.g., "deriva-py-docs").
                If omitted, all sources are reingested.
        """
        try:
            targets = (
                [s for s in all_sources if s.name == source_name] if source_name else all_sources
            )
            if source_name and not targets:
                return json.dumps({"error": f"Unknown source: {source_name!r}"})
            counts = {}
            for src in targets:
                counts[src.name] = await docs_manager.ingest(src, force=True)
            return json.dumps({"ingested": counts})
        except Exception as exc:
            logger.error("rag_ingest failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_add_source(
        name: str,
        repo_owner: str,
        repo_name: str,
        branch: str = "master",
        path_prefix: str = "docs/",
        doc_type: str = "user-guide",
    ) -> str:
        """Register a new documentation source and immediately index it.

        Persists the source to sources.json so it survives restarts.
        Sources added via this tool are merged with built-in and plugin-declared
        sources at startup; plugin-declared sources take precedence on name conflict.

        Args:
            name: Unique identifier for the source (e.g., "myproject-docs").
            repo_owner: GitHub org or user (e.g., "my-org").
            repo_name: Repository name.
            branch: Branch to crawl (default: "master").
            path_prefix: Path prefix filter (default: "docs/").
            doc_type: Document type tag stored in the vector store.
        """
        if any(s.name == name for s in all_sources):
            return json.dumps({"error": f"Source {name!r} already exists"})
        try:
            src = DocSource(
                name=name,
                owner=repo_owner,
                repo=repo_name,
                branch=branch,
                path_prefix=path_prefix,
                doc_type=doc_type,
            )
            docs_manager.add_source(src)
            all_sources.append(src)
            count = await docs_manager.update(src)
            return json.dumps({
                "status": "added",
                "name": name,
                "chunks_indexed": count,
            })
        except Exception as exc:
            logger.error("rag_add_source failed: %s", exc)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_remove_source(name: str) -> str:
        """Remove a runtime-added documentation source and delete its indexed chunks.

        Only sources added via rag_add_source can be removed. Built-in and
        plugin-declared sources return an error if removal is attempted.

        Args:
            name: Source name to remove (e.g., "myproject-docs").
        """
        if not docs_manager.is_runtime_source(name):
            return json.dumps({
                "error": (
                    f"Source {name!r} is not a runtime-added source and cannot be removed. "
                    "Only sources added via rag_add_source can be removed."
                )
            })
        try:
            # Remove from the in-memory list
            for i, s in enumerate(all_sources):
                if s.name == name:
                    all_sources.pop(i)
                    break
            # Delete indexed chunks whose source key starts with "name:"
            await store.delete_source(name)
            docs_manager.remove_source(name)
            return json.dumps({"status": "removed", "name": name})
        except Exception as exc:
            logger.error("rag_remove_source failed: %s", exc)
            return json.dumps({"error": str(exc)})

    rag_tools = [
        rag_search, rag_update_docs, rag_index_schema, rag_index_table, rag_status,
        rag_ingest, rag_add_source, rag_remove_source,
    ]
    logger.info("RAG tools registered: %s", [fn.__name__ for fn in rag_tools])