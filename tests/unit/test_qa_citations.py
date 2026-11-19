"""回答解析與引用驗證（純函式）：嚴格解析、代號與行號檢查、不信任模型自述。"""

import json
from typing import Any

import pytest

from repopilot.qa.citations import (
    InvalidReason,
    ParsedAnswer,
    ResolvedCitation,
    parse_answer,
    verify_answer,
)
from repopilot.qa.context import ContextLimits, ContextResult, RetrievedChunk, build_context
from repopilot.qa.errors import (
    AnswerEmptyStatementsError,
    AnswerInvalidFieldError,
    AnswerNotJsonError,
    AnswerNotObjectError,
    AnswerParseError,
    AnswerTooLargeError,
    AnswerUnexpectedFieldsError,
)
from repopilot.qa.fake_provider import FakeProvider
from repopilot.qa.prompts import answer_json_schema
from repopilot.qa.provider import ChatMessage, GenerateRequest

SHA_A = "a" * 40
SHA_B = "b" * 40
MAX_CHARS = 100_000


def make_context() -> ContextResult:
    chunks = [
        RetrievedChunk(
            1, "demo", 1, SHA_A, "src/a.py", 10, 19, "\n".join(f"a{i}" for i in range(10))
        ),
        RetrievedChunk(
            2, "demo", 2, SHA_B, "src/a.py", 10, 19, "\n".join(f"b{i}" for i in range(10))
        ),
        RetrievedChunk(
            3, "other", 3, SHA_B, "lib/c.py", 1, 5, "\n".join(f"c{i}" for i in range(5))
        ),
    ]
    return build_context(chunks, ContextLimits())


CONTEXT = make_context()


def cite(chunk: Any, start: Any, end: Any) -> dict[str, Any]:
    return {"chunk": chunk, "start_line": start, "end_line": end}


def statement(
    kind: str = "fact", citations: list[Any] | None = None, text: str = "a claim"
) -> dict[str, Any]:
    return {"text": text, "kind": kind, "citations": [] if citations is None else citations}


def answer(*statements: dict[str, Any], status: str = "answered") -> str:
    return json.dumps({"status": status, "statements": list(statements)})


def verify(*statements: dict[str, Any], status: str = "answered"):  # type: ignore[no-untyped-def]
    return verify_answer(parse_answer(answer(*statements, status=status), MAX_CHARS), CONTEXT)


def reason_codes(verdict: Any) -> list[str]:
    return [r.reason.value for r in verdict.reasons]


def test_qa_citations_valid_fact_carries_full_coordinates_from_the_chunk() -> None:
    result = verify(statement("fact", [cite("C1", 12, 14)]))

    (verdict,) = result.statements
    assert verdict.status == "valid"
    assert verdict.citations == (
        ResolvedCitation(
            chunk_code="C1",
            chunk_id=1,
            repo="demo",
            snapshot_id=1,
            commit_sha=SHA_A,
            path="src/a.py",
            start_line=12,
            end_line=14,
        ),
    )
    assert result.rejected == ()


def test_qa_citations_range_may_cover_exactly_the_whole_chunk() -> None:
    result = verify(statement("fact", [cite("C1", 10, 19)]))

    assert len(result.statements) == 1


@pytest.mark.parametrize("line", [10, 19])
def test_qa_citations_single_line_at_chunk_edges_is_valid(line: int) -> None:
    assert len(verify(statement("fact", [cite("C1", line, line)])).statements) == 1


@pytest.mark.parametrize("code", ["C9", "C0", "c1", "", "C01", " C1", "C1 ", "../C1"])
def test_qa_citations_unknown_chunk_code_marks_statement_invalid(code: str) -> None:
    result = verify(statement("fact", [cite(code, 10, 11)]))

    assert result.statements == ()
    (verdict,) = result.rejected
    assert reason_codes(verdict) == ["unknown_chunk_code"]
    assert verdict.reasons[0].citation_index == 0
    assert verdict.citations == ()
    assert verdict.claimed_citations[0].chunk == code


