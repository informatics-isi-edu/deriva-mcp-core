"""Unit tests for rag/crawler.py and rag/docs.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deriva_mcp_core.rag.crawler import FileEntry, GitHubCrawler
from deriva_mcp_core.rag.docs import DocSource, RAGDocsManager
from deriva_mcp_core.rag.store import Chunk


# ---------------------------------------------------------------------------
# GitHubCrawler
# ---------------------------------------------------------------------------


def test_crawler_normalizes_path_prefix_adds_trailing_slash():
    crawler = GitHubCrawler("owner", "repo", "main", "docs")
    assert crawler._path_prefix == "docs/"


def test_crawler_normalizes_path_prefix_strips_leading_slash():
    crawler = GitHubCrawler("owner", "repo", "main", "/docs/")
    assert crawler._path_prefix == "docs/"


def test_crawler_empty_prefix_stays_empty():
    crawler = GitHubCrawler("owner", "repo", "main", "")
    assert crawler._path_prefix == ""


async def test_list_files_filters_by_prefix_and_extension(httpx_mock):
    """list_files returns only .md files under path_prefix."""
    httpx_mock.add_response(
        url="https://api.github.com/repos/my-org/my-repo/git/trees/main?recursive=1",
        json={
            "tree": [
                {"path": "docs/guide.md", "sha": "abc123", "type": "blob"},
                {"path": "docs/api.md", "sha": "def456", "type": "blob"},
                {"path": "docs/sub/extra.md", "sha": "ghi789", "type": "blob"},
                {"path": "src/main.py", "sha": "jkl012", "type": "blob"},
                {"path": "README.md", "sha": "mno345", "type": "blob"},
                {"path": "docs/", "sha": "tree111", "type": "tree"},
            ]
        },
    )
    crawler = GitHubCrawler("my-org", "my-repo", "main", "docs/")
    files = await crawler.list_files()

    assert len(files) == 3
    paths = [f.path for f in files]
    assert "docs/guide.md" in paths
    assert "docs/api.md" in paths
    assert "docs/sub/extra.md" in paths
    assert "src/main.py" not in paths
    assert "README.md" not in paths


async def test_list_files_no_prefix_returns_all_md(httpx_mock):
    """Empty prefix returns all .md files in the tree."""
    httpx_mock.add_response(
        url="https://api.github.com/repos/org/repo/git/trees/master?recursive=1",
        json={
            "tree": [
                {"path": "README.md", "sha": "aaa", "type": "blob"},
                {"path": "docs/guide.md", "sha": "bbb", "type": "blob"},
            ]
        },
    )
    crawler = GitHubCrawler("org", "repo", "master", "")
    files = await crawler.list_files()
    assert len(files) == 2


async def test_list_files_returns_sha(httpx_mock):
    httpx_mock.add_response(
        url="https://api.github.com/repos/org/repo/git/trees/main?recursive=1",
        json={"tree": [{"path": "docs/index.md", "sha": "cafebabe", "type": "blob"}]},
    )
    crawler = GitHubCrawler("org", "repo", "main", "docs/")
    files = await crawler.list_files()
    assert files[0].sha == "cafebabe"


async def test_fetch_content_returns_text(httpx_mock):
    """fetch_content returns the raw text of a file."""
    httpx_mock.add_response(
        url="https://raw.githubusercontent.com/org/repo/main/docs/guide.md",
        text="# Guide\n\nThis is the guide.",
    )
    crawler = GitHubCrawler("org", "repo", "main", "docs/")
    content = await crawler.fetch_content(FileEntry(path="docs/guide.md", sha="abc"))
    assert content == "# Guide\n\nThis is the guide."


# ---------------------------------------------------------------------------
# RAGDocsManager
# ---------------------------------------------------------------------------


class _MockStore:
    def __init__(self):
        self.upserted: list[list[Chunk]] = []

    async def upsert(self, chunks: list[Chunk]) -> None:
        self.upserted.append(chunks)

    async def search(self, query, limit=10, where=None):
        return []

    async def delete_source(self, source):
        pass

    async def has_source(self, source):
        return False

    async def source_stats(self):
        return {}


@pytest.fixture()
def tmp_settings(tmp_path):
    settings = MagicMock()
    settings.data_dir = str(tmp_path)
    return settings


@pytest.fixture()
def mock_store():
    return _MockStore()


@pytest.fixture()
def docs_manager(mock_store, tmp_settings):
    return RAGDocsManager(mock_store, tmp_settings)


_SOURCE = DocSource(
    name="test-docs",
    owner="org",
    repo="repo",
    branch="main",
    path_prefix="docs/",
    doc_type="user-guide",
)


async def test_ingest_fetches_and_upserts(docs_manager, mock_store):
    """ingest() crawls and upserts chunks for each file."""
    mock_crawler = MagicMock()
    mock_crawler.list_files = AsyncMock(
        return_value=[FileEntry(path="docs/guide.md", sha="abc123")]
    )
    mock_crawler.fetch_content = AsyncMock(return_value="# Guide\n\nIntroduction.")

    with patch("deriva_mcp_core.rag.docs.GitHubCrawler", return_value=mock_crawler):
        count = await docs_manager.ingest(_SOURCE)

    assert count == 1
    assert len(mock_store.upserted) == 1


async def test_ingest_skips_unchanged_sha(docs_manager, mock_store, tmp_settings):
    """ingest() skips files whose SHA matches the cache."""
    sha_cache_path = Path(tmp_settings.data_dir) / "test-docs_sha_cache.json"
    sha_cache_path.write_text(json.dumps({"docs/guide.md": "abc123"}))

    mock_crawler = MagicMock()
    mock_crawler.list_files = AsyncMock(
        return_value=[FileEntry(path="docs/guide.md", sha="abc123")]
    )
    mock_crawler.fetch_content = AsyncMock(return_value="# Guide")

    with patch("deriva_mcp_core.rag.docs.GitHubCrawler", return_value=mock_crawler):
        count = await docs_manager.ingest(_SOURCE)

    assert count == 0
    mock_crawler.fetch_content.assert_not_called()


async def test_ingest_force_refetches_all(docs_manager, mock_store, tmp_settings):
    """ingest(force=True) re-fetches all files even if SHA matches."""
    sha_cache_path = Path(tmp_settings.data_dir) / "test-docs_sha_cache.json"
    sha_cache_path.write_text(json.dumps({"docs/guide.md": "abc123"}))

    mock_crawler = MagicMock()
    mock_crawler.list_files = AsyncMock(
        return_value=[FileEntry(path="docs/guide.md", sha="abc123")]
    )
    mock_crawler.fetch_content = AsyncMock(return_value="# Guide")

    with patch("deriva_mcp_core.rag.docs.GitHubCrawler", return_value=mock_crawler):
        count = await docs_manager.ingest(_SOURCE, force=True)

    assert count == 1


async def test_update_delegates_to_ingest(docs_manager, mock_store):
    """update() calls ingest(force=False)."""
    with patch.object(docs_manager, "ingest", new=AsyncMock(return_value=3)) as mock_ingest:
        result = await docs_manager.update(_SOURCE)
    assert result == 3
    mock_ingest.assert_called_once_with(_SOURCE, force=False)


async def test_ingest_handles_fetch_error(docs_manager, mock_store):
    """ingest() skips a file if fetch_content raises, and continues."""
    mock_crawler = MagicMock()
    mock_crawler.list_files = AsyncMock(
        return_value=[
            FileEntry(path="docs/bad.md", sha="111"),
            FileEntry(path="docs/good.md", sha="222"),
        ]
    )
    mock_crawler.fetch_content = AsyncMock(
        side_effect=[RuntimeError("network error"), "# Good file"]
    )

    with patch("deriva_mcp_core.rag.docs.GitHubCrawler", return_value=mock_crawler):
        count = await docs_manager.ingest(_SOURCE)

    assert count == 1  # only good.md was ingested


def test_sha_cache_roundtrip(docs_manager, tmp_settings):
    """SHA cache is persisted and loaded correctly."""
    cache = {"docs/guide.md": "abc123", "docs/api.md": "def456"}
    docs_manager._save_sha_cache("test-source", cache)
    loaded = docs_manager._load_sha_cache("test-source")
    assert loaded == cache


def test_sha_cache_load_returns_empty_when_missing(docs_manager):
    """_load_sha_cache returns {} when no cache file exists."""
    result = docs_manager._load_sha_cache("nonexistent-source")
    assert result == {}


def test_sha_cache_save_handles_write_error(docs_manager, tmp_settings):
    """_save_sha_cache does not raise if write fails."""
    with patch("builtins.open", side_effect=OSError("permission denied")):
        docs_manager._save_sha_cache("test-source", {"file.md": "sha"})
    # Should not raise