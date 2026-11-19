"""片段向量化的取消邊界。"""

import pytest

from repopilot.services.retrieval_build import SourceChunk, vectorize_chunk


def test_cancellation_before_embedding_does_not_send_content() -> None:
    calls: list[str] = []
    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            raise RuntimeError("cancelled")

    def embed(value: str) -> list[float]:
        calls.append(value)
        return [1.0] + [0.0] * 767

    with pytest.raises(RuntimeError, match="cancelled"):
        list(
            vectorize_chunk(
                SourceChunk(1, "fake content"),
                count_tokens=lambda value: 1,
                embed=embed,
                checkpoint=checkpoint,
            )
        )
    assert calls == []
