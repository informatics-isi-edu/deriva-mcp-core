from __future__ import annotations

"""Unit tests for the Markdown chunker."""

from deriva_mcp_core.rag.chunker import (
    _collect_paragraphs,
    _last_sentence,
    _split_at_headings,
    chunk_markdown,
)


class TestSplitAtHeadings:
    def test_preamble_only(self):
        text = "Some intro text.\nAnother line."
        sections = _split_at_headings(text)
        assert len(sections) == 1
        level, heading, body = sections[0]
        assert level == 0
        assert heading == ""
        assert "intro text" in body

    def test_single_h2(self):
        text = "## My Section\nBody text here."
        sections = _split_at_headings(text)
        assert len(sections) == 1
        level, heading, body = sections[0]
        assert level == 2
        assert "My Section" in heading
        assert "Body text" in body

    def test_heading_does_not_split_inside_fence(self):
        text = "```\n## fake heading\n```\n## Real Heading\nReal body."
        sections = _split_at_headings(text)
        # Only "Real Heading" should be a section boundary
        headings = [h for _, h, _ in sections if h]
        assert len(headings) == 1
        assert "Real Heading" in headings[0]

    def test_h1_and_h2(self):
        text = "# Title\nIntro.\n## Section\nSection body."
        sections = _split_at_headings(text)
        assert len(sections) == 2
        assert sections[0][0] == 1
        assert sections[1][0] == 2

    def test_skips_empty_sections(self):
        text = "## A\n## B\nContent."
        sections = _split_at_headings(text)
        # Section A has no body, section B has content. Empty sections filtered.
        assert any("B" in h for _, h, _ in sections)

    def test_tilde_fence(self):
        text = "~~~\n## not a heading\n~~~\n## Real\nbody"
        sections = _split_at_headings(text)
        headings = [h for _, h, _ in sections if h]
        assert all("Real" in h for h in headings)


class TestChunkMarkdown:
    def test_small_doc_single_chunk(self):
        text = "## Intro\nShort paragraph."
        chunks = chunk_markdown(text, source="src", doc_type="user-guide")
        assert len(chunks) >= 1
        assert chunks[0].source == "src"
        assert chunks[0].doc_type == "user-guide"

    def test_heading_in_chunk_text(self):
        text = "## My Section\nSome content here."
        chunks = chunk_markdown(text, source="s", doc_type="d")
        assert any("My Section" in c.text for c in chunks)

    def test_section_heading_metadata(self):
        text = "## Alpha\nContent A.\n## Beta\nContent B."
        chunks = chunk_markdown(text, source="s", doc_type="d")
        headings = {c.section_heading for c in chunks}
        assert "Alpha" in headings
        assert "Beta" in headings

    def test_heading_hierarchy(self):
        text = "## Parent\nIntro.\n### Child\nDetail."
        chunks = chunk_markdown(text, source="s", doc_type="d")
        child_chunks = [c for c in chunks if c.section_heading == "Child"]
        assert child_chunks, "Expected chunks for ### Child"
        assert "Parent" in child_chunks[0].heading_hierarchy

    def test_chunk_index_sequential(self):
        text = "## A\nBody.\n## B\nBody."
        chunks = chunk_markdown(text, source="s", doc_type="d")
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_large_section_split(self):
        # A section with >target_words words should produce multiple chunks
        words = " ".join(["word"] * 1000)
        text = f"## Big Section\n{words}"
        chunks = chunk_markdown(text, source="s", doc_type="d", target_words=200)
        assert len(chunks) > 1

    def test_no_empty_chunks(self):
        text = "## A\n\n## B\nContent."
        chunks = chunk_markdown(text, source="s", doc_type="d")
        assert all(c.text.strip() for c in chunks)

    def test_code_block_not_split(self):
        # A code block alone in a section should stay as one chunk
        code = "\n".join(["```python"] + ["x = 1"] * 50 + ["```"])
        text = f"## Code\n{code}"
        chunks = chunk_markdown(text, source="s", doc_type="d", target_words=10)
        # The code block should appear intact in exactly one chunk
        code_chunks = [c for c in chunks if "```python" in c.text]
        assert len(code_chunks) == 1


class TestCollectParagraphs:
    def test_splits_on_blank_line(self):
        text = "Para one.\n\nPara two."
        paras = _collect_paragraphs(text)
        assert len(paras) == 2

    def test_code_block_is_one_paragraph(self):
        text = "Before.\n\n```\ncode here\nmore code\n```\n\nAfter."
        paras = _collect_paragraphs(text)
        code_paras = [p for p in paras if "code here" in p]
        assert len(code_paras) == 1
        assert "more code" in code_paras[0]


class TestLastSentence:
    def test_simple(self):
        text = "First sentence. Second sentence. Third sentence."
        result = _last_sentence(text)
        assert "Third" in result

    def test_single_sentence(self):
        text = "Just one sentence."
        result = _last_sentence(text)
        assert result  # not empty

    def test_short_text_returned_as_is(self):
        text = "Short."
        result = _last_sentence(text)
        assert result == "Short."
