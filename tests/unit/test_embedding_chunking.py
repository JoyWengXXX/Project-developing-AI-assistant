"""模型 token 限制不得造成來源內容遺漏。"""

from itertools import pairwise

import pytest

from repopilot.embedding.chunking import split_for_embedding


@pytest.mark.parametrize("content", ["", "abc", "繁體中文🙂\nlong" * 20])
def test_segments_cover_unicode_source_and_respect_limits(content: str) -> None:
    segments = split_for_embedding(
        content, count_tokens=lambda text: len(text) + 2, max_tokens=9, max_bytes=16
    )
    assert "".join(content[segment.start : segment.end] for segment in segments) == content
    assert all(left.end == right.start for left, right in pairwise(segments))
    for segment in segments:
        text = content[segment.start : segment.end]
        assert len(text.encode("utf-8")) <= 16
        assert segment.token_count == len(text) + 2 <= 9


def test_nonmonotonic_token_counts_do_not_lead_to_an_oversized_segment() -> None:
    def count_tokens(text: str) -> int:
        return 10 if len(text) == 3 else len(text)

    content = "abcdef"
    segments = split_for_embedding(content, count_tokens=count_tokens, max_tokens=3, max_bytes=16)
    assert "".join(content[segment.start : segment.end] for segment in segments) == content
    assert all(count_tokens(content[segment.start : segment.end]) <= 3 for segment in segments)


@pytest.mark.parametrize(("length", "segment_count"), [(510, 1), (511, 2)])
def test_splits_only_when_token_limit_is_exceeded(length: int, segment_count: int) -> None:
    segments = split_for_embedding(
        "x" * length, count_tokens=lambda text: len(text) + 2, max_tokens=512, max_bytes=1024
    )
    assert len(segments) == segment_count


def test_tokenizer_failure_is_not_silently_ignored() -> None:
    def fail(_: str) -> int:
        raise RuntimeError("controlled tokenizer failure")

    with pytest.raises(RuntimeError, match="controlled tokenizer failure"):
        split_for_embedding("abc", count_tokens=fail, max_tokens=5, max_bytes=8)


def test_rejects_a_character_that_cannot_fit() -> None:
    with pytest.raises(ValueError, match="token"):
        split_for_embedding("x", count_tokens=lambda _: 10, max_tokens=5, max_bytes=8)


@pytest.mark.parametrize("count", [0, -1, True])
def test_rejects_invalid_tokenizer_results(count: int) -> None:
    with pytest.raises(ValueError, match="token"):
        split_for_embedding("x", count_tokens=lambda _: count, max_tokens=5, max_bytes=8)
