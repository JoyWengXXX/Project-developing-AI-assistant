import pytest

from repopilot.search_query import keyword_terms


def test_keyword_terms_are_case_insensitive_and_unique() -> None:
    assert keyword_terms("Widget widget sort_file 中文") == ["widget", "sort_file", "中文"]


@pytest.mark.parametrize("value", ["++", "\x00abc", " ".join(f"term{i}" for i in range(17))])
def test_invalid_or_unbounded_keyword_queries_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        keyword_terms(value)
