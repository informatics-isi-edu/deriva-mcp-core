from __future__ import annotations

"""GitHub repository crawler for documentation ingestion.

Uses the GitHub Trees API (GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1)
to discover all .md files under a configured path prefix. No GitHub authentication
is required for public repositories.

Fetches file content from GitHub raw URLs. Stores per-file SHA for incremental
updates -- only files whose SHA has changed since the last crawl are re-fetched.
This makes rag_update_docs fast even for large documentation sets.
"""

import logging
import httpx
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_GITHUB_RAW = "https://raw.githubusercontent.com"


@dataclass
class FileEntry:
    """Metadata for a single file discovered in a GitHub repository tree."""

    path: str  # repository-relative path (e.g., "docs/guide.md")
    sha: str  # Git blob SHA for change detection


class GitHubCrawler:
    """Crawls a GitHub repository for Markdown files using the Trees API.

    No authentication is required for public repositories.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        branch: str = "master",
        path_prefix: str = "docs/",
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._branch = branch
        # Normalise: no leading slash, with trailing slash for prefix matching
        self._path_prefix = path_prefix.lstrip("/")
        if self._path_prefix and not self._path_prefix.endswith("/"):
            self._path_prefix += "/"

    async def list_files(self) -> list[FileEntry]:
        """Return all .md files under path_prefix using the GitHub Trees API.

        Raises:
            httpx.HTTPStatusError: If the GitHub API returns an error.
        """
        url = f"{_GITHUB_API}/repos/{self._owner}/{self._repo}/git/trees/{self._branch}?recursive=1"
        async with httpx.AsyncClient(
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=30.0,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        entries: list[FileEntry] = []
        for item in data.get("tree", []):
            path: str = item.get("path", "")
            if item.get("type") != "blob":
                continue
            if not path.lower().endswith(".md"):
                continue
            if self._path_prefix and not path.startswith(self._path_prefix):
                continue
            entries.append(FileEntry(path=path, sha=item.get("sha", "")))

        logger.debug(
            "GitHubCrawler: %d .md files under %s/%s/%s",
            len(entries),
            self._owner,
            self._repo,
            self._path_prefix,
        )
        return entries

    async def fetch_content(self, entry: FileEntry) -> str:
        """Fetch the raw text content of a file.

        Args:
            entry: FileEntry from list_files().

        Returns:
            UTF-8 decoded file content.

        Raises:
            httpx.HTTPStatusError: If GitHub returns an error.
        """
        url = f"{_GITHUB_RAW}/{self._owner}/{self._repo}/{self._branch}/{entry.path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
