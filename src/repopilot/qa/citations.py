"""解析模型回答並由系統自行驗證引用；不信任模型對有效性的任何自述。"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from repopilot.qa.context import ContextResult
from repopilot.qa.errors import (
    AnswerEmptyStatementsError,
    AnswerInvalidFieldError,
    AnswerNotJsonError,
    AnswerNotObjectError,
    AnswerTooLargeError,
    AnswerUnexpectedFieldsError,
)

AnswerStatus = Literal["answered", "insufficient_evidence"]
StatementKind = Literal["fact", "inference", "open_question"]

_STRICT_FROZEN = ConfigDict(extra="forbid", strict=True, frozen=True)


class ParsedCitation(BaseModel):
    model_config = _STRICT_FROZEN

    chunk: str
    start_line: int
    end_line: int


class ParsedStatement(BaseModel):
    model_config = _STRICT_FROZEN

    text: str
    kind: StatementKind
    citations: list[ParsedCitation]

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class ParsedAnswer(BaseModel):
    model_config = _STRICT_FROZEN

    status: AnswerStatus
    statements: list[ParsedStatement]


class _DuplicateKeyError(Exception):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-standard JSON constant")


def parse_answer(raw_text: str, max_chars: int) -> ParsedAnswer:
    """嚴格解析；任何偏離契約都拋出 `AnswerParseError` 的子類（訊息不含模型輸出）。

    - 不做 markdown 圍欄剝除、不修補、不忽略多餘欄位或重複的鍵。
    - 欄位用 strict 模式：布林、浮點數、字串數字都不能當整數。
    - 行號是否合理（0、負數、超出片段）留給 `verify_answer`。
    """
    if len(raw_text) > max_chars:
        raise AnswerTooLargeError("answer exceeds the maximum allowed size")
    try:
        data = json.loads(
            raw_text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except _DuplicateKeyError:
        raise AnswerUnexpectedFieldsError("answer JSON has duplicate keys") from None
    except (ValueError, RecursionError):
        raise AnswerNotJsonError("answer is not valid JSON") from None
    if not isinstance(data, dict):
        raise AnswerNotObjectError("answer JSON is not an object")
    try:
        answer = ParsedAnswer.model_validate(data)
    except ValidationError as exc:
        if any(error["type"] == "extra_forbidden" for error in exc.errors()):
            raise AnswerUnexpectedFieldsError("answer JSON has unexpected fields") from None
        raise AnswerInvalidFieldError("answer JSON has missing or invalid fields") from None
    if answer.status == "answered" and not answer.statements:
        raise AnswerEmptyStatementsError("status is answered but there are no statements")
    return answer


class InvalidReason(StrEnum):
    UNKNOWN_CHUNK_CODE = "unknown_chunk_code"
    LINE_OUT_OF_RANGE = "line_out_of_range"
    CITATION_RANGE_INVERTED = "citation_range_inverted"
    FACT_WITHOUT_CITATION = "fact_without_citation"
    INFERENCE_WITHOUT_CITATIONS = "inference_without_citations"
    STATEMENT_WITH_INSUFFICIENT_EVIDENCE = "statement_with_insufficient_evidence"


@dataclass(frozen=True)
class Rejection:
    reason: InvalidReason
    # 引用層級的原因帶引用在該陳述中的 0 起算位置；陳述層級的原因為 None
    citation_index: int | None = None


@dataclass(frozen=True)
class ResolvedCitation:
    """座標一律取自送出的片段，不是模型的說法。"""

    chunk_code: str
    chunk_id: int
    repo: str
    snapshot_id: int
    commit_sha: str
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class StatementVerdict:
    index: int
    text: str
    kind: StatementKind
    status: Literal["valid", "invalid"]
    # 只有 valid 才有；invalid 一律為空
    citations: tuple[ResolvedCitation, ...]
    # 模型原本宣稱的引用，供追蹤記錄使用，不可當成已驗證資料
    claimed_citations: tuple[ParsedCitation, ...]
    reasons: tuple[Rejection, ...]


@dataclass(frozen=True)
class VerifiedAnswer:
    status: AnswerStatus
    verdicts: tuple[StatementVerdict, ...]

    @property
    def statements(self) -> tuple[StatementVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == "valid")

    @property
    def rejected(self) -> tuple[StatementVerdict, ...]:
        return tuple(v for v in self.verdicts if v.status == "invalid")


def verify_answer(parsed: ParsedAnswer, context: ContextResult) -> VerifiedAnswer:
    """逐一驗證陳述。

    - 引用的代號必須在 `context.included`，行號必須落在該片段送出的範圍內（含頭尾）；
      不修正、不夾限。
    - 任何一個引用無效，整個陳述就無效（保守）；`reasons` 列出是哪幾個引用。
    - `fact` 與 `inference` 至少要有一個引用；`open_question` 可無引用，但有的引用仍須有效。
    - `status=insufficient_evidence` 時不接受任何陳述：全部標為無效並保留，不進已驗證答案。
    """
    verdicts = tuple(
        _verify_statement(index, statement, parsed.status, context)
        for index, statement in enumerate(parsed.statements)
    )
    return VerifiedAnswer(status=parsed.status, verdicts=verdicts)


def _verify_statement(
    index: int, statement: ParsedStatement, status: AnswerStatus, context: ContextResult
) -> StatementVerdict:
    claimed = tuple(statement.citations)
    reasons: list[Rejection] = []
    resolved: list[ResolvedCitation] = []

    if status == "insufficient_evidence":
        reasons.append(Rejection(InvalidReason.STATEMENT_WITH_INSUFFICIENT_EVIDENCE))
    else:
        if not claimed:
            if statement.kind == "fact":
                reasons.append(Rejection(InvalidReason.FACT_WITHOUT_CITATION))
            elif statement.kind == "inference":
                reasons.append(Rejection(InvalidReason.INFERENCE_WITHOUT_CITATIONS))
        for position, citation in enumerate(claimed):
            outcome = _resolve_citation(citation, context)
            if isinstance(outcome, ResolvedCitation):
                resolved.append(outcome)
            else:
                reasons.extend(Rejection(reason, position) for reason in outcome)

    valid = not reasons
    return StatementVerdict(
        index=index,
        text=statement.text,
        kind=statement.kind,
        status="valid" if valid else "invalid",
        citations=tuple(resolved) if valid else (),
        claimed_citations=claimed,
        reasons=tuple(reasons),
    )


def _resolve_citation(
    citation: ParsedCitation, context: ContextResult
) -> ResolvedCitation | list[InvalidReason]:
    chunk = context.get(citation.chunk)
    if chunk is None:
        return [InvalidReason.UNKNOWN_CHUNK_CODE]
    problems: list[InvalidReason] = []
    if citation.start_line > citation.end_line:
        problems.append(InvalidReason.CITATION_RANGE_INVERTED)
    inside = (
        chunk.start_line <= citation.start_line <= chunk.end_line
        and chunk.start_line <= citation.end_line <= chunk.end_line
    )
    if not inside:
        problems.append(InvalidReason.LINE_OUT_OF_RANGE)
    if problems:
        return problems
    return ResolvedCitation(
        chunk_code=chunk.code,
        chunk_id=chunk.chunk_id,
        repo=chunk.repo,
        snapshot_id=chunk.snapshot_id,
        commit_sha=chunk.commit_sha,
        path=chunk.path,
        start_line=citation.start_line,
        end_line=citation.end_line,
    )