@pytest.mark.parametrize(
    ("start", "end"), [(9, 12), (10, 20), (20, 25), (1, 5), (0, 10), (-3, 12), (0, 0), (-5, -1)]
)
def test_qa_citations_lines_outside_the_chunk_are_invalid_and_never_clamped(
    start: int, end: int
) -> None:
    result = verify(statement("fact", [cite("C1", start, end)]))

    (verdict,) = result.rejected
    assert InvalidReason.LINE_OUT_OF_RANGE.value in reason_codes(verdict)
    assert verdict.citations == ()
    assert (verdict.claimed_citations[0].start_line, verdict.claimed_citations[0].end_line) == (
        start,
        end,
    )


def test_qa_citations_inverted_range_is_invalid_even_inside_the_chunk() -> None:
    (verdict,) = verify(statement("fact", [cite("C1", 15, 12)])).rejected

    assert reason_codes(verdict) == ["citation_range_inverted"]


def test_qa_citations_inverted_range_outside_chunk_reports_both() -> None:
    (verdict,) = verify(statement("fact", [cite("C3", 9, 2)])).rejected

    assert reason_codes(verdict) == ["citation_range_inverted", "line_out_of_range"]


def test_qa_citations_fact_without_citation_is_invalid() -> None:
    result = verify(statement("fact", []))

    (verdict,) = result.rejected
    assert reason_codes(verdict) == ["fact_without_citation"]
    assert verdict.reasons[0].citation_index is None
    assert result.statements == ()


def test_qa_citations_inference_requires_citations() -> None:
    (verdict,) = verify(statement("inference", [])).rejected

    assert reason_codes(verdict) == ["inference_without_citations"]


def test_qa_citations_inference_with_valid_citation_is_valid() -> None:
    assert len(verify(statement("inference", [cite("C2", 11, 12)])).statements) == 1


def test_qa_citations_open_question_may_have_no_citations() -> None:
    (verdict,) = verify(statement("open_question", [])).statements

    assert verdict.kind == "open_question"
    assert verdict.citations == ()


def test_qa_citations_open_question_with_an_invalid_citation_is_invalid() -> None:
    (verdict,) = verify(statement("open_question", [cite("C9", 1, 1)])).rejected

    assert reason_codes(verdict) == ["unknown_chunk_code"]


def test_qa_citations_one_bad_citation_invalidates_the_statement_and_names_it() -> None:
    result = verify(statement("fact", [cite("C1", 10, 11), cite("C1", 30, 31), cite("C9", 1, 1)]))

    (verdict,) = result.rejected
    assert [(r.reason.value, r.citation_index) for r in verdict.reasons] == [
        ("line_out_of_range", 1),
        ("unknown_chunk_code", 2),
    ]
    assert verdict.citations == ()
    assert len(verdict.claimed_citations) == 3


def test_qa_citations_valid_and_invalid_statements_are_separated_in_order() -> None:
    result = verify(
        statement("fact", [cite("C1", 10, 10)], text="good one"),
        statement("fact", [cite("C1", 99, 99)], text="bad"),
        statement("open_question", [], text="good two"),
    )

    assert [v.text for v in result.statements] == ["good one", "good two"]
    assert [v.text for v in result.rejected] == ["bad"]
    assert [v.index for v in result.verdicts] == [0, 1, 2]
    assert [v.status for v in result.verdicts] == ["valid", "invalid", "valid"]


def test_qa_citations_repeated_citations_to_the_same_chunk_are_all_resolved() -> None:
    (verdict,) = verify(
        statement("fact", [cite("C1", 10, 11), cite("C1", 10, 11), cite("C1", 15, 16)])
    ).statements

    assert [(c.start_line, c.end_line) for c in verdict.citations] == [(10, 11), (10, 11), (15, 16)]


def test_qa_citations_same_path_in_two_snapshots_keeps_each_snapshot_and_sha() -> None:
    (verdict,) = verify(statement("fact", [cite("C1", 10, 11), cite("C2", 10, 11)])).statements

    first, second = verdict.citations
    assert first.path == second.path == "src/a.py"
    assert (first.snapshot_id, first.commit_sha) == (1, SHA_A)
    assert (second.snapshot_id, second.commit_sha) == (2, SHA_B)


def test_qa_citations_lines_are_checked_against_the_cited_chunk_not_another() -> None:
    (verdict,) = verify(statement("fact", [cite("C3", 10, 11)])).rejected

    assert reason_codes(verdict) == ["line_out_of_range"]


def test_qa_citations_truncated_chunk_only_accepts_lines_that_were_sent() -> None:
    text = "\n".join(f"line{i:02d}" for i in range(1, 21))
    context = build_context(
        [RetrievedChunk(1, "demo", 1, SHA_A, "a.py", 1, 20, text)],
        ContextLimits(max_chunk_chars=30),
    )
    parsed_ok = parse_answer(answer(statement("fact", [cite("C1", 1, 4)])), MAX_CHARS)
    parsed_cut = parse_answer(answer(statement("fact", [cite("C1", 1, 5)])), MAX_CHARS)

    assert len(verify_answer(parsed_ok, context).statements) == 1
    assert len(verify_answer(parsed_cut, context).rejected) == 1


def test_qa_citations_insufficient_evidence_with_no_statements_is_fine() -> None:
    result = verify(status="insufficient_evidence")

    assert result.status == "insufficient_evidence"
    assert result.verdicts == ()


def test_qa_citations_statements_under_insufficient_evidence_are_ignored_but_kept() -> None:
    result = verify(
        statement("fact", [cite("C1", 10, 11)]),
        statement("fact", []),
        status="insufficient_evidence",
    )

    assert result.statements == ()
    assert len(result.rejected) == 2
    assert all(reason_codes(v) == ["statement_with_insufficient_evidence"] for v in result.rejected)


def test_qa_citations_verification_ignores_extra_validity_claims_by_rejecting_them() -> None:
    raw = json.dumps(
        {
            "status": "answered",
            "statements": [
                {**statement("fact", [cite("C9", 1, 1)]), "valid": True, "verified": True}
            ],
        }
    )

    with pytest.raises(AnswerUnexpectedFieldsError):
        parse_answer(raw, MAX_CHARS)


def test_qa_citations_verify_against_empty_context_rejects_everything() -> None:
    empty = build_context([], ContextLimits())
    parsed = parse_answer(answer(statement("fact", [cite("C1", 1, 1)])), MAX_CHARS)

    result = verify_answer(parsed, empty)

    assert result.statements == ()
    assert reason_codes(result.rejected[0]) == ["unknown_chunk_code"]


def test_qa_citations_valid_answer_parses_into_typed_model() -> None:
    parsed = parse_answer(
        answer(statement("fact", [cite("C1", 10, 11)], text="中文 text")), MAX_CHARS
    )

    assert isinstance(parsed, ParsedAnswer)
    assert parsed.status == "answered"
    assert parsed.statements[0].citations[0].chunk == "C1"


def test_qa_citations_parse_accepts_insufficient_evidence_with_empty_statements() -> None:
    parsed = parse_answer(answer(status="insufficient_evidence"), MAX_CHARS)

    assert parsed.statements == []


def test_qa_citations_nested_json_inside_text_stays_a_plain_string() -> None:
    nested = json.dumps(
        {"status": "answered", "statements": [statement("fact", [cite("C1", 10, 10)])]}
    )
    parsed = parse_answer(answer(statement("open_question", [], text=nested)), MAX_CHARS)

    assert parsed.statements[0].text == nested
    assert len(parsed.statements) == 1


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json",
        '{"status": "answered"',
        "```json\n" + answer(statement("open_question")) + "\n```",
        "Here you go: " + answer(statement("open_question")),
        answer(statement("open_question")) + answer(statement("open_question")),
        '{"status": NaN, "statements": []}',
        "{'status': 'answered', 'statements': []}",
        "9" * 5000,
    ],
)
def test_qa_citations_non_json_is_rejected_with_its_own_code(raw: str) -> None:
    with pytest.raises(AnswerNotJsonError) as caught:
        parse_answer(raw, 1_000_000)

    assert caught.value.code == "answer_not_json"


@pytest.mark.parametrize("opener", ["[", '{"a":'])
def test_qa_citations_deeply_nested_json_is_not_json_not_a_crash(opener: str) -> None:
    with pytest.raises(AnswerNotJsonError):
        parse_answer(opener * 100_000, 10_000_000)


@pytest.mark.parametrize("raw", ["[]", "[1, 2]", '"text"', "123", "true", "null", "1.5"])
def test_qa_citations_non_object_json_is_rejected(raw: str) -> None:
    with pytest.raises(AnswerNotObjectError) as caught:
        parse_answer(raw, MAX_CHARS)

    assert caught.value.code == "answer_not_object"


@pytest.mark.parametrize(
    "raw",
    [
        '{"status": "answered", "statements": [], "confidence": 0.9}',
        '{"status": "insufficient_evidence", "statements": [], "note": "x"}',
        '{"status": "insufficient_evidence", "statements": [], "status": "answered"}',
        json.dumps(
            {
                "status": "answered",
                "statements": [{**statement("fact", [cite("C1", 1, 1)]), "x": 1}],
            }
        ),
        json.dumps(
            {
                "status": "answered",
                "statements": [
                    {
                        **statement("fact"),
                        "citations": [{**cite("C1", 1, 1), "path": "/etc/passwd"}],
                    }
                ],
            }
        ),
        '{"status": "answered", "statements": [], "": 1}',
    ],
)
def test_qa_citations_extra_fields_or_duplicate_keys_are_rejected(raw: str) -> None:
    with pytest.raises(AnswerUnexpectedFieldsError) as caught:
        parse_answer(raw, MAX_CHARS)

    assert caught.value.code == "answer_unexpected_fields"


def bad_statement(**overrides: Any) -> dict[str, Any]:
    return {**statement("fact", [cite("C1", 10, 11)]), **overrides}


def wrap(*statements: Any, status: Any = "answered") -> str:
    return json.dumps({"status": status, "statements": list(statements)})


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"status": "answered"}',
        '{"statements": []}',
        wrap(bad_statement(), status="done"),
        wrap(bad_statement(), status=None),
        wrap(bad_statement(), status=1),
        wrap(bad_statement(), status="ANSWERED"),
        '{"status": "answered", "statements": null}',
        '{"status": "answered", "statements": {}}',
        '{"status": "answered", "statements": "none"}',
        wrap("a string"),
        wrap(None),
        wrap(bad_statement(kind="guess")),
        wrap(bad_statement(kind="Fact")),
        wrap(bad_statement(kind=None)),
        wrap(bad_statement(text=None)),
        wrap(bad_statement(text=5)),
        wrap(bad_statement(text="")),
        wrap(bad_statement(text="  \n\t")),
        wrap(bad_statement(citations=None)),
        wrap(bad_statement(citations="C1")),
        wrap(bad_statement(citations=["C1"])),
        wrap(bad_statement(citations=[None])),
        wrap({"text": "t", "kind": "fact"}),
        wrap({"kind": "fact", "citations": []}),
        wrap(bad_statement(citations=[{"chunk": "C1", "start_line": 1}])),
        wrap(bad_statement(citations=[{"start_line": 1, "end_line": 1}])),
        wrap(bad_statement(citations=[cite(None, 1, 1)])),
        wrap(bad_statement(citations=[cite(1, 1, 1)])),
        wrap(bad_statement(citations=[cite("C1", None, 1)])),
        wrap(bad_statement(citations=[cite("C1", 1, None)])),
        wrap(bad_statement(citations=[cite("C1", 1.5, 2)])),
        wrap(bad_statement(citations=[cite("C1", 1.0, 2)])),
        wrap(bad_statement(citations=[cite("C1", 1, 2.0)])),
        wrap(bad_statement(citations=[cite("C1", "1", 2)])),
        wrap(bad_statement(citations=[cite("C1", True, 2)])),
        wrap(bad_statement(citations=[cite("C1", 1, False)])),
        wrap(bad_statement(citations=[cite("C1", [1], 2)])),
        '{"status": "answered", "statements": [{"text": "t", "kind": "fact", '
        '"citations": [{"chunk": "C1", "start_line": 1e2, "end_line": 1}]}]}',
    ],
)
def test_qa_citations_wrong_types_enums_or_missing_fields_are_rejected(raw: str) -> None:
    with pytest.raises(AnswerInvalidFieldError) as caught:
        parse_answer(raw, MAX_CHARS)

    assert caught.value.code == "answer_invalid_field"


