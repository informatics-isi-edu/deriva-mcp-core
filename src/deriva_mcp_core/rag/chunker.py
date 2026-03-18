"""Markdown-aware document chunker.

Splits Markdown content at heading boundaries (## and ### only) while
preserving heading hierarchy context in each chunk's metadata. Never splits
inside fenced code blocks.

Target chunk size is approximately 800 tokens (approximated by word count).
A one-sentence overlap is preserved at chunk boundaries for context continuity.

Returns a list of Chunk dataclasses (defined in store.py) with text and
metadata fields suitable for upsert into any VectorStore implementation.
"""

from __future__ import annotations

# TODO (Phase 4): implement chunk_markdown()
