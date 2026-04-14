from __future__ import annotations

"""MCP tool registration for the RAG subsystem.

register(ctx) is called from rag/__init__.py during server startup.
get_rag_store() exposes the active VectorStore to other tool modules
(e.g. entity.py uses it for RAG suggestions).
"""

import asyncio
import datetime
import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..context import get_catalog, get_request_user_id, resolve_user_identity

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)

# Module-level reference to the active VectorStore. Set by register() when
# DERIVA_MCP_RAG_ENABLED=true. None when RAG is disabled or not yet started.
_rag_store: Any | None = None

# RAG configuration summary populated by register(). None when RAG is disabled.
_rag_status: dict | None = None


def get_rag_status() -> dict | None:
    """Return a summary of RAG configuration, or None if RAG is disabled."""
    return _rag_status

# Per-user schema visibility class index. Maps (user_id, hostname, catalog_id)
# to the 16-character truncated schema hash stored in the vector store source name.
# Populated by the on_catalog_connect hook and rag_index_schema.
# Used by rag_search to restrict schema results to the caller's ACL view.
_user_schema_hashes: dict[tuple[str, str, str], str] = {}

# Per-source enricher locks. Prevents concurrent enricher runs for the same
# source (e.g. when multiple catalog connects fire before the first run writes
# its chunks back). If a run is already in progress, new fires are skipped.
_enricher_locks: dict[str, asyncio.Lock] = {}

# Number of dataset rows to enrich per batch. Bounds peak memory: each batch
# is enriched, numbered, and upserted before the next batch begins.
_ENRICH_BATCH_SIZE = 10