def test_qa_citations_answered_without_statements_is_rejected() -> None:
    with pytest.raises(AnswerEmptyStatementsError) as caught:
        parse_answer(answer(status="answered"), MAX_CHARS)

    assert caught.value.code == "answer_empty_statements"


def test_qa_citations_output_over_the_limit_is_rejected_before_parsing() -> None:
    raw = answer(statement("open_question", [], text="x" * 5000))

    with pytest.raises(AnswerTooLargeError) as caught:
        parse_answer(raw, 1000)

    assert caught.value.code == "answer_too_large"


def test_qa_citations_output_exactly_at_the_limit_is_accepted() -> None:
    raw = answer(statement("open_question", [], text="x"))

    assert parse_answer(raw, len(raw)).status == "answered"
    with pytest.raises(AnswerTooLargeError):
        parse_answer(raw, len(raw) - 1)


def test_qa_citations_huge_output_is_rejected_without_parsing() -> None:
    with pytest.raises(AnswerTooLargeError):
        parse_answer("[" * 5_000_000, 200_000)


def test_qa_citations_zero_negative_and_huge_integer_lines_parse_but_fail_verification() -> None:
    result = verify(
        statement("fact", [cite("C1", 0, 0)]),
        statement("fact", [cite("C1", -1, 5)]),
        statement("fact", [cite("C1", 10, 10**30)]),
    )

    assert result.statements == ()
    assert len(result.rejected) == 3


@pytest.mark.parametrize(
    "error",
    [
        AnswerTooLargeError,
        AnswerNotJsonError,
        AnswerNotObjectError,
        AnswerUnexpectedFieldsError,
        AnswerInvalidFieldError,
        AnswerEmptyStatementsError,
    ],
)
def test_qa_citations_parse_errors_share_a_base_and_have_distinct_codes(
    error: type[AnswerParseError],
) -> None:
    assert issubclass(error, AnswerParseError)
    codes = {
        e.code
        for e in (
            AnswerTooLargeError,
            AnswerNotJsonError,
            AnswerNotObjectError,
            AnswerUnexpectedFieldsError,
            AnswerInvalidFieldError,
            AnswerEmptyStatementsError,
        )
    }
    assert len(codes) == 6
    assert error.code in codes


def test_qa_citations_error_messages_do_not_leak_model_output() -> None:
    secret = "SECRET-MODEL-OUTPUT"
    raws = [
        secret,
        json.dumps({"status": "answered", "statements": [], secret: 1}),
        json.dumps({"status": secret, "statements": []}),
        secret * 1000,
    ]
    for raw in raws:
        with pytest.raises(AnswerParseError) as caught:
            parse_answer(raw, 5000)
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)


def test_qa_citations_schema_matches_the_parser_contract() -> None:
    schema: Any = answer_json_schema()
    statement_schema = schema["properties"]["statements"]["items"]
    citation_schema = statement_schema["properties"]["citations"]["items"]

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(ParsedAnswer.model_fields)
    assert set(statement_schema["properties"]) == {"text", "kind", "citations"}
    assert statement_schema["required"] == ["text", "kind", "citations"]
    assert statement_schema["additionalProperties"] is False
    assert statement_schema["properties"]["kind"]["enum"] == ["fact", "inference", "open_question"]
    assert set(citation_schema["properties"]) == {"chunk", "start_line", "end_line"}
    assert citation_schema["additionalProperties"] is False
    assert schema["properties"]["status"]["enum"] == ["answered", "insufficient_evidence"]


def test_qa_citations_schema_is_a_fresh_copy_each_call() -> None:
    first = answer_json_schema()
    first["required"] = []

    assert answer_json_schema()["required"] == ["status", "statements"]


def test_qa_citations_schema_is_accepted_by_the_provider_request() -> None:
    request = GenerateRequest(
        model="claude-haiku-5-5",
        system="s",
        messages=(ChatMessage(role="user", content="u"),),
        max_tokens=100,
        effort="low",
        timeout_seconds=5,
        json_schema=answer_json_schema(),
    )
    provider = FakeProvider(text=answer(statement("open_question")))

    response = provider.generate(request)

    assert parse_answer(response.text, MAX_CHARS).statements[0].kind == "open_question"
