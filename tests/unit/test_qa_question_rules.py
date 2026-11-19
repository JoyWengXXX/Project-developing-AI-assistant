"""問答流程的純規則：檢索查詢、輸入估計上限與提問請求驗證。"""

import pytest
from pydantic import ValidationError

from repopilot.api.schemas import QaQuestionCreate
from repopilot.qa.context import ContextLimits, RetrievedChunk, build_context
from repopilot.qa.prompts import build_user_message
from repopilot.qa.question_rules import (
    estimate_request_input_tokens,
    input_tokens_upper_bound,
    retrieval_query,
)
from repopilot.search_query import keyword_terms

SHA = "a" * 40


def test_retrieval_query_keeps_all_terms_in_order_when_few() -> None:
    assert retrieval_query("How does the SSO login work?") == "How does the SSO login work"


def test_retrieval_query_dedupes_case_insensitively() -> None:
    assert retrieval_query("login Login LOGIN flow") == "login flow"


def test_retrieval_query_keeps_the_longest_sixteen_terms_in_original_order() -> None:
    words = [f"w{n}" for n in range(10)] + [f"longerword{n}" for n in range(16)]
    query = retrieval_query(" ".join(words))

    assert query.split() == [f"longerword{n}" for n in range(16)]
    assert len(keyword_terms(query)) == 16


def test_retrieval_query_result_is_always_a_valid_keyword_query() -> None:
    question = " ".join(f"term{n}" for n in range(200))

    assert len(keyword_terms(retrieval_query(question))) == 16


def test_retrieval_query_supports_cjk_and_mixed_questions() -> None:
    assert retrieval_query("SSO 登入怎麼運作？") == "SSO 登入怎麼運作"


def test_retrieval_query_is_bounded_to_512_characters() -> None:
    question = " ".join("x" * 100 + str(n) for n in range(16))

    assert len(retrieval_query(question)) <= 512


def test_retrieval_query_truncates_a_single_oversized_word() -> None:
    assert len(retrieval_query("a" * 2000)) == 512


@pytest.mark.parametrize("question", ["?!", "   ", "。。。"])
def test_retrieval_query_is_empty_without_words(question: str) -> None:
    assert retrieval_query(question) == ""


def test_input_upper_bound_covers_a_full_context_request() -> None:
    limits = ContextLimits()
    question = "How does it work? " * 100
    chunks = [
        RetrievedChunk(
            chunk_id=n,
            repo="demo",
            snapshot_id=1,
            commit_sha=SHA,
            path=f"src/file{n}.py",
            start_line=1,
            end_line=200,
            text="\n".join(f"line {n} {i} " + "x" * 40 for i in range(200)),
        )
        for n in range(1, 60)
    ]
    context = build_context(chunks, limits)
    assert context.estimated_tokens > limits.max_input_tokens * 0.9  # 確實接近預算上限

    actual = estimate_request_input_tokens(build_user_message(question.strip(), context))

    assert actual <= input_tokens_upper_bound(question.strip(), limits)


def test_input_upper_bound_grows_with_the_question() -> None:
    assert input_tokens_upper_bound("a" * 900) > input_tokens_upper_bound("a")


def test_create_request_accepts_a_minimal_body_and_strips_the_question() -> None:
    body = QaQuestionCreate.model_validate({"snapshot_ids": [1, 2], "question": "  why?  "})

    assert body.question == "why?"
    assert body.max_tokens is None


@pytest.mark.parametrize(
    "body",
    [
        {"snapshot_ids": [], "question": "q"},
        {"snapshot_ids": [1, 1], "question": "q"},
        {"snapshot_ids": list(range(1, 22)), "question": "q"},
        {"snapshot_ids": [0], "question": "q"},
        {"snapshot_ids": [True], "question": "q"},
        {"snapshot_ids": [1], "question": ""},
        {"snapshot_ids": [1], "question": "?!"},
        {"snapshot_ids": [1], "question": "a\x00b"},
        {"snapshot_ids": [1], "question": "\ud800 broken"},
        {"snapshot_ids": [1], "question": "q" * 2001},
        {"snapshot_ids": [1], "question": "q", "max_tokens": 0},
        {"snapshot_ids": [1], "question": "q", "max_tokens": 16385},
        {"snapshot_ids": [1], "question": "q", "max_tokens": "10"},
        # 這一版沒有檢索篩選：不接受（不能默默忽略）
        {"snapshot_ids": [1], "question": "q", "file_kinds": ["source"]},
        {"snapshot_ids": [1], "question": "q", "path_prefix": "src/"},
    ],
)
def test_create_request_rejects_invalid_bodies(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QaQuestionCreate.model_validate(body)