# Number of chunks per store.add() call for rag_import_chunks.
_IMPORT_BATCH_SIZE = 50


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

    from .chunker import chunk_markdown
    from .data import index_table_data
    from .docs import BUILTIN_SOURCES, DocSource, LocalSource, RAGDocsManager, WebSource
    from .schema import compute_schema_hash, has_schema, index_schema, schema_source_name
    from .store import Chunk, get_store

    store = get_store(settings)
    docs_manager = RAGDocsManager(store, settings)

    global _rag_store, _rag_status
    _rag_store = store
    _rag_status = {
        "backend": settings.vector_backend,
        "auto_update": settings.auto_update,
        "data_dir": str(settings.data_dir),
    }

    # Collect all documentation sources: built-ins + plugin-declared + runtime-added
    all_sources: list[DocSource | WebSource | LocalSource] = list(BUILTIN_SOURCES)
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
    for decl in ctx._rag_web_sources:
        all_sources.append(
            WebSource(
                name=decl.name,
                base_url=decl.base_url,
                max_pages=decl.max_pages,
                doc_type=decl.doc_type,
                allowed_domains=decl.allowed_domains,
                include_path_prefix=decl.include_path_prefix,
                rate_limit_seconds=decl.rate_limit_seconds,
            )
        )
    for decl in ctx._rag_local_sources:
        all_sources.append(
            LocalSource(
                name=decl.name,
                path=decl.path,
                glob=decl.glob,
                doc_type=decl.doc_type,
                encoding=decl.encoding,
            )
        )

    # Load runtime-added sources (persisted across restarts via sources.json).
    # Plugin-declared sources take precedence on name conflict.
    _static_names = {s.name for s in all_sources}
    for runtime_src in docs_manager.load_runtime_sources():
        if runtime_src.name not in _static_names:
            all_sources.append(runtime_src)

    async def _ingest_any(
        src: DocSource | WebSource | LocalSource,
        force: bool,
        progress_cb=None,
    ) -> int:
        """Dispatch to the correct ingest method based on source type."""
        if isinstance(src, WebSource):
            return await docs_manager.ingest_web(src, force=force, progress_cb=progress_cb)
        if isinstance(src, LocalSource):
            return await docs_manager.ingest_local(src, force=force)
        return await docs_manager.ingest(src, force=force)

    # Index sources not yet in the vector store at startup, skipping any
    # that were indexed recently (within startup_ttl_hours).
    if settings.auto_update:
        async def _startup_update() -> None:
            eligible = all_sources if settings.auto_update_web_sources else [
                s for s in all_sources if not isinstance(s, WebSource)
            ]
            stale = [s for s in eligible if not docs_manager.is_source_fresh(s.name)]
            fresh = [s for s in eligible if docs_manager.is_source_fresh(s.name)]
            if fresh:
                logger.info(
                    "RAG startup: skipping %d fresh source(s) (within %dh TTL): %s",
                    len(fresh),
                    settings.startup_ttl_hours,
                    ", ".join(s.name for s in fresh),
                )
            if not stale:
                logger.info("RAG startup: all sources are fresh, nothing to crawl")
                return
            logger.info(
                "RAG startup crawl: updating %d stale source(s): %s",
                len(stale),
                ", ".join(s.name for s in stale),
            )
            failed = 0
            for src in stale:
                try:
                    await _ingest_any(src, force=False)
                except Exception:
                    failed += 1
                    logger.warning("Startup doc update failed for %r", src.name, exc_info=True)
            logger.info(
                "RAG startup crawl complete: %d source(s) processed, %d failed",
                len(stale),
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
            # Record this user's visibility class so rag_search can filter to it.
            # resolve_user_identity uses the contextvar in HTTP mode (already set)
            # and the cached /authn/session result in stdio mode.
            user_id = resolve_user_identity(hostname)
            _user_schema_hashes[(user_id, hostname, catalog_id)] = schema_hash[:16]
        except Exception:
            logger.warning("Schema auto-index failed for %s/%s", hostname, catalog_id, exc_info=True)

        # Run dataset enrichers declared by plugins (TTL-gated, auto_enrich opt-in only).
        # Both the indexer flag and DERIVA_MCP_RAG_AUTO_ENRICH must be true.
        if settings.auto_enrich:
            for indexer in ctx._rag_dataset_indexers:
                if indexer.auto_enrich:
                    await _run_dataset_enricher(hostname, catalog_id, indexer)

    ctx.on_catalog_connect(_handle_catalog_connect)

    async def _run_dataset_enricher(
        hostname: str,
        catalog_id: str,
        indexer: Any,
    ) -> dict | None:
        """Fetch rows, call the enricher, and index enriched chunks.

        Returns a dict {"rows_fetched": N, "failed": N, "chunks": N} on
        completion, or None if the run was skipped (locked, TTL fresh, or
        fetch error).

        Source name: enriched:{hostname}:{catalog_id}:{schema}:{table}
        Staleness is checked per source using the indexer's ttl_seconds.

        Concurrent fires are skipped (not queued): the TTL check is not atomic,
        so multiple catalog-connect events arriving before the first run completes
        would all pass the check and duplicate work. The per-source lock ensures
        only one run proceeds at a time.

        Rows are processed in batches of _ENRICH_BATCH_SIZE. The source is
        deleted once upfront and chunks are added batch-by-batch, bounding peak
        memory to one batch at a time rather than accumulating all chunks first.
        """
        # Skip if this enricher is scoped to a specific hostname or catalog_id
        if indexer.hostname and indexer.hostname != hostname:
            return
        if indexer.catalog_id and indexer.catalog_id != catalog_id:
            return

        source_name = f"enriched:{hostname}:{catalog_id}:{indexer.schema}:{indexer.table}"

        # Skip if another run for this source is already in progress.
        # lock.locked() + async with is safe under asyncio cooperative scheduling:
        # no context switch can occur between the check and the acquire.
        lock = _enricher_locks.setdefault(source_name, asyncio.Lock())
        if lock.locked():
            logger.info("Dataset enricher already in progress for %s, skipping", source_name)
            return None

        async with lock:
            # TTL check inside the lock so the winner of a concurrent burst
            # doesn't re-run immediately after the first finishes.
            try:
                if await store.has_source(source_name):
                    stats = await store.source_stats()
                    entry = stats.get(source_name)
                    if entry and entry.indexed_at:
                        ts = datetime.datetime.fromisoformat(entry.indexed_at)
                        age = time.time() - ts.timestamp()
                        # Config var overrides the per-indexer TTL.
                        # None (default) means never re-run automatically.
                        effective_ttl = (
                            settings.dataset_enricher_ttl_seconds
                            if settings.dataset_enricher_ttl_seconds is not None
                            else indexer.ttl_seconds
                        )
                        if age < effective_ttl and entry.chunk_count > 0:
                            return None
            except Exception:
                logger.warning(
                    "Dataset enricher TTL check failed for %s -- skipping run",
                    source_name,
                    exc_info=True,
                )
                return None

            try:
                enc = lambda v: urllib.parse.quote(str(v), safe="")  # noqa: E731
                catalog = get_catalog(hostname, catalog_id)
                url = f"/entity/{enc(indexer.schema)}:{enc(indexer.table)}"
                for k, v in indexer.filter.items():
                    v_str = str(v).lower() if isinstance(v, bool) else str(v)
                    url = f"{url}/{enc(k)}={enc(v_str)}"
                if indexer.limit:
                    url = f"{url}?limit={indexer.limit}"
                rows = await asyncio.to_thread(lambda: catalog.get(url).json())
            except Exception:
                logger.warning(
                    "Dataset enricher fetch failed for %s:%s on %s/%s",
                    indexer.schema, indexer.table, hostname, catalog_id,
                    exc_info=True,
                )
                return None

            logger.info("Dataset enricher: fetched %d rows for %s", len(rows), source_name)

            # Delete stale data once so batched adds don't overwrite each other.
            try:
                await store.delete_source(source_name)
            except Exception:
                logger.warning("Dataset enricher: delete_source failed for %s", source_name, exc_info=True)

            # Process rows in batches, adding each batch immediately to bound
            # peak memory. chunk_index is a global counter so IDs are unique
            # within the shared source name across batches.
            total_chunks = 0
            failed = 0
            chunk_offset = 0
            for batch_start in range(0, len(rows), _ENRICH_BATCH_SIZE):
                batch = rows[batch_start:batch_start + _ENRICH_BATCH_SIZE]
                batch_chunks = []
                for row in batch:
                    try:
                        text = await indexer.enricher(row, catalog)
                    except Exception:
                        logger.warning("Dataset enricher callable failed for row %s", row.get("RID"), exc_info=True)
                        failed += 1
                        continue
                    if not text:
                        continue
                    rid = row.get("RID", "")
                    row_title = (row.get("title") or "").strip()
                    chaise_url = (f"https://{hostname}/chaise/record/#{catalog_id}"
                                  f"/{enc(indexer.schema)}:{enc(indexer.table)}/RID={enc(rid)}"
                                  if rid else "")
                    for chunk in chunk_markdown(text, source=source_name, doc_type=indexer.doc_type):
                        chunk.url = chaise_url
                        chunk.title = row_title
                        batch_chunks.append(chunk)

                for i, chunk in enumerate(batch_chunks):
                    chunk.chunk_index = chunk_offset + i
                chunk_offset += len(batch_chunks)

                if batch_chunks:
                    await store.add(batch_chunks)
                    total_chunks += len(batch_chunks)
                logger.debug(
                    "Dataset enricher batch %d-%d: %d chunks (%s)",
                    batch_start, batch_start + len(batch) - 1, len(batch_chunks), source_name,
                )

            logger.info(
                "Dataset enricher: %s -- %d rows fetched, %d failed, %d chunks indexed",
                source_name, len(rows), failed, total_chunks,
            )
            return {"rows_fetched": len(rows), "failed": failed, "chunks": total_chunks}

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
                # Restrict schema results to this caller's ACL visibility class.
                # Two users whose /schema responses are identical share the same
                # hash and therefore the same index entry. A restricted user gets
                # a different hash and only sees their own entry. Non-schema
                # sources (user-guide, data) are always included.
                user_id = resolve_user_identity(hostname)
                user_hash = _user_schema_hashes.get((user_id, hostname, catalog_id))
                if user_hash:
                    own_source = schema_source_name(hostname, catalog_id, user_hash)
                    results = [
                        r for r in results
                        if not r.source.startswith("schema:") or r.source == own_source
                    ]
                else:
                    # Schema not yet indexed for this user -- exclude schema results.
                    results = [r for r in results if not r.source.startswith("schema:")]
            out = []
            for r in results:
                entry: dict = {
                    "text": r.text,
                    "source": r.source,
                    "doc_type": r.doc_type,
                    "score": round(r.score, 4),
                }
                url = r.metadata.get("url", "")
                if url:
                    entry["url"] = url
                title = r.metadata.get("title", "")
                if title:
                    entry["title"] = title
                out.append(entry)
            return json.dumps(out)
        except Exception as exc:
            logger.error("rag_search failed: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_update_docs(
        source_name: str | None = None,
        force: bool = False,
    ) -> str:
        """Incrementally update already-indexed GitHub documentation sources (SHA delta).

        Re-indexes only files whose SHA has changed since the last crawl.
        Use for keeping GitHub-sourced docs current. NOT suitable for web sources
        (e.g. "facebase-web") or any source in rag_status "available_to_ingest" --
        use rag_ingest for those.

        Args:
            source_name: Specific source to update (e.g., "deriva-py-docs").
                If omitted, all sources are updated.
            force: If True, re-fetch and re-index all files regardless of SHA.
        """
        try:
            targets = [s for s in all_sources if s.name == source_name] if source_name else all_sources
            if source_name and not targets:
                return json.dumps({"error": f"Unknown source: {source_name!r}"})
            counts = {}
            for src in targets:
                counts[src.name] = await _ingest_any(src, force=force)
            return json.dumps({"updated": counts})
        except Exception as exc:
            logger.error("rag_update_docs failed: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_update_docs_async(
        source_name: str | None = None,
        force: bool = False,
    ) -> str:
        """Submit a documentation update as a background task. Returns task_id immediately.

        Same behavior as rag_update_docs but runs in the background so the tool
        returns immediately. Use get_task_status(task_id) to poll for completion.

        Args:
            source_name: Specific source to update (e.g., "deriva-py-docs").
                If omitted, all sources are updated.
            force: If True, re-fetch and re-index all files regardless of SHA.
                Use when content may have changed without a SHA update, or to
                force a full rebuild. Default False (incremental).
        """
        targets = ([s for s in all_sources if s.name == source_name] if source_name else all_sources)
        if source_name and not targets:
            return json.dumps({"error": f"Unknown source: {source_name!r}"})

        async def _do_update() -> dict:
            counts = {}
            for src in targets:
                counts[src.name] = await _ingest_any(src, force=force)
            return {"updated": counts}

        task_label = source_name or "all-sources"
        try:
            task_id = ctx.submit_task(
                _do_update(),
                name=f"rag_update_docs {task_label}",
            )
        except Exception as exc:
            logger.error("rag_update_docs_async failed to submit: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps({"task_id": task_id, "status": "submitted"})

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
            user_id = resolve_user_identity(hostname)
            _user_schema_hashes[(user_id, hostname, catalog_id)] = schema_hash[:16]
            return json.dumps(
                {
                    "status": "indexed",
                    "hostname": hostname,
                    "catalog_id": catalog_id,
                    "schema_hash": schema_hash[:16],
                }
            )
        except Exception as exc:
            logger.error("rag_index_schema failed: %s", exc, exc_info=True)
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
            logger.error("rag_index_table failed: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_status() -> str:
        """Return RAG subsystem status: per-source chunk counts and timestamps.

        Returns a JSON object with a "sources" dict keyed by source name,
        each with "chunk_count", "indexed_at" (ISO-8601 or null), and
        "registered" (True for all known sources, False for store-only entries).
        Sources registered via plugins or config that have not yet been indexed
        appear with chunk_count 0 and indexed_at null so callers know they exist
        and can be targeted by rag_ingest or rag_update_docs.
        """
        try:
            stats = await store.source_stats()
            indexed: dict[str, dict] = {
                name: {
                    "chunk_count": s.chunk_count,
                    "indexed_at": s.indexed_at,
                }
                for name, s in stats.items()
            }
            # Sources registered via plugin/config that have not yet been indexed.
            # indexed keys are compound ("source-name:path/to/file"); extract prefix.
            indexed_source_names = {k.split(":")[0] for k in indexed}
            available_to_ingest = [src.name for src in all_sources if src.name not in indexed_source_names]
            return json.dumps(
                {
                    "enabled": True,
                    "vector_backend": settings.vector_backend,
                    "available_to_ingest": available_to_ingest,
                    "indexed_sources": indexed,
                }
            )
        except Exception as exc:
            logger.error("rag_status failed: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_ingest(source_name: str | None = None) -> str:
        """Force a full re-crawl and reindex as a background task. Returns task_id immediately.

        Use this tool when a source appears in rag_status "available_to_ingest" (not yet
        indexed) OR when a full forced reindex of any source is needed. This is the correct
        tool for web sources (e.g. "facebase-web") and for any initial indexing of a source.
        Runs in the background -- use get_task_status(task_id) to poll for completion.

        Do NOT use rag_update_docs for web sources or sources that have never been indexed;
        rag_update_docs is for incremental SHA-delta updates of already-indexed GitHub sources.

        Args:
            source_name: Exact source name as it appears in rag_status (e.g. "facebase-web").
                If omitted, all registered sources are ingested.
        """
        targets = ([s for s in all_sources if s.name == source_name] if source_name else all_sources)
        if source_name and not targets:
            return json.dumps({"error": f"Unknown source: {source_name!r}"})

        task_id_ref: list[str] = []

        async def _do_ingest() -> dict:
            task_id = task_id_ref[0] if task_id_ref else None
            counts = {}
            for i, src in enumerate(targets):
                if task_id:
                    ctx._task_manager.update_progress(
                        task_id,
                        f"source {i + 1}/{len(targets)}: starting {src.name!r}",
                    )

                def _make_progress_cb(name: str, tid: str):
                    def _cb(crawled: int, ingested: int) -> None:
                        ctx._task_manager.update_progress(
                            tid,
                            f"{name!r}: {crawled} pages crawled, {ingested} ingested",
                        )
                    return _cb

                cb = _make_progress_cb(src.name, task_id) if task_id else None
                counts[src.name] = await _ingest_any(src, force=True, progress_cb=cb)
            return {"ingested": counts}

        task_label = source_name or "all-sources"
        try:
            task_id = ctx.submit_task(
                _do_ingest(),
                name=f"rag_ingest {task_label}",
            )
            task_id_ref.append(task_id)
        except Exception as exc:
            logger.error("rag_ingest failed to submit: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps({"task_id": task_id, "status": "submitted"})

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
            logger.error("rag_add_source failed: %s", exc, exc_info=True)
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
            logger.error("rag_remove_source failed: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    @ctx.tool(mutates=False)
    async def rag_import_chunks(
        file_path: str,
        source_name: str | None = None,
        doc_type: str | None = None,
        replace: bool = False,
    ) -> str:
        """Bulk-import pre-built chunks from a JSON file into the vector store.

        Reads a JSON array of chunk objects and upserts them directly without
        crawling. Designed for operators who run an offline crawl or ETL pipeline
        and produce a standard chunk export.

        Each chunk object must have a "text" field. Optional fields: "source",
        "doc_type", "chunk_index", "metadata".

        Args:
            file_path: Path to a JSON file containing a list of chunk objects.
            source_name: If provided, overrides the "source" field in every chunk.
            doc_type: If provided, overrides the "doc_type" field in every chunk.
            replace: If True, delete all existing chunks for the effective source
                name before upserting. Requires source_name to be set.
        """
        try:
            data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("rag_import_chunks: failed to read %s: %s", file_path, exc, exc_info=True)
            return json.dumps({"error": f"Failed to read file: {exc}"})

        if not isinstance(data, list):
            return json.dumps({"error": "File must contain a JSON array"})

        if replace and not source_name:
            return json.dumps({"error": "replace=True requires source_name to be set"})

        # Group chunks by effective source so each source is deleted once
        # upfront and then added in batches, bounding peak memory.
        groups: dict[str, list[Chunk]] = {}
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            text = item.get("text", "")
            if not text:
                continue
            eff_source = source_name or item.get("source", "imported")
            eff_doc_type = doc_type or item.get("doc_type", "user-guide")
            if eff_source not in groups:
                groups[eff_source] = []
            groups[eff_source].append(Chunk(
                text=text,
                source=eff_source,
                doc_type=eff_doc_type,
                section_heading=item.get("section_heading", ""),
                heading_hierarchy=item.get("heading_hierarchy", []),
                chunk_index=item.get("chunk_index", i),
            ))

        total = 0
        for eff_source, source_chunks in groups.items():
            try:
                await store.delete_source(eff_source)
            except Exception as exc:
                logger.warning("rag_import_chunks: delete_source failed for %s: %s", eff_source, exc, exc_info=True)
            for batch_start in range(0, len(source_chunks), _IMPORT_BATCH_SIZE):
                batch = source_chunks[batch_start:batch_start + _IMPORT_BATCH_SIZE]
                try:
                    await store.add(batch)
                except Exception as exc:
                    logger.error("rag_import_chunks: add failed for %s: %s", eff_source, exc, exc_info=True)
                    return json.dumps({"error": f"Add failed: {exc}"})
            total += len(source_chunks)

        return json.dumps({"status": "imported", "chunk_count": total})

    @ctx.tool(mutates=False)
    async def rag_ingest_datasets(
        hostname: str,
        catalog_id: str,
        source_name: str | None = None,
    ) -> str:
        """Force re-enrichment of dataset indexer sources as a background task.

        Bypasses the TTL gate and re-fetches, enriches, and re-indexes all rows
        from the registered dataset enrichers for the given catalog. Use this
        when the enricher output format has changed (e.g. after an enricher code
        update) and you need the indexed chunks to reflect the new format without
        waiting for the TTL to expire.

        Returns a task_id immediately. Use get_task_status(task_id) to poll.

        Args:
            hostname: Catalog hostname (e.g. "dev.facebase.org").
            catalog_id: Catalog ID (e.g. "1").
            source_name: If provided, only re-enrich the matching enricher source
                (e.g. "enriched:dev.facebase.org:1:isa:dataset"). If omitted,
                all enrichers registered for this catalog are re-run.
        """
        indexers = [
            ix for ix in ctx._rag_dataset_indexers
            if (not ix.hostname or ix.hostname == hostname)
            and (not ix.catalog_id or ix.catalog_id == catalog_id)
        ]
        if not indexers:
            return json.dumps({"error": "No dataset indexers registered for this catalog"})

        if source_name:
            indexers = [
                ix for ix in indexers
                if f"enriched:{hostname}:{catalog_id}:{ix.schema}:{ix.table}" == source_name
            ]
            if not indexers:
                return json.dumps({"error": f"No indexer matches source_name {source_name!r}"})

        async def _do_enrich() -> dict:
            results = {}
            for ix in indexers:
                src = f"enriched:{hostname}:{catalog_id}:{ix.schema}:{ix.table}"
                # Delete existing data so the TTL check passes on re-entry.
                try:
                    await store.delete_source(src)
                except Exception:
                    pass
                try:
                    run_stats = await _run_dataset_enricher(hostname, catalog_id, ix)
                    results[src] = run_stats if run_stats is not None else {"skipped": True}
                except Exception as exc:
                    results[src] = {"error": str(exc)}
            return {"enriched": results}

        try:
            task_id = ctx.submit_task(
                _do_enrich(),
                name=f"rag_ingest_datasets {hostname}/{catalog_id}",
            )
        except Exception as exc:
            logger.error("rag_ingest_datasets failed to submit: %s", exc, exc_info=True)
            return json.dumps({"error": str(exc)})
        return json.dumps({"task_id": task_id, "status": "submitted"})

    rag_tools = [
        rag_search, rag_update_docs, rag_update_docs_async, rag_index_schema,
        rag_index_table, rag_status, rag_ingest, rag_add_source, rag_remove_source,
        rag_import_chunks, rag_ingest_datasets,
    ]
    logger.info("RAG tools registered: %s", [fn.__name__ for fn in rag_tools])
