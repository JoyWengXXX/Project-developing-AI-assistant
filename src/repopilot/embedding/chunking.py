"""依 tokenizer 將來源切成不重疊的片段。"""

from collections.abc import Callable
from dataclasses import dataclass

from repopilot.indexing.retrieval_chunking import chunk_text


@dataclass(frozen=True)
class EmbeddingSegment:
    start: int
    end: int
    token_count: int


def split_for_embedding(
    content: str, *, count_tokens: Callable[[str], int], max_tokens: int, max_bytes: int
) -> list[EmbeddingSegment]:
    """回傳來源字元半開區間；每段均以 tokenizer 實測，不假設 token 數單調。"""
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive.")
    chunks = chunk_text(content, max_bytes=max_bytes)
    pending = [(chunk.start, chunk.end) for chunk in reversed(chunks)]
    segments: list[EmbeddingSegment] = []
    while pending:
        start, end = pending.pop()
        token_count = count_tokens(content[start:end])
        if type(token_count) is not int or token_count < 1:
            raise ValueError("The tokenizer returned an invalid token count.")
        if token_count <= max_tokens:
            segments.append(EmbeddingSegment(start, end, token_count))
            continue
        if end - start == 1:
            raise ValueError("A single character exceeds the model token limit.")
        midpoint = start + (end - start) // 2
        pending.append((midpoint, end))
        pending.append((start, midpoint))
    return segments
