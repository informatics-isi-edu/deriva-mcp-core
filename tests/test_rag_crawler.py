"""Unit tests for rag/github_crawler.py, rag/web_crawler.py, and rag/docs.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deriva_mcp_core.rag.github_crawler import FileEntry, GitHubCrawler
from deriva_mcp_core.rag.docs import DocSource, LocalSource, RAGDocsManager, WebSource
from deriva_mcp_core.rag.store import Chunk
from deriva_mcp_core.rag.web_crawler import WebCrawler


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

    async def add(self, chunks: list[Chunk]) -> None:
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
    settings.rag_dir = str(tmp_path / "rag")
    settings.startup_ttl_hours = 0  # disable TTL gate in tests
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
    sha_cache_path = Path(tmp_settings.rag_dir) / "test-docs_sha_cache.json"
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
    sha_cache_path = Path(tmp_settings.rag_dir) / "test-docs_sha_cache.json"
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


# ---------------------------------------------------------------------------
# WebCrawler unit tests
# ---------------------------------------------------------------------------


class TestWebCrawlerUrlFiltering:
    def test_is_crawlable_allows_base_domain(self):
        crawler = WebCrawler("https://example.com")
        assert crawler._is_crawlable("https://example.com/about")

    def test_is_crawlable_rejects_external_domain(self):
        crawler = WebCrawler("https://example.com")
        assert not crawler._is_crawlable("https://other.org/page")

    def test_is_crawlable_allows_extra_domain(self):
        crawler = WebCrawler("https://example.com", allowed_domains=["example.com", "cdn.example.com"])
        assert crawler._is_crawlable("https://cdn.example.com/asset")

    def test_is_indexable_with_prefix(self):
        crawler = WebCrawler("https://example.com", include_path_prefix="/docs")
        assert crawler._is_indexable("https://example.com/docs/guide")
        assert not crawler._is_indexable("https://example.com/about")

    def test_is_indexable_without_prefix(self):
        crawler = WebCrawler("https://example.com")
        assert crawler._is_indexable("https://example.com/anything")

    def test_has_loop_repeated_segment(self):
        crawler = WebCrawler("https://example.com")
        assert crawler._has_loop("https://example.com/a/b/a/b/a/b/c")

    def test_has_loop_excessive_depth(self):
        crawler = WebCrawler("https://example.com", max_depth=5)
        assert crawler._has_loop("https://example.com/a/b/c/d/e/f/g")

    def test_has_loop_normal_url(self):
        crawler = WebCrawler("https://example.com")
        assert not crawler._has_loop("https://example.com/docs/guide/intro")

    def test_normalize_url_strips_fragment_and_query(self):
        assert WebCrawler._normalize_url("https://example.com/page?foo=1#bar") == "https://example.com/page"


async def _collect(crawler: WebCrawler) -> list:
    """Collect all results from the async-generator crawl() into a list."""
    return [r async for r in crawler.crawl()]


@pytest.mark.rag
class TestWebCrawlerCrawl:
    """Integration-style tests using httpx_mock."""

    async def test_crawl_returns_page_content(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com",
            text="<html><head><title>Home</title></head><body><main><p>Welcome to Example.</p></main></body></html>",
            headers={"content-type": "text/html"},
        )
        crawler = WebCrawler("https://example.com", max_pages=1, rate_limit_seconds=0)
        results = await _collect(crawler)
        assert len(results) == 1
        assert results[0].title == "Home"
        assert "Welcome to Example" in results[0].text

    async def test_crawl_deduplicates_by_content_hash(self, httpx_mock):
        # Root links to /a and /b; both /a and /b have identical content.
        # Only one of them should be in the results.
        identical_body = "<html><head><title>Dupe</title></head><body><p>Identical content here.</p></body></html>"
        httpx_mock.add_response(
            url="https://example.com",
            text='<html><body><a href="/a">a</a><a href="/b">b</a><p>Root page text.</p></body></html>',
            headers={"content-type": "text/html"},
        )
        httpx_mock.add_response(
            url="https://example.com/a",
            text=identical_body,
            headers={"content-type": "text/html"},
        )
        httpx_mock.add_response(
            url="https://example.com/b",
            text=identical_body,
            headers={"content-type": "text/html"},
        )
        crawler = WebCrawler("https://example.com", max_pages=10, rate_limit_seconds=0)
        results = await _collect(crawler)
        texts = [r.text for r in results]
        # Root has unique content; /a and /b are deduped to one entry.
        assert len(texts) == len(set(texts))

    async def test_crawl_skips_non_html(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com",
            text='<html><body><a href="/doc.pdf">pdf</a><p>Page</p></body></html>',
            headers={"content-type": "text/html"},
        )
        httpx_mock.add_response(
            url="https://example.com/doc.pdf",
            content=b"%PDF-1.4",
            headers={"content-type": "application/pdf"},
        )
        crawler = WebCrawler("https://example.com", max_pages=5, rate_limit_seconds=0)
        results = await _collect(crawler)
        sources = [r.url for r in results]
        assert "https://example.com/doc.pdf" not in sources

    async def test_crawl_respects_max_pages(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com",
            text='<html><body><a href="/a">a</a><a href="/b">b</a><p>Root.</p></body></html>',
            headers={"content-type": "text/html"},
        )
        httpx_mock.add_response(
            url="https://example.com/a",
            text="<html><body><p>Page A content here.</p></body></html>",
            headers={"content-type": "text/html"},
        )
        crawler = WebCrawler("https://example.com", max_pages=2, rate_limit_seconds=0)
        results = await _collect(crawler)
        assert len(results) <= 2

    async def test_crawl_skips_loop_urls(self, httpx_mock):
        httpx_mock.add_response(
            url="https://example.com",
            text='<html><body><a href="/a/b/a/b/a/b">loop</a><p>Root.</p></body></html>',
            headers={"content-type": "text/html"},
        )
        crawler = WebCrawler("https://example.com", max_pages=5, rate_limit_seconds=0)
        results = await _collect(crawler)
        urls = [r.url for r in results]
        assert "https://example.com/a/b/a/b/a/b" not in urls

    async def test_crawl_skips_failed_requests(self, httpx_mock):
        import httpx as _httpx
        httpx_mock.add_exception(
            _httpx.ConnectError("connection refused"),
            url="https://example.com",
        )
        crawler = WebCrawler("https://example.com", max_pages=5, rate_limit_seconds=0)
        results = await _collect(crawler)
        assert results == []


# ---------------------------------------------------------------------------
# RAGDocsManager.ingest_web
# ---------------------------------------------------------------------------


async def _crawl_gen(*items):
    """Async generator helper that yields items one at a time (for mocking crawl())."""
    for item in items:
        yield item


@pytest.mark.rag
class TestIngestWeb:
    async def test_ingest_web_upserts_pages(self, docs_manager, mock_store):
        web_source = WebSource(
            name="test-web",
            base_url="https://example.com",
            max_pages=5,
        )
        page = MagicMock(url="https://example.com/page1", title="Page 1", text="Content of page one here.")
        mock_crawler_instance = MagicMock()
        mock_crawler_instance.crawl = lambda: _crawl_gen(page)

        with patch("deriva_mcp_core.rag.docs.WebCrawler", return_value=mock_crawler_instance):
            count = await docs_manager.ingest_web(web_source)

        assert count == 1
        assert len(mock_store.upserted) == 1

    async def test_ingest_web_skips_unchanged_content(self, docs_manager, mock_store, tmp_settings):
        from deriva_mcp_core.rag.web_crawler import CrawlResult
        import hashlib

        web_source = WebSource(name="test-web", base_url="https://example.com")
        text = "Unchanged content for the page."
        content_hash = hashlib.md5(text.encode()).hexdigest()
        cache_path = Path(tmp_settings.rag_dir) / "test-web_url_cache.json"
        cache_path.write_text(json.dumps({"https://example.com/page1": content_hash}))

        result = CrawlResult(url="https://example.com/page1", title="P1", text=text)
        mock_crawler_instance = MagicMock()
        mock_crawler_instance.crawl = lambda: _crawl_gen(result)

        with patch("deriva_mcp_core.rag.docs.WebCrawler", return_value=mock_crawler_instance):
            count = await docs_manager.ingest_web(web_source)

        assert count == 0
        assert mock_store.upserted == []

    async def test_ingest_web_force_reupserts(self, docs_manager, mock_store, tmp_settings):
        from deriva_mcp_core.rag.web_crawler import CrawlResult
        import hashlib

        web_source = WebSource(name="test-web", base_url="https://example.com")
        text = "Same content."
        content_hash = hashlib.md5(text.encode()).hexdigest()
        cache_path = Path(tmp_settings.rag_dir) / "test-web_url_cache.json"
        cache_path.write_text(json.dumps({"https://example.com/p": content_hash}))

        result = CrawlResult(url="https://example.com/p", title="P", text=text)
        mock_crawler_instance = MagicMock()
        mock_crawler_instance.crawl = lambda: _crawl_gen(result)

        with patch("deriva_mcp_core.rag.docs.WebCrawler", return_value=mock_crawler_instance):
            count = await docs_manager.ingest_web(web_source, force=True)

        assert count == 1

    def test_url_cache_roundtrip(self, docs_manager, tmp_settings):
        cache = {"https://example.com/page": "abc123hash"}
        docs_manager._save_url_cache("web-src", cache)
        loaded = docs_manager._load_url_cache("web-src")
        assert loaded == cache

    def test_url_cache_missing_returns_empty(self, docs_manager):
        assert docs_manager._load_url_cache("nonexistent") == {}


# ---------------------------------------------------------------------------
# RAGDocsManager.ingest_local
# ---------------------------------------------------------------------------


class TestIngestLocal:
    async def test_ingest_local_reads_md_files(self, docs_manager, mock_store, tmp_path):
        (tmp_path / "guide.md").write_text("# Guide\n\nSome content.", encoding="utf-8")
        src = LocalSource(name="local-docs", path=str(tmp_path))
        count = await docs_manager.ingest_local(src)
        assert count == 1
        assert len(mock_store.upserted) == 1

    async def test_ingest_local_skips_unchanged_mtime(self, docs_manager, mock_store, tmp_path, tmp_settings):
        f = tmp_path / "guide.md"
        f.write_text("# Guide\n\nContent.", encoding="utf-8")
        mtime = str(f.stat().st_mtime)
        cache_path = Path(tmp_settings.rag_dir) / "local-docs_mtime_cache.json"
        cache_path.write_text(json.dumps({"guide.md": mtime}))

        src = LocalSource(name="local-docs", path=str(tmp_path))
        count = await docs_manager.ingest_local(src)
        assert count == 0

    async def test_ingest_local_force_reupserts(self, docs_manager, mock_store, tmp_path, tmp_settings):
        f = tmp_path / "guide.md"
        f.write_text("# Guide\n\nContent.", encoding="utf-8")
        mtime = str(f.stat().st_mtime)
        cache_path = Path(tmp_settings.rag_dir) / "local-docs_mtime_cache.json"
        cache_path.write_text(json.dumps({"guide.md": mtime}))

        src = LocalSource(name="local-docs", path=str(tmp_path))
        count = await docs_manager.ingest_local(src, force=True)
        assert count == 1

    async def test_ingest_local_nonexistent_path(self, docs_manager, mock_store):
        src = LocalSource(name="missing", path="/no/such/path")
        count = await docs_manager.ingest_local(src)
        assert count == 0
        assert mock_store.upserted == []

    async def test_ingest_local_single_file(self, docs_manager, mock_store, tmp_path):
        f = tmp_path / "single.md"
        f.write_text("# Single\n\nJust one file.", encoding="utf-8")
        src = LocalSource(name="one-file", path=str(f))
        count = await docs_manager.ingest_local(src)
        assert count == 1

    async def test_ingest_local_glob_filter(self, docs_manager, mock_store, tmp_path):
        (tmp_path / "a.md").write_text("# A\n\nMarkdown.", encoding="utf-8")
        (tmp_path / "b.txt").write_text("Plain text file here.", encoding="utf-8")
        src = LocalSource(name="local-docs", path=str(tmp_path), glob="**/*.md")
        count = await docs_manager.ingest_local(src)
        assert count == 1  # only .md file

    def test_mtime_cache_roundtrip(self, docs_manager, tmp_settings):
        cache = {"guide.md": "1700000000.0"}
        docs_manager._save_mtime_cache("local-src", cache)
        loaded = docs_manager._load_mtime_cache("local-src")
        assert loaded == cache

    def test_mtime_cache_missing_returns_empty(self, docs_manager):
        assert docs_manager._load_mtime_cache("nonexistent") == {}