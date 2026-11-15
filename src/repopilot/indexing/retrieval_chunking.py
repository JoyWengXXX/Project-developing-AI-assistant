"""可追溯至快照原文的有界片段切分。"""

from bisect import bisect_right
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TextChunk:
    start: int
    end: int
    start_line: int
    end_line: int
    split_kind: Literal["structure", "paragraph", "text"]


def chunk_text(
    content: str, *, max_bytes: int, structure_lines: list[int] | None = None
) -> list[TextChunk]:
    """依 UTF-8 bytes 限制切分；offset 為 Python 字元索引，end 不含在內。"""
    if max_bytes < 4:
        raise ValueError("max_bytes must be at least 4.")

    line_starts = [0] + [i + 1 for i, char in enumerate(content) if char == "\n"]
    structure_offsets: set[int] = set()
    for line in structure_lines or []:
        if line < 1 or line > len(line_starts):
            raise ValueError("A structure boundary is outside the source.")
        structure_offsets.add(line_starts[line - 1])

    paragraph_offsets = [
        line_starts[i + 1]
        for i in range(len(line_starts) - 1)
        if not content[line_starts[i] : line_starts[i + 1]].strip()
    ]
    paragraph_starts = set(paragraph_offsets)
    boundaries: list[tuple[list[int], Literal["structure", "paragraph", "text"]]] = [
        (sorted(structure_offsets), "structure"),
        (paragraph_offsets, "paragraph"),
        (line_starts, "text"),
    ]
    chunks: list[TextChunk] = []
    start = 0
    while start < len(content):
        end = start
        size = 0
        while end < len(content):
            char_size = len(content[end].encode("utf-8"))
            if size + char_size > max_bytes:
                break
            size += char_size
            end += 1

        kind: Literal["structure", "paragraph", "text"] = "text"
        if end < len(content):
            for offsets, candidate_kind in boundaries:
                index = bisect_right(offsets, end) - 1
                if index >= 0 and offsets[index] > start:
                    end = offsets[index]
                    kind = candidate_kind
                    break
        if start in structure_offsets and kind == "text":
            kind = "structure"
        elif start in paragraph_starts and kind == "text":
            kind = "paragraph"
        chunks.append(
            TextChunk(
                start=start,
                end=end,
                start_line=bisect_right(line_starts, start),
                end_line=bisect_right(line_starts, end - 1),
                split_kind=kind,
            )
        )
        start = end
    return chunks
