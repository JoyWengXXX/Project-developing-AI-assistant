"""內容搜尋的純邏輯：分批規則與片段視窗（切片 8，規格 8.2、8.5）。"""

from repopilot.services.search import (
    TEXT_BATCH_FIRST_FILES,
    TEXT_BATCH_MAX_BYTES,
    TEXT_BATCH_MAX_FILES,
    _text_batches,
    make_snippet,
)


def _candidates(sizes: list[int | None]) -> list[tuple[int, str, int | None]]:
    return [(number, f"f{number}", size) for number, size in enumerate(sizes)]


def test_batches_start_small_double_up_to_the_cap_and_lose_no_candidate() -> None:
    candidates = _candidates([10] * 2_000)

    batches = list(_text_batches(candidates))

    assert [len(batch) for batch in batches[:4]] == [20, 40, 80, 160]
    assert max(len(batch) for batch in batches) == TEXT_BATCH_MAX_FILES
    assert [row[0] for batch in batches for row in batch] == list(range(2_000))
    assert len(batches[0]) == TEXT_BATCH_FIRST_FILES


def test_a_batch_never_exceeds_the_byte_budget_unless_a_single_file_does() -> None:
    sizes = [TEXT_BATCH_MAX_BYTES // 3] * 7 + [TEXT_BATCH_MAX_BYTES * 2] + [1, None]

    batches = list(_text_batches(_candidates(sizes)))

    assert [row[0] for batch in batches for row in batch] == list(range(10))
    for batch in batches:
        total = sum(row[2] or 0 for row in batch)
        assert total <= TEXT_BATCH_MAX_BYTES or len(batch) == 1


def test_there_are_no_batches_without_candidates() -> None:
    assert list(_text_batches([])) == []


def test_a_short_line_is_returned_whole() -> None:
    assert make_snippet("hello world", 6) == ("hello world", False, 0, 6)


def test_a_long_line_gets_a_window_that_starts_at_most_100_characters_before_the_match() -> None:
    line = "a" * 500 + "MATCH" + "b" * 500

    snippet, truncated, snippet_offset, match_offset = make_snippet(line, 500)

    assert (truncated, snippet_offset, match_offset) == (True, 400, 100)
    assert len(snippet) == 300 and snippet[match_offset : match_offset + 5] == "MATCH"


def test_a_match_near_the_start_of_a_long_line_starts_the_window_at_zero() -> None:
    snippet, truncated, snippet_offset, match_offset = make_snippet("x" * 5 + "M" + "y" * 600, 5)

    assert (truncated, snippet_offset, match_offset, len(snippet)) == (True, 0, 5, 300)
