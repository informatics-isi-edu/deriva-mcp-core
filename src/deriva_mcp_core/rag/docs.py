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

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from .chunker import chunk_markdown
from .github_crawler import GitHubCrawler

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
        self._data_dir = Path(os.path.expanduser(settings.data_dir))
        self._data_dir.mkdir(parents=True, exist_ok=True)

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
