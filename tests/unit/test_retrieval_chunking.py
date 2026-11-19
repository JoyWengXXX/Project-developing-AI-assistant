"""片段切分必須保留每個字元與來源行號。"""

from itertools import pairwise

import pytest

from repopilot.indexing.retrieval_chunking import chunk_text


@pytest.mark.parametrize(
    "content",
    [
        "",
        "first\nsecond\n",
        "繁體中文🙂" * 30,
        "x" * 150,
        "a\r\n\r\nb\n\nc\n",
    ],
)
def test_chunks_preserve_source_and_respect_utf8_byte_limit(content: str) -> None:
    chunks = chunk_text(content, max_bytes=24)

    assert "".join(content[chunk.start : chunk.end] for chunk in chunks) == content
    if chunks:
        assert chunks[0].start == 0 and chunks[-1].end == len(content)
        assert all(left.end == right.start for left, right in pairwise(chunks))
    for chunk in chunks:
        assert 0 < len(content[chunk.start : chunk.end].encode("utf-8")) <= 24
        assert chunk.start_line == content.count("\n", 0, chunk.start) + 1
        assert chunk.end_line == content.count("\n", 0, chunk.end - 1) + 1


def test_prefers_structure_boundaries_without_losing_surrounding_text() -> None:
    content = "header\nfunction a\nbody\nfunction b\nbody\n"
    chunks = chunk_text(content, max_bytes=28, structure_lines=[2, 4])

    assert [content[chunk.start : chunk.end] for chunk in chunks] == [
        "header\nfunction a\nbody\n",
        "function b\nbody\n",
    ]
    assert chunks[0].split_kind == "structure"


def test_rejects_byte_limit_smaller_than_utf8_character() -> None:
    with pytest.raises(ValueError):
        chunk_text("🙂", max_bytes=3)


def test_rejects_structure_boundary_outside_source() -> None:
    with pytest.raises(ValueError):
        chunk_text("line\n", max_bytes=24, structure_lines=[3])


def test_paragraph_boundaries_are_preferred_when_no_structure_is_available() -> None:
    content = "first paragraph\n\nsecond paragraph"
    chunks = chunk_text(content, max_bytes=24)
    assert [content[chunk.start : chunk.end] for chunk in chunks] == [
        "first paragraph\n\n",
        "second paragraph",
    ]
    assert all(chunk.split_kind == "paragraph" for chunk in chunks)
