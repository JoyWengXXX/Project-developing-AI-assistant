"""背景工作：執行一個問答（第四階段 4-3a 成功路徑，加上最低限度的安全處理）。

流程：重新驗證（設定與同意可能在排隊期間改變）→ 混合檢索 → 組上下文並保存片段清單 →
預留額度 → 呼叫供應商 → 解析與引用驗證 → **同一交易**內結算帳本並保存答案與用量 → 完成 job。

額度與供應商的順序保證：
- 預留之前不會有供應商呼叫；`token_budget_exceeded` 時沒有呼叫、沒有帳本事件。
- 預留 id 在預留**之前**就寫進問答記錄，崩潰後也找得到。
- 任何例外都不會讓預留懸空：呼叫之前失敗 → `release`（全數釋放）；供應商明確拒絕（認證、限流、
  請求錯誤…，沒有處理過請求）→ `release`；供應商可能已處理（逾時、5xx、格式錯誤、答案無法解析、
  非預期例外）→ 以實際用量結算，沒有用量就沿用預留量並標記 `usage_unknown`。
- 失敗時問答標為 `failed` 並保留已保存的片段清單與用量；完整的失敗與取消矩陣（重試、取消進行中的
  呼叫）是 #6 的範圍。

日誌與錯誤訊息只含固定文字、id 與錯誤碼；不記錄問題、片段、回答，**也不記錄例外訊息或堆疊**
（SQL 例外可能夾帶參數）。

.NET 對照：像 saga 的單一處理器：每個外部副作用（預留額度、呼叫 API）都有對應的補償步驟，
補償本身不能失敗成「沒有紀錄」。
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import (
    QaCitation,
    QaContextChunk,
    QaQuestion,
    QaStatement,
    Repository,
)
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_client import SocketEmbeddingClient
from repopilot.errors import (
    EmbeddingNotConfiguredError,
    ImportCancelledError,
    JobLeaseLostError,
    QaProviderNotConfiguredError,
    QaQuestionFailedError,
    QaQuestionNotFoundError,
    QaReservationFinalizedError,
    QaReservationNotFoundError,
    RepoPilotError,
    TokenBudgetExceededError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.qa import usage_ledger
from repopilot.qa.anthropic_provider import AnthropicProvider
from repopilot.qa.citations import VerifiedAnswer, parse_answer, verify_answer
from repopilot.qa.context import (
    ContextLimits,
    ContextResult,
    IncludedChunk,
    RetrievedChunk,
    build_context,
)
from repopilot.qa.errors import (
    AnswerParseError,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderRequestTooLargeError,
    QAProviderError,
    UnsupportedModelError,
)
from repopilot.qa.prompts import SYSTEM_PROMPT, answer_json_schema, build_user_message
from repopilot.qa.provider import (
    ChatMessage,
    GenerateRequest,
    GenerateResponse,
    GenerationOutcome,
    QAProvider,
)
from repopilot.qa.question_rules import (
    GENERATE_EFFORT,
    GENERATE_TIMEOUT_SECONDS,
    RETRIEVAL_CANDIDATES,
    answer_max_chars,
    estimate_request_input_tokens,
    retrieval_query,
)
from repopilot.qa.usage_ledger import ModelPrice, token_cost_usd
from repopilot.services import jobs, retrieval_search
from repopilot.services import qa_settings as settings_service
from repopilot.services.qa_questions import check_prerequisites

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.qa_question_runner")

_INT64_MAX = 2**63 - 1

# 供應商明確拒絕、沒有處理請求者：預留全數釋放（其餘一律保守結算）
_NOT_CHARGED = (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderRateLimitedError,
    ProviderPaymentRequiredError,
    ProviderRequestTooLargeError,
    UnsupportedModelError,
)

_OUTCOME_CODES = {
    GenerationOutcome.MAX_TOKENS: "provider_max_tokens",
    GenerationOutcome.REFUSAL: "provider_refusal",
    GenerationOutcome.CONTEXT_WINDOW_EXCEEDED: "provider_context_window_exceeded",
}
_OUTCOME_MESSAGES = {
    GenerationOutcome.MAX_TOKENS: "The provider stopped at max_tokens; the answer is incomplete.",
    GenerationOutcome.REFUSAL: "The provider refused to answer.",
    GenerationOutcome.CONTEXT_WINDOW_EXCEEDED: "The request exceeded the model's context window.",
}


def build_provider(api_key: str) -> QAProvider:
    """供應商客戶端的唯一建立點；測試以假供應商取代它（不連網）。"""
    return AnthropicProvider(api_key)


@dataclass
class _State:
    started: float = field(default_factory=time.monotonic)
    price: ModelPrice = field(default_factory=lambda: ModelPrice(Decimal(0), Decimal(0)))
    reservation_id: UUID | None = None
    reserved: bool = False
    closed: bool = False
    call_started: bool = False
    response: GenerateResponse | None = None


class _ProviderOutcomeError(Exception):
    """供應商回了不是 COMPLETED 的結果（max_tokens、拒答、超出上下文）。"""

    def __init__(self, outcome: GenerationOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


def _now() -> datetime:
    return datetime.now(UTC)


def run_question(
    session: Session,
    job_id: int,
    token: UUID,
    job_params: dict[str, Any],
    settings: Settings,
    heartbeat_checkpoint: Callable[[], None],
    finished: dict[str, str],
) -> None:
    question_id = job_params.get("question_id")
    if type(question_id) is not int:
        raise QaQuestionNotFoundError("The job has no question.")
    row = session.execute(
        select(QaQuestion)
        .where(QaQuestion.id == question_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        session.rollback()
        raise QaQuestionNotFoundError("Question not found.")
    if row.status != "queued":
        _handle_not_queued(session, job_id, token, row, finished)
        return
    row.status = "running"
    row.started_at = _now()
    session.commit()

    state = _State()
    try:
        answer_status = _execute(
            session, job_id, token, settings, heartbeat_checkpoint, question_id, state
        )
    except JobLeaseLostError:
        raise  # 新的擁有者負責；這裡不寫任何結果（預留由重跑時的 _handle_not_queued 收尾）
    except ImportCancelledError:
        _record_cancelled(session, question_id, state)
        raise
    except Exception as exc:
        code, message = _describe(exc)
        logger.warning(
            "qa_question_failed",
            extra={
                "question_id": question_id,
                "error_code": code,
                "error_type": type(exc).__name__,
            },
        )
        _record_failure(session, question_id, state, code, message, exc)
        raise QaQuestionFailedError(code, message) from None
    _complete_job(session, job_id, token, question_id, answer_status, finished)


def _complete_job(
    session: Session,
    job_id: int,
    token: UUID,
    question_id: int,
    answer_status: str,
    finished: dict[str, str],
) -> None:
    done = jobs.complete_job(
        session, job_id, token, result={"question_id": question_id, "answer_status": answer_status}
    )
    finished["status"] = done.status
    if done.status == "cancelled":
        # 答案已經保存、額度已結算：問答保持 succeeded（結果已經產生也已計費）
        logger.info("qa_job_cancelled_after_success", extra={"question_id": question_id})
        raise ImportCancelledError("The job was cancelled.")


def _execute(
    session: Session,
    job_id: int,
    token: UUID,
    settings: Settings,
    heartbeat_checkpoint: Callable[[], None],
    question_id: int,
    state: _State,
) -> str:
    row = _load_question(session, question_id)
    question_text, snapshot_ids = row.question, list(row.snapshot_ids)
    model, max_tokens = row.model, row.max_tokens
    heartbeat_checkpoint()
    prerequisites = check_prerequisites(session, settings, snapshot_ids)
    state.price = prerequisites.price
    api_key = settings_service.decrypt_provider_key(session, settings)
    if api_key is None:
        raise QaProviderNotConfiguredError("The provider API key is not configured.")

    jobs.update_progress(session, job_id, token, phase="retrieving")
    heartbeat_checkpoint()
    retrieval = _retrieve_context(session, settings, snapshot_ids, question_text)
    context = retrieval.context
    _save_context_chunks(session, question_id, retrieval)
    session.commit()  # 同時結束檢索的讀取交易（SET LOCAL statement_timeout）

    if not context.included:
        # 沒有任何可送出的證據：不花任何額度，直接回「證據不足」
        _finish_without_call(session, question_id, state)
        return "insufficient_evidence"

    user_message = build_user_message(question_text, context)
    estimated_input = estimate_request_input_tokens(user_message)
    reservation_id = uuid4()
    row = _load_question(session, question_id)
    row.reservation_id = reservation_id
    row.estimated_input_tokens = estimated_input
    row.estimated_cost_usd = token_cost_usd(estimated_input, max_tokens, state.price)
    session.commit()
    state.reservation_id = reservation_id
    usage_ledger.reserve(
        session,
        estimated_input_tokens=estimated_input,
        max_tokens=max_tokens,
        price=state.price,
        limits=prerequisites.limits,
        now=_now(),
        tz=ZoneInfo(settings.qa_usage_timezone),
        reservation_id=reservation_id,
    )
    state.reserved = True

    jobs.update_progress(session, job_id, token, phase="generating")
    heartbeat_checkpoint()
    request = GenerateRequest(
        model=model,
        system=SYSTEM_PROMPT,
        messages=(ChatMessage("user", user_message),),
        max_tokens=max_tokens,
        effort=GENERATE_EFFORT,
        timeout_seconds=GENERATE_TIMEOUT_SECONDS,
        json_schema=answer_json_schema(),
        disable_thinking=True,
    )
    provider = build_provider(api_key)
    state.call_started = True
    response = provider.generate(request)
    state.response = response
    if response.outcome is not GenerationOutcome.COMPLETED:
        raise _ProviderOutcomeError(response.outcome)
    verified = verify_answer(parse_answer(response.text, answer_max_chars(max_tokens)), context)
    _persist_success(session, question_id, state, response, verified, retrieval.repository_ids)
    return verified.status


def _load_question(session: Session, question_id: int) -> QaQuestion:
    row = session.get(QaQuestion, question_id)
    if row is None:
        raise QaQuestionNotFoundError("Question not found.")
    return row


@dataclass(frozen=True)
class _Retrieval:
    context: ContextResult
    # chunk_id → repository_id、檢索排名（1 起算，相關度由高到低）
    repository_ids: dict[int, int]
    ranks: dict[int, int]


def _retrieve_context(
    session: Session, settings: Settings, snapshot_ids: list[int], question: str
) -> _Retrieval:
    """混合檢索 → `build_context`。"""
    if settings.embedding_socket_path is None:
        raise EmbeddingNotConfiguredError("The isolated embedding socket is not configured.")
    client = SocketEmbeddingClient(
        socket_path=settings.embedding_socket_path,
        profile=EmbeddingProfile.from_settings(settings),
    )
    client.verify_identity()
    items = retrieval_search.search(
        session,
        settings,
        snapshot_ids=snapshot_ids,
        query=retrieval_query(question),
        mode="hybrid",
        limit=RETRIEVAL_CANDIDATES,
        embed=client.embed,
    )
    names = dict(
        session.execute(
            select(Repository.id, Repository.name).where(
                Repository.id.in_({item.repository_id for item in items})
            )
        ).all()
    )
    chunks = [
        RetrievedChunk(
            chunk_id=item.chunk_id,
            repo=names[item.repository_id],
            snapshot_id=item.snapshot_id,
            commit_sha=item.commit_sha,
            path=item.path,
            start_line=item.start_line,
            end_line=item.end_line,
            text=item.content,
        )
        for item in items
    ]
    repository_ids = {item.chunk_id: item.repository_id for item in items}
    ranks = {item.chunk_id: rank for rank, item in enumerate(items, 1)}
    return _Retrieval(build_context(chunks, ContextLimits()), repository_ids, ranks)


def _save_context_chunks(session: Session, question_id: int, retrieval: _Retrieval) -> None:
    """保存送出（與未送出）的片段與當下的來源位置；之後快照被移除也能解釋引用。"""
    context = retrieval.context

    def add(
        source: IncludedChunk | RetrievedChunk,
        code: str | None,
        reason: str | None,
        truncated: bool,
    ) -> None:
        session.add(
            QaContextChunk(
                question_id=question_id,
                rank=retrieval.ranks[source.chunk_id],
                code=code,
                skip_reason=reason,
                repository_id=retrieval.repository_ids[source.chunk_id],
                repo_name=source.repo,
                snapshot_id=source.snapshot_id,
                commit_sha=source.commit_sha,
                path=source.path,
                start_line=source.start_line,
                end_line=source.end_line,
                chunk_id=source.chunk_id,
                truncated=truncated,
            )
        )

    for included in context.included:
        add(included, included.code, None, included.truncated)
    for skipped in context.skipped:
        add(skipped.chunk, None, skipped.reason, False)
    session.flush()


def _finish_without_call(session: Session, question_id: int, state: _State) -> None:
    row = session.get(QaQuestion, question_id)
    if row is None:
        raise QaQuestionNotFoundError("Question not found.")
    row.status = "succeeded"
    row.answer_status = "insufficient_evidence"
    row.input_tokens = row.output_tokens = 0
    row.cost_usd = Decimal(0)
    row.estimated_input_tokens = 0
    row.estimated_cost_usd = Decimal(0)
    _stamp_finished(row, state, _now())
    session.commit()


def _stamp_finished(row: QaQuestion, state: _State, now: datetime) -> None:
    row.finished_at = now
    row.duration_ms = int((time.monotonic() - state.started) * 1000)


def _clamp(value: int) -> int:
    return max(-_INT64_MAX, min(_INT64_MAX, value))


def _persist_success(
    session: Session,
    question_id: int,
    state: _State,
    response: GenerateResponse,
    verified: VerifiedAnswer,
    repository_ids: dict[int, int],
) -> None:
    """結算帳本、保存陳述與引用、更新問答：同一交易，要嘛全部成功、要嘛都不留。"""
    now = _now()
    if state.reservation_id is None:
        raise RepoPilotError("The question has no reservation.")
    totals = usage_ledger.settle(
        session, state.reservation_id, response.usage, price=state.price, now=now, commit=False
    )
    row = _load_question(session, question_id)
    row.status = "succeeded"
    row.answer_status = verified.status
    _apply_usage(row, totals, not response.usage.known, response.request_id)
    _stamp_finished(row, state, now)

    statements = [
        QaStatement(
            question_id=question_id,
            position=verdict.index,
            kind=verdict.kind,
            body=verdict.text,
            status=verdict.status,
            rejection_reasons=[
                {"reason": rejection.reason.value, "citation_index": rejection.citation_index}
                for rejection in verdict.reasons
            ],
        )
        for verdict in verified.verdicts
    ]
    session.add_all(statements)
    session.flush()
    for statement, verdict in zip(statements, verified.verdicts, strict=True):
        for position, claimed in enumerate(verdict.claimed_citations):
            reasons = [
                rejection.reason.value
                for rejection in verdict.reasons
                if rejection.citation_index == position
            ]
            resolved = verdict.citations[position] if verdict.status == "valid" else None
            session.add(
                QaCitation(
                    statement_id=statement.id,
                    position=position,
                    claimed_chunk=claimed.chunk[:50],
                    claimed_start_line=_clamp(claimed.start_line),
                    claimed_end_line=_clamp(claimed.end_line),
                    valid=not reasons,
                    reasons=reasons,
                    resolved_chunk_id=resolved.chunk_id if resolved else None,
                    resolved_repository_id=(
                        repository_ids[resolved.chunk_id] if resolved else None
                    ),
                    resolved_repo_name=resolved.repo if resolved else None,
                    resolved_snapshot_id=resolved.snapshot_id if resolved else None,
                    resolved_commit_sha=resolved.commit_sha if resolved else None,
                    resolved_path=resolved.path if resolved else None,
                    resolved_start_line=resolved.start_line if resolved else None,
                    resolved_end_line=resolved.end_line if resolved else None,
                )
            )
    session.commit()
    state.closed = True


def _apply_usage(
    row: QaQuestion, totals: usage_ledger.UsageTotals, unknown: bool, request_id: str | None
) -> None:
    row.input_tokens = totals.input_tokens
    row.output_tokens = totals.output_tokens
    row.cost_usd = totals.cost_usd
    row.usage_unknown = unknown
    row.provider_request_id = None if request_id is None else request_id[:100]


# --- 失敗與取消（最低限度的安全處理；完整矩陣見 #6） ---


def _describe(exc: BaseException) -> tuple[str, str]:
    """例外 → (穩定錯誤碼, 固定的安全訊息)；不使用例外訊息中可能含內容的部分。"""
    if isinstance(exc, SoftTimeLimitExceeded):
        return "job_time_limit_exceeded", "The job reached the worker's task time limit."
    if isinstance(exc, _ProviderOutcomeError):
        return _OUTCOME_CODES[exc.outcome], _OUTCOME_MESSAGES[exc.outcome]
    if isinstance(exc, TokenBudgetExceededError):
        return exc.code, f"The token budget does not allow this request ({exc.reason})."
    if isinstance(exc, QAProviderError | AnswerParseError | RepoPilotError):
        return exc.code, str(exc)
    if isinstance(exc, OperationalError | InterfaceError):
        return "database_unavailable", "The database was unavailable."
    return "internal_error", f"Unexpected {type(exc).__name__}."


@dataclass(frozen=True)
class _Closed:
    totals: usage_ledger.UsageTotals
    usage_unknown: bool


def _close_reservation(
    session: Session, state: _State, exc: BaseException | None
) -> _Closed | None:
    """結束仍開著的預留；回傳要記在問答上的有效用量（沒有預留或已結束則為 None）。"""
    if state.reservation_id is None or not state.reserved or state.closed:
        return None
    now = _now()
    try:
        if state.response is not None:
            usage = state.response.usage
            closed = _Closed(
                usage_ledger.settle(
                    session, state.reservation_id, usage, price=state.price, now=now
                ),
                not usage.known,
            )
        elif state.call_started and not isinstance(exc, _NOT_CHARGED):
            closed = _Closed(
                usage_ledger.settle(
                    session, state.reservation_id, None, price=state.price, now=now
                ),
                True,
            )
        else:
            closed = _Closed(
                usage_ledger.release(
                    session, state.reservation_id, None, price=state.price, now=now
                ),
                False,
            )
    except (QaReservationNotFoundError, QaReservationFinalizedError):
        state.closed = True
        return None
    except Exception as close_error:
        logger.error(
            "qa_reservation_close_failed",
            extra={
                "reservation_id": str(state.reservation_id),
                "error_type": type(close_error).__name__,
            },
        )
        return None
    state.closed = True
    return closed


def _record_failure(
    session: Session,
    question_id: int,
    state: _State,
    code: str,
    message: str,
    exc: BaseException,
) -> None:
    try:
        session.rollback()
        closed = _close_reservation(session, state, exc)
        row = session.get(QaQuestion, question_id, populate_existing=True)
        if row is None:  # 執行期間被刪除（不應發生：進行中不能刪）
            return
        row.status = "failed"
        row.error_code = code
        row.error_summary = message[:500]
        if closed is not None:
            request_id = None if state.response is None else state.response.request_id
            _apply_usage(row, closed.totals, closed.usage_unknown, request_id)
        if not state.reserved:
            row.reservation_id = None
        _stamp_finished(row, state, _now())
        session.commit()
    except Exception as record_error:
        session.rollback()
        logger.error(
            "qa_question_record_failed",
            extra={"question_id": question_id, "error_type": type(record_error).__name__},
        )


def _record_cancelled(session: Session, question_id: int, state: _State) -> None:
    try:
        session.rollback()
        closed = _close_reservation(session, state, None)
        row = session.get(QaQuestion, question_id, populate_existing=True)
        if row is None:
            return
        row.status = "cancelled"
        if closed is not None:
            _apply_usage(row, closed.totals, closed.usage_unknown, None)
        _stamp_finished(row, state, _now())
        session.commit()
    except Exception as record_error:
        session.rollback()
        logger.error(
            "qa_question_record_failed",
            extra={"question_id": question_id, "error_type": type(record_error).__name__},
        )


def _handle_not_queued(
    session: Session, job_id: int, token: UUID, row: QaQuestion, finished: dict[str, str]
) -> None:
    """job 重跑時問答已不是 queued：上一次執行中斷（running）或問答已有結果。"""
    question_id, status = row.id, row.status
    reservation_id, answer_status = row.reservation_id, row.answer_status or ""
    session.rollback()
    if status == "succeeded":
        _complete_job(session, job_id, token, question_id, answer_status, finished)
        return
    if status == "running":
        state = _State(reservation_id=reservation_id, reserved=reservation_id is not None)
        state.call_started = True  # 不知道呼叫有沒有發生：保守結算（沿用預留量並標記）
        message = "The previous run was interrupted."
        _record_failure(session, question_id, state, "qa_interrupted", message, RuntimeError())
        raise QaQuestionFailedError("qa_interrupted", message)
    raise QaQuestionFailedError(
        "qa_question_not_runnable", "The question already ended and cannot run again."
    )
