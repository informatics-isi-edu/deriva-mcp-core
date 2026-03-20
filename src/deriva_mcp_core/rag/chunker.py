from __future__ import annotations

"""Markdown-aware document chunker.

Splits Markdown content at heading boundaries (## and ### only) while
preserving heading hierarchy context in each chunk's metadata. Never splits
inside fenced code blocks.

Target chunk size is approximately 800 tokens (approximated by word count).
A one-sentence overlap is preserved at chunk boundaries for context continuity.

Returns a list of Chunk dataclasses (defined in store.py) with text and
metadata fields suitable for upsert into any VectorStore implementation.
"""

import re
from .store import Chunk

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)")


def chunk_markdown(
    text: str,
    source: str,
    doc_type: str,
    target_words: int = 800,
) -> list[Chunk]:
    """Split Markdown into Chunk objects at heading boundaries.

    Splits at ## and ### heading boundaries only. Never splits inside fenced
    code blocks. Target chunk size is target_words words (approximated).
    A one-sentence overlap is added at chunk boundaries for context continuity.

    Args:
        text: Raw Markdown text.
        source: Source identifier stored in each Chunk.
        doc_type: Document type tag (e.g. "user-guide", "schema").
        target_words: Approximate target word count per chunk.

    Returns:
        List of Chunk objects with section heading metadata.
    """
    raw_sections = _split_at_headings(text)
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, heading_text)

    for level, heading_line, body in raw_sections:
        if heading_line:
            # Pop ancestors at the same or deeper level before pushing the new heading
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            m = _HEADING_RE.match(heading_line)
            heading_text = m.group(2).strip() if m else heading_line.lstrip("#").strip()
            heading_stack.append((level, heading_text))

        hierarchy = [h for _, h in heading_stack[:-1]]
        section_heading = heading_stack[-1][1] if heading_stack else ""

        for sub in _split_body(body, target_words):
            full_text = (f"{heading_line}\n\n{sub}" if heading_line else sub).strip()
            if not full_text:
                continue
            chunks.append(
                Chunk(
                    text=full_text,
                    source=source,
                    doc_type=doc_type,
                    section_heading=section_heading,
                    heading_hierarchy=list(hierarchy),
                    chunk_index=len(chunks),
                )
            )

    return chunks


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fence_open_char(line: str) -> str | None:
    """Return '`' or '~' if line opens a fenced code block, else None."""
    stripped = line.strip()
    if stripped.startswith("```"):
        return "`"
    if stripped.startswith("~~~"):
        return "~"
    return None


def _split_at_headings(text: str) -> list[tuple[int, str, str]]:
    """Split Markdown text into (level, heading_line, body) sections.

    The first section may have level=0 and heading_line="" for preamble text
    that precedes the first heading. Headings inside fenced code blocks are
    ignored.
    """
    results: list[tuple[int, str, str]] = []
    current_level = 0
    current_heading = ""
    current_body: list[str] = []
    fence_char: str | None = None

    for line in text.split("\n"):
        fc = _fence_open_char(line)
        if fence_char is None:
            if fc:
                fence_char = fc
                current_body.append(line)
                continue
            m = _HEADING_RE.match(line)
            if m:
                results.append((current_level, current_heading, "\n".join(current_body)))
                current_level = len(m.group(1))
                current_heading = line.rstrip()
                current_body = []
            else:
                current_body.append(line)
        else:
            current_body.append(line)
            stripped = line.strip()
            if stripped.startswith(fence_char * 3):
                fence_char = None

    results.append((current_level, current_heading, "\n".join(current_body)))
    return [(lvl, hdr, body) for lvl, hdr, body in results if hdr or body.strip()]


def _split_body(body: str, target_words: int) -> list[str]:
    """Split a section body into word-count-bounded sub-chunks.

    Never splits inside a fenced code block. Adds a one-sentence overlap at
    each boundary. Falls back to word-boundary splitting for oversized paragraphs
    that cannot be split at blank-line boundaries.
    """
    if not body.strip():
        return []
    if len(body.split()) <= target_words:
        return [body]

    paragraphs = _collect_paragraphs(body)
    # Expand oversized non-code paragraphs into word-count chunks
    expanded: list[str] = []
    for para in paragraphs:
        expanded.extend(_split_para_by_words(para, target_words))

    result: list[str] = []
    current: list[str] = []
    current_words = 0

    for para in expanded:
        pw = len(para.split())
        if current and current_words + pw > target_words:
            result.append("\n\n".join(current))
            overlap = _last_sentence(current[-1])
            current = [overlap] if overlap else []
            current_words = len(overlap.split()) if overlap else 0
        current.append(para)
        current_words += pw

    if current:
        result.append("\n\n".join(current))
    return result


def _split_para_by_words(para: str, target_words: int) -> list[str]:
    """Split an oversized paragraph at word boundaries. Code blocks are atomic."""
    stripped = para.strip()
    if stripped.startswith("```") or stripped.startswith("~~~"):
        return [para]  # never split code blocks
    words = para.split()
    if len(words) <= target_words:
        return [para]
    result = []
    for start in range(0, len(words), target_words):
        result.append(" ".join(words[start : start + target_words]))
    return result


def _collect_paragraphs(text: str) -> list[str]:
    """Split text into paragraphs separated by blank lines, keeping code blocks atomic."""
    result: list[str] = []
    lines = text.split("\n")
    current: list[str] = []
    fence_char: str | None = None

    for line in lines:
        fc = _fence_open_char(line)
        if fence_char is None:
            if fc:
                fence_char = fc
                current.append(line)
            elif line.strip() == "" and current:
                result.append("\n".join(current))
                current = []
            else:
                current.append(line)
        else:
            current.append(line)
            if line.strip().startswith(fence_char * 3):
                fence_char = None

    if current:
        result.append("\n".join(current))
    return [p for p in result if p.strip()]


def _last_sentence(text: str) -> str:
    """Extract the last sentence from text for chunk overlap.

    Finds the last sentence separator ('. ', '! ', '? ') and returns everything
    after it. Falls back to the last 200 characters for very long texts with no
    sentence separators.
    """
    text = text.strip()
    last_sep = -1
    for sep in (". ", "! ", "? "):
        idx = text.rfind(sep)
        if idx > last_sep:
            last_sep = idx
    if last_sep >= 0:
        return text[last_sep + 2 :].strip()
    return text[-200:] if len(text) > 200 else text
