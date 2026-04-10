from __future__ import annotations

"""Documentation source ingestion pipeline.

Manages the set of GitHub-hosted Markdown documentation sources and their
ingestion into the vector store.

Default sources (indexed as core DERIVA documentation):
    deriva-py-docs  -- informatics-isi-edu/deriva-py, branch master, path docs/
    ermrest-docs    -- informatics-isi-edu/ermrest, branch master, path docs/
    chaise-docs     -- informatics-isi-edu/chaise, branch master, path docs/

Additional sources can be registered at runtime via the rag_add_source tool
(out of scope for Phase 4 -- reserved for future extension).

Key functions:
    ingest_docs(source_name?, force?)  -- full crawl and ingestion
    update_docs(source_name?)          -- incremental update (SHA delta)
"""

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from .chunker import chunk_markdown
from .github_crawler import GitHubCrawler
from .web_crawler import WebCrawler

if TYPE_CHECKING:
    from .config import RAGSettings
    from .store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class DocSource:
    """Configuration for a GitHub-hosted Markdown documentation source."""

    name: str  # unique identifier (e.g., "deriva-py-docs")
    owner: str  # GitHub org or user (e.g., "informatics-isi-edu")
    repo: str  # repository name
    branch: str  # branch name (e.g., "master")
    path_prefix: str  # path prefix filter (e.g., "docs/")
    doc_type: str = "user-guide"  # stored as metadata in the vector store


@dataclass
class WebSource:
    """Configuration for a website to crawl and index."""

    name: str  # unique identifier (e.g., "facebase-web")
    base_url: str  # crawl root (e.g., "https://www.facebase.org")
    max_pages: int = 200  # crawl page limit
    doc_type: str = "web-content"
    allowed_domains: list[str] = field(default_factory=list)  # empty = base_url domain only
    include_path_prefix: str = ""  # only index URLs under this prefix (optional)
    rate_limit_seconds: float = 1.0  # delay between HTTP requests


@dataclass
class LocalSource:
    """Configuration for a local filesystem documentation source."""

    name: str  # unique identifier (e.g., "my-site-docs")
    path: str  # absolute or relative path to directory or single file
    glob: str = "**/*.md"  # glob pattern for file discovery
    doc_type: str = "user-guide"
    encoding: str = "utf-8"


# Built-in documentation sources indexed by default when RAG is enabled
BUILTIN_SOURCES: list[DocSource] = [
    DocSource(
        name="deriva-py-docs",
        owner="informatics-isi-edu",
        repo="deriva-py",
        branch="master",
        path_prefix="docs/",
        doc_type="user-guide",
    ),
    DocSource(
        name="ermrest-docs",
        owner="informatics-isi-edu",
        repo="ermrest",
        branch="master",
        path_prefix="docs/",
        doc_type="user-guide",
    ),
    DocSource(
        name="chaise-docs",
        owner="informatics-isi-edu",
        repo="chaise",
        branch="master",
        path_prefix="docs/",
        doc_type="user-guide",
    ),
]


