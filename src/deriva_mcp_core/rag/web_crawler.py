from __future__ import annotations

"""Async BFS web crawler for documentation ingestion.

Crawls a public website starting from a base URL, extracts text content
from HTML pages using beautifulsoup4, and returns a list of CrawlResult
objects suitable for chunking and upserting into the vector store.

Requires the 'rag' optional dependency group (beautifulsoup4).

Key behaviours:
- BFS traversal from base_url, capped at max_pages.
- Deduplication by MD5 hash of extracted text -- multiple URLs with
  identical content (common in Chaise facet URLs) produce one result.
- Path loop detection: skips URLs where any path segment appears more
  than twice, or where path depth exceeds max_depth (default 10).
- Rate limiting: configurable sleep between requests (default 1.0 s).
- Domain scoping: only follows links in allowed_domains (defaults to
  base_url domain).
- Optional path prefix filter: only indexes URLs under include_path_prefix.
- Fragment and query parameters stripped from discovered links before
  deduplication (query params often generate Chaise-style duplicate pages).
"""

import asyncio
import hashlib
import logging
import re
from collections import Counter, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

_USER_AGENT = "deriva-mcp-crawler/1.0 (+https://github.com/informatics-isi-edu/deriva-mcp-core)"

# CSS selectors tried in order when extracting main page content.
# Falls back to <body> if none match.
_CONTENT_SELECTORS = [
    "main",
    "article",
    ".content",
    "#content",
    ".main-content",
    'div[role="main"]',
]


@dataclass
class CrawlResult:
    """Extracted content from one crawled page."""

    url: str
    title: str
    text: str  # extracted plain text (whitespace-normalised)


class WebCrawler:
    """Async BFS crawler for a public website.

    Args:
        base_url: Starting URL and crawl root.
        max_pages: Maximum number of unique pages to collect.
        allowed_domains: Domains whose links may be followed. Defaults to
            the domain of base_url.
        include_path_prefix: If non-empty, only URLs whose path starts with
            this prefix are indexed (links outside the prefix are still
            followed to discover more URLs within it).
        rate_limit_seconds: Seconds to sleep between HTTP requests.
        max_depth: Maximum URL path segment count before the URL is
            considered a loop and skipped.
    """

    def __init__(
        self,
        base_url: str,
        max_pages: int = 200,
        allowed_domains: list[str] | None = None,
        include_path_prefix: str = "",
        rate_limit_seconds: float = 1.0,
        max_depth: int = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_pages = max_pages
        self._base_domain = urlparse(base_url).netloc
        self._allowed_domains: set[str] = set(allowed_domains) if allowed_domains else {self._base_domain}
        self._include_path_prefix = include_path_prefix
        self._rate_limit = rate_limit_seconds
        self._max_depth = max_depth

    # ------------------------------------------------------------------
    # URL filtering
    # ------------------------------------------------------------------

    def _is_crawlable(self, url: str) -> bool:
        """Return True if the URL belongs to an allowed domain."""
        return urlparse(url).netloc in self._allowed_domains

    def _is_indexable(self, url: str) -> bool:
        """Return True if the URL should produce an indexed chunk.

        A URL is indexable if it passes domain and path prefix filters.
        """
        if not self._is_crawlable(url):
            return False
        if self._include_path_prefix:
            return urlparse(url).path.startswith(self._include_path_prefix)
        return True

    def _has_loop(self, url: str) -> bool:
        """Return True if the URL path looks like a crawler loop."""
        segments = [s for s in urlparse(url).path.split("/") if s]
        if len(segments) > self._max_depth:
            return True
        counts = Counter(segments)
        return any(c > 2 for c in counts.values())

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip fragment and query string; return scheme://netloc/path."""
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_links(soup: Any, page_url: str) -> list[str]:
        links = []
        for tag in soup.find_all("a", href=True):
            full = urljoin(page_url, tag["href"])
            if full.startswith("http"):
                links.append(WebCrawler._normalize_url(full))
        return links

    @staticmethod
    def _extract_content(soup: Any, url: str) -> CrawlResult:
        title_tag = soup.find("title")
        title = title_tag.get_text().strip() if title_tag else ""

        # Remove noise elements before extracting text
        for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        content_el = None
        for selector in _CONTENT_SELECTORS:
            content_el = soup.select_one(selector)
            if content_el:
                break
        if content_el is None:
            content_el = soup.find("body")

        text = content_el.get_text(separator="\n") if content_el else ""
        # Collapse runs of blank lines
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        return CrawlResult(url=url, title=title, text=text)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    async def crawl(self) -> AsyncIterator[CrawlResult]:
        """BFS crawl from base_url. Yields up to max_pages results one at a time.

        Only URLs passing the domain and path prefix filters are yielded.
        All same-domain URLs are followed for link discovery even if they
        would not produce an indexed chunk themselves.

        Pages are yielded as they are fetched so callers can process and
        store each page immediately without accumulating all results in memory.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "beautifulsoup4 is required for WebCrawler. "
                "Install the 'rag' extras: pip install deriva-mcp-core[rag]"
            ) from exc

        visited: set[str] = set()
        content_hashes: set[str] = set()
        queue: deque[str] = deque([self._base_url])
        collected = 0

        async with httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
            follow_redirects=True,
        ) as client:
            while queue and collected < self._max_pages:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)

                if not self._is_crawlable(url):
                    continue
                if self._has_loop(url):
                    logger.debug("WebCrawler: skipping loop URL %s", url)
                    continue

                resp = None
                for attempt in range(3):
                    try:
                        resp = await client.get(url)
                        break
                    except httpx.RemoteProtocolError as exc:
                        if attempt < 2:
                            wait = 2 ** attempt
                            logger.warning(
                                "WebCrawler: server disconnected for %s (attempt %d/3), retrying in %ds: %s",
                                url, attempt + 1, wait, exc,
                            )
                            await asyncio.sleep(wait)
                        else:
                            logger.warning("WebCrawler: giving up on %s after 3 attempts: %s", url, exc)
                    except Exception:
                        logger.debug("WebCrawler: fetch failed for %s", url, exc_info=True)
                        break
                if resp is None:
                    continue

                if self._rate_limit > 0:
                    await asyncio.sleep(self._rate_limit)

                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                # Always enqueue links (even from non-indexable pages)
                for link in self._extract_links(soup, url):
                    if link not in visited:
                        queue.append(link)

                if not self._is_indexable(url):
                    continue

                result = self._extract_content(soup, url)
                if not result.text:
                    continue

                # Dedup by content hash
                content_hash = hashlib.md5(result.text.encode()).hexdigest()  # noqa: S324
                if content_hash in content_hashes:
                    continue
                content_hashes.add(content_hash)

                collected += 1
                yield result

        logger.info("WebCrawler: %d pages collected from %s (%d visited)",
                    collected, self._base_url, len(visited))