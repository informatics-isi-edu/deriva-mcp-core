"""GitHub repository crawler for documentation ingestion.

Uses the GitHub Trees API (GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1)
to discover all .md files under a configured path prefix. No GitHub authentication
is required for public repositories.

Fetches file content from GitHub raw URLs. Stores per-file SHA for incremental
updates -- only files whose SHA has changed since the last crawl are re-fetched.
This makes rag_update_docs fast even for large documentation sets.
"""

from __future__ import annotations

# TODO (Phase 4): implement crawl_repo(), fetch_file_content()