class RAGDocsManager:
    """Manages documentation source ingestion into the vector store."""

    def __init__(self, store: VectorStore, settings: RAGSettings) -> None:
        self._store = store
        self._data_dir = Path(os.path.expanduser(settings.rag_dir))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._startup_ttl_seconds = settings.startup_ttl_hours * 3600

    # ------------------------------------------------------------------
    # Source-level timestamp tracking (startup TTL gate)
    # ------------------------------------------------------------------

    def _timestamps_path(self) -> Path:
        return self._data_dir / "source_timestamps.json"

    def _load_timestamps(self) -> dict[str, float]:
        path = self._timestamps_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _mark_source_indexed(self, source_name: str) -> None:
        timestamps = self._load_timestamps()
        timestamps[source_name] = time.time()
        path = self._timestamps_path()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(timestamps), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.warning("Failed to save source timestamps", exc_info=True)

    def is_source_fresh(self, source_name: str) -> bool:
        """Return True if source was indexed within startup_ttl_seconds."""
        if self._startup_ttl_seconds <= 0:
            return False
        timestamps = self._load_timestamps()
        ts = timestamps.get(source_name)
        if ts is None:
            return False
        return (time.time() - ts) < self._startup_ttl_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ingest(self, source: DocSource, force: bool = False) -> int:
        """Full crawl and ingestion of a documentation source.

        Fetches all .md files, chunks them, and upserts into the vector store.
        When force=False, individual files whose SHA matches the cache are
        skipped (incremental). When force=True, all files are re-fetched.

        Args:
            source: DocSource configuration.
            force: If True, re-fetch and reindex all files regardless of SHA.

        Returns:
            Number of files ingested (fetched and upserted).
        """
        logger.info("ingest %r: crawling %s/%s@%s/%s", source.name, source.owner, source.repo, source.branch, source.path_prefix)
        crawler = GitHubCrawler(
            owner=source.owner,
            repo=source.repo,
            branch=source.branch,
            path_prefix=source.path_prefix,
        )
        entries = await crawler.list_files()
        sha_cache = self._load_sha_cache(source.name) if not force else {}

        ingested = 0
        new_cache = dict(sha_cache)

        for entry in entries:
            if not force and sha_cache.get(entry.path) == entry.sha:
                continue  # unchanged since last crawl

            try:
                content = await crawler.fetch_content(entry)
            except Exception:
                logger.warning("Failed to fetch %s/%s", source.repo, entry.path, exc_info=True)
                continue

            file_source = f"{source.name}:{entry.path}"
            chunks = chunk_markdown(content, source=file_source, doc_type=source.doc_type)
            if chunks:
                await self._store.upsert(chunks)
            new_cache[entry.path] = entry.sha
            ingested += 1

        self._save_sha_cache(source.name, new_cache)
        self._mark_source_indexed(source.name)
        logger.info("ingest %r: %d/%d files processed", source.name, ingested, len(entries))
        return ingested

    async def update(self, source: DocSource) -> int:
        """Incremental update: only re-fetch files whose SHA has changed.

        Equivalent to ingest(source, force=False).
        """
        return await self.ingest(source, force=False)

    # ------------------------------------------------------------------
    # Runtime source persistence
    # ------------------------------------------------------------------

    def _runtime_sources_path(self) -> Path:
        return self._data_dir / "sources.json"

    def load_runtime_sources(self) -> list[DocSource]:
        """Return the list of runtime-added sources from sources.json."""
        path = self._runtime_sources_path()
        if not path.exists():
            return []
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            return [DocSource(**e) for e in entries]
        except Exception:
            logger.warning("Failed to load runtime sources from %s", path, exc_info=True)
            return []

    def _save_runtime_sources(self, sources: list[DocSource]) -> None:
        path = self._runtime_sources_path()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(
                    [
                        {
                            "name": s.name,
                            "owner": s.owner,
                            "repo": s.repo,
                            "branch": s.branch,
                            "path_prefix": s.path_prefix,
                            "doc_type": s.doc_type,
                        }
                        for s in sources
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.warning("Failed to save runtime sources to %s", path, exc_info=True)

    def add_source(self, source: DocSource) -> None:
        """Add a runtime source and persist to sources.json.

        If a source with the same name already exists it is replaced.
        """
        existing = [s for s in self.load_runtime_sources() if s.name != source.name]
        self._save_runtime_sources(existing + [source])

    def remove_source(self, name: str) -> None:
        """Remove a runtime source from sources.json.

        Does not touch the vector store; callers must delete indexed chunks
        separately if needed.
        """
        updated = [s for s in self.load_runtime_sources() if s.name != name]
        self._save_runtime_sources(updated)

    def is_runtime_source(self, name: str) -> bool:
        """Return True if name is in the runtime sources list."""
        return any(s.name == name for s in self.load_runtime_sources())

    # ------------------------------------------------------------------
    # SHA cache persistence
    # ------------------------------------------------------------------

    def _sha_cache_path(self, source_name: str) -> Path:
        return self._data_dir / f"{source_name}_sha_cache.json"

    def _load_sha_cache(self, source_name: str) -> dict[str, str]:
        path = self._sha_cache_path(source_name)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_sha_cache(self, source_name: str, cache: dict[str, str]) -> None:
        path = self._sha_cache_path(source_name)
        try:
            path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except Exception:
            logger.warning("Failed to save SHA cache for %r", source_name, exc_info=True)

    # ------------------------------------------------------------------
    # Web source ingestion
    # ------------------------------------------------------------------

    # Number of changed pages to accumulate before flushing to the vector store.
    _WEB_BATCH_SIZE: int = 10

    async def ingest_web(
        self,
        source: WebSource,
        force: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> int:
        """Crawl a website and ingest its content into the vector store.

        Uses WebCrawler for BFS traversal. Pages whose content hash has not
        changed since the last crawl are skipped (unless force=True).
        Changed pages are accumulated in batches of _WEB_BATCH_SIZE before
        being written to the vector store to reduce round-trip overhead.

        Args:
            source: WebSource configuration.
            force: If True, re-fetch and reindex all pages regardless of cache.
            progress_cb: Optional callback invoked after each crawled page with
                (pages_crawled, pages_ingested). Used to stream progress to the
                task manager for get_task_status() polling.

        Returns:
            Number of pages ingested (upserted).
        """
        logger.info(
            "ingest_web %r: crawling %s (max_pages=%d)",
            source.name, source.base_url, source.max_pages,
        )
        crawler = WebCrawler(
            base_url=source.base_url,
            max_pages=source.max_pages,
            allowed_domains=source.allowed_domains or None,
            include_path_prefix=source.include_path_prefix,
            rate_limit_seconds=source.rate_limit_seconds,
        )
        url_cache = self._load_url_cache(source.name) if not force else {}
        new_cache: dict[str, str] = {}
        ingested = 0
        total = 0

        # Accumulate (chunk_source, chunks) pairs; flush when batch is full.
        pending: list[tuple[str, list]] = []

        async def _flush() -> None:
            if not pending:
                return
            for chunk_source, chunks in pending:
                await self._store.upsert(chunks)
            pending.clear()

        async for result in crawler.crawl():
            total += 1
            content_hash = hashlib.md5(result.text.encode()).hexdigest()  # noqa: S324
            new_cache[result.url] = content_hash

            if not force and url_cache.get(result.url) == content_hash:
                if progress_cb is not None:
                    progress_cb(total, ingested)
                continue  # unchanged since last crawl

            doc = f"# {result.title}\n\n{result.text}" if result.title else result.text
            chunk_source = f"{source.name}:{result.url}"
            chunks = chunk_markdown(doc, source=chunk_source, doc_type=source.doc_type)
            if chunks:
                pending.append((chunk_source, chunks))
            ingested += 1

            if len(pending) >= self._WEB_BATCH_SIZE:
                await _flush()

            if progress_cb is not None:
                progress_cb(total, ingested)

        await _flush()
        self._save_url_cache(source.name, new_cache)
        self._mark_source_indexed(source.name)
        logger.info("ingest_web %r: %d/%d pages processed", source.name, ingested, total)
        return ingested

    def _url_cache_path(self, source_name: str) -> Path:
        return self._data_dir / f"{source_name}_url_cache.json"

    def _load_url_cache(self, source_name: str) -> dict[str, str]:
        path = self._url_cache_path(source_name)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_url_cache(self, source_name: str, cache: dict[str, str]) -> None:
        path = self._url_cache_path(source_name)
        try:
            path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except Exception:
            logger.warning("Failed to save URL cache for %r", source_name, exc_info=True)

    # ------------------------------------------------------------------
    # Local filesystem source ingestion
    # ------------------------------------------------------------------

    async def ingest_local(self, source: LocalSource, force: bool = False) -> int:
        """Ingest local filesystem files into the vector store.

        Walks source.path using source.glob. Files whose mtime has not
        changed since the last ingest are skipped (unless force=True).

        Args:
            source: LocalSource configuration.
            force: If True, reindex all files regardless of mtime cache.

        Returns:
            Number of files ingested (upserted).
        """
        base = Path(source.path)
        if base.is_dir():
            files = list(base.glob(source.glob))
        elif base.is_file():
            files = [base]
        else:
            logger.warning("ingest_local %r: path does not exist: %s", source.name, source.path)
            return 0

        mtime_cache = self._load_mtime_cache(source.name) if not force else {}
        new_cache: dict[str, str] = {}
        ingested = 0

        for file_path in files:
            if not file_path.is_file():
                continue
            mtime_str = str(file_path.stat().st_mtime)
            try:
                rel = str(file_path.relative_to(base))
            except ValueError:
                rel = file_path.name
            new_cache[rel] = mtime_str

            if not force and mtime_cache.get(rel) == mtime_str:
                continue  # unchanged since last ingest

            try:
                content = file_path.read_text(encoding=source.encoding)
            except Exception:
                logger.warning(
                    "ingest_local %r: failed to read %s", source.name, file_path, exc_info=True,
                )
                continue

            chunk_source = f"{source.name}:{rel}"
            chunks = chunk_markdown(content, source=chunk_source, doc_type=source.doc_type)
            if chunks:
                await self._store.upsert(chunks)
            ingested += 1

        self._save_mtime_cache(source.name, new_cache)
        logger.info("ingest_local %r: %d/%d files processed", source.name, ingested, len(files))
        return ingested

    def _mtime_cache_path(self, source_name: str) -> Path:
        return self._data_dir / f"{source_name}_mtime_cache.json"

    def _load_mtime_cache(self, source_name: str) -> dict[str, str]:
        path = self._mtime_cache_path(source_name)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_mtime_cache(self, source_name: str, cache: dict[str, str]) -> None:
        path = self._mtime_cache_path(source_name)
        try:
            path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except Exception:
            logger.warning("Failed to save mtime cache for %r", source_name, exc_info=True)
