"""背景工作：執行一個問答（第四階段 4-3a 成功路徑；4-3b 失敗、重試與取消）。

流程：重新驗證（設定與同意可能在排隊期間改變）→ 混合檢索 → 組上下文並保存片段清單 →
**嘗試迴圈**（預留額度 → 呼叫供應商）→ 解析與引用驗證 → **同一交易**內結算帳本、保存答案與用量並
完成 job（`jobs.complete_job` 的 `before_publish`）。

額度與供應商的順序保證：
- 每次嘗試之前：先檢查取消，再向帳本預留（重試也一樣，不以重試掩蓋額度不足）；預留之前不會有
  供應商呼叫；`token_budget_exceeded` 時沒有呼叫、沒有新的帳本事件。
- 預留 id 在預留**之前**就寫進問答記錄，崩潰後也找得到。
- 每個預留恰好結束一次：供應商明確拒絕（認證、限流、請求錯誤、花費上限…，沒有處理過請求）→
  `release`；供應商可能已處理（逾時、5xx、格式錯誤、答案無法解析、非預期例外）→ 以實際用量結算，
  沒有用量就沿用預留量並標記 `usage_unknown`。失敗的嘗試用量累計在問答上。
- 只有暫時性錯誤（逾時、429、5xx／529，且不是供應商端花費上限）才重試，上限與退避見
  `qa.failures`；退避期間以小間隔檢查取消，睡眠函式可注入（測試不真的等待）。
- 取消：嘗試之間與退避期間檢查；已送出的呼叫結束後用量照實結算，之後不再呼叫。取消與完成的先後
  由 job 列鎖決定：問答與 job 要嘛一起 `succeeded`，要嘛一起 `cancelled`（用量仍結算）。
- 失敗時問答標為 `failed` 並保留已保存的片段清單與用量。

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
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderRequestTooLargeError,
    ProviderSpendLimitReachedError,
    QAProviderError,
    UnsupportedModelError,
)
from repopilot.qa.failures import (
    MAX_ATTEMPTS,
    SLEEP_SLICE_SECONDS,
    ProviderOutcomeError,
    describe_failure,
    retry_delay_seconds,
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
from repopilot.qa.usage_ledger import ModelPrice, UsageLimits, token_cost_usd
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
    ProviderSpendLimitReachedError,
    UnsupportedModelError,
)

# 退避期間的睡眠；測試以 monkeypatch 取代（不真的等待）
sleep: Callable[[float], None] = time.sleep


def build_provider(api_key: str) -> QAProvider:
    """供應商客戶端的唯一建立點；測試以假供應商取代它（不連網）。"""
    return AnthropicProvider(api_key)


@dataclass
class _State:
    started: float = field(default_factory=time.monotonic)
    price: ModelPrice = field(default_factory=lambda: ModelPrice(Decimal(0), Decimal(0)))
    # 以下是「目前這次嘗試」的預留與呼叫狀態；每次嘗試由 `begin_attempt` 重設
    reservation_id: UUID | None = None
    reserved: bool = False
    closed: bool = False
    call_started: bool = False
    response: GenerateResponse | None = None

    def begin_attempt(self, reservation_id: UUID) -> None:
        self.reservation_id = reservation_id
        self.reserved = self.closed = self.call_started = False
        self.response = None


@dataclass(frozen=True)
class _Result:
    """嘗試迴圈的成果：答案狀態，以及在 job 完成交易內保存問答的回呼。"""

    answer_status: str
    persist: Callable[[Session], None]


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
        result = _execute(
            session, job_id, token, settings, heartbeat_checkpoint, question_id, state
        )
        _publish(session, job_id, token, question_id, state, result, finished)
    except JobLeaseLostError:
        raise  # 新的擁有者負責；這裡不寫任何結果（預留由重跑時的 _handle_not_queued 收尾）
    except ImportCancelledError:
        _record_cancelled(session, question_id, state, None)
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
        if _cancel_pending(session, job_id, token):
            # 失敗的同時使用者要求取消：job 會以 cancelled 結束（`fail_job` 的規則），問答跟著一致
            _record_cancelled(session, question_id, state, exc)
            raise ImportCancelledError("The job was cancelled.") from None
        _record_failure(session, question_id, state, code, message, exc)
        raise QaQuestionFailedError(code, message) from None


def _publish(
    session: Session,
    job_id: int,
    token: UUID,
    question_id: int,
    state: _State,
    result: _Result,
    finished: dict[str, str],
) -> None:
    """保存答案、結算帳本與完成 job 在同一交易（列鎖內才檢查取消旗標）；取消勝出則一律不保存。"""
    done = jobs.complete_job(
        session,
        job_id,
        token,
        result={"question_id": question_id, "answer_status": result.answer_status},
        before_publish=result.persist,
    )
    finished["status"] = done.status
    if done.status == "cancelled":
        raise ImportCancelledError("The job was cancelled.")
    state.closed = True


def _complete_job(
    session: Session,
    job_id: int,
    token: UUID,
    question_id: int,
    answer_status: str,
    finished: dict[str, str],
) -> None:
    """job 重跑時問答已經成功：只完成 job（答案已保存、額度已結算）。"""
    done = jobs.complete_job(
        session, job_id, token, result={"question_id": question_id, "answer_status": answer_status}
    )
    finished["status"] = done.status
    if done.status == "cancelled":
        logger.info("qa_job_cancelled_after_success", extra={"question_id": question_id})
        raise ImportCancelledError("The job was cancelled.")


def _check_cancel(
    session: Session, job_id: int, token: UUID, heartbeat_checkpoint: Callable[[], None]
) -> None:
    """檢查點：租約遺失或使用者要求取消就中止。直接讀資料庫旗標，不等心跳執行緒的下一輪。"""
    heartbeat_checkpoint()
    if jobs.read_cancel_requested(session, job_id, token):
        raise ImportCancelledError("The job was cancelled.")


def _cancel_pending(session: Session, job_id: int, token: UUID) -> bool:
    try:
        session.rollback()
        return jobs.read_cancel_requested(session, job_id, token)
    except Exception:
        session.rollback()
        return False


def _sleep_unless_cancelled(
    session: Session,
    job_id: int,
    token: UUID,
    heartbeat_checkpoint: Callable[[], None],
    seconds: float,
) -> None:
    remaining = seconds
    while remaining > 1e-9:
        step = min(SLEEP_SLICE_SECONDS, remaining)
        sleep(step)
        remaining -= step
        _check_cancel(session, job_id, token, heartbeat_checkpoint)


def _execute(
    session: Session,
    job_id: int,
    token: UUID,
    settings: Settings,
    heartbeat_checkpoint: Callable[[], None],
    question_id: int,
    state: _State,
) -> _Result:
    row = _load_question(session, question_id)
    question_text, snapshot_ids = row.question, list(row.snapshot_ids)
    model, max_tokens = row.model, row.max_tokens
    # 解析檢索篩選（無篩選時 retrieval_filters 為空 dict）
    retrieval_filters = row.retrieval_filters or {}
    file_kinds = retrieval_filters.get("file_kinds")
    path_prefix = retrieval_filters.get("path_prefix")
    heartbeat_checkpoint()
    prerequisites = check_prerequisites(session, settings, snapshot_ids)
    state.price = prerequisites.price
    api_key = settings_service.decrypt_provider_key(session, settings)
    if api_key is None:
        raise QaProviderNotConfiguredError("The provider API key is not configured.")

    jobs.update_progress(session, job_id, token, phase="retrieving")
    heartbeat_checkpoint()
    retrieval = retrieve_context(
        session,
        settings,
        snapshot_ids,
        question_text,
        file_kinds=file_kinds,  # type: ignore[arg-type]
        path_prefix=path_prefix,  # type: ignore[arg-type]
    )
    context = retrieval.context
    _save_context_chunks(session, question_id, retrieval)
    session.commit()  # 同時結束檢索的讀取交易（SET LOCAL statement_timeout）

    if not context.included:
        # 沒有任何可送出的證據：不花任何額度，直接回「證據不足」
        return _Result(
            "insufficient_evidence", lambda s: _persist_without_call(s, question_id, state)
        )

    user_message = build_user_message(question_text, context)
    estimated_input = estimate_request_input_tokens(user_message)
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
    provider: QAProvider | None = None
    attempt = 0
    while True:
        _check_cancel(session, job_id, token, heartbeat_checkpoint)
        if attempt > 0:
            # 重試：設定、同意與額度上限都重新取得（退避期間可能改變）
            prerequisites = check_prerequisites(session, settings, snapshot_ids)
            state.price = prerequisites.price

        # C2：嘗試使用 provider count_tokens 獲得精確 token 數（預留前進行估計優化）
        # 但預留仍使用本機估計（可靠且不依賴 provider），以支持早期預算檢查
        final_estimated_input = estimated_input
        estimation_method = "local_estimate"
        precise_estimate = None

        try:
            if provider is None:
                provider = build_provider(api_key)
            provider_count = _try_provider_count_tokens(settings, provider, request)
            if provider_count is not None:
                precise_estimate = provider_count
                estimation_method = "provider_count"
        except Exception:
            # count_tokens 失敗：降級為本機估計×係數（用於預留）
            # provider 重置以便錯誤恢復
            provider = None
            logger.info(
                "qa_token_count_fallback",
                extra={
                    "question_id": question_id,
                    "reason": "count_tokens_error",
                },
            )

        # 預留時使用本機估計×係數（確保預算檢查不受 provider 狀態影響）
        # 如果 count_tokens 成功，之後會用更精確的值
        factor = settings.qa_token_estimate_safety_factor
        final_estimated_input = precise_estimate or int(estimated_input * factor)

        _reserve(
            session,
            settings,
            question_id,
            state,
            final_estimated_input,
            prerequisites.limits,
            estimation_method,
        )
        attempt += 1
        _count_attempt(session, question_id, attempt)

        jobs.update_progress(session, job_id, token, phase="generating")
        heartbeat_checkpoint()

        # 確認 provider 可用；若未建立或之前失敗，現在嘗試重新建立
        if provider is None:
            provider = build_provider(api_key)

        state.call_started = True
        try:
            response = provider.generate(request)
        except QAProviderError as error:
            delay = retry_delay_seconds(error, attempt)
            if delay is None or attempt >= MAX_ATTEMPTS:
                raise
            if not _close_attempt(session, question_id, state, error):
                raise
            logger.info(
                "qa_question_retry",
                extra={
                    "question_id": question_id,
                    "attempt": attempt,
                    "error_code": error.code,
                    "delay_seconds": delay,
                },
            )
            _sleep_unless_cancelled(session, job_id, token, heartbeat_checkpoint, delay)
            continue
        break
    state.response = response
    if response.outcome is not GenerationOutcome.COMPLETED:
        raise ProviderOutcomeError(response.outcome)
    verified = verify_answer(parse_answer(response.text, answer_max_chars(max_tokens)), context)
    return _Result(
        verified.status,
        lambda s: _persist_success(
            s, question_id, state, response, verified, retrieval.repository_ids
        ),
    )


def _reserve(
    session: Session,
    settings: Settings,
    question_id: int,
    state: _State,
    estimated_input: int,
    limits: UsageLimits,
    estimation_method: str = "local_estimate",
) -> None:
    row = _load_question(session, question_id)
    reservation_id = uuid4()
    row.reservation_id = reservation_id
    row.estimated_input_tokens = estimated_input
    row.estimated_cost_usd = token_cost_usd(estimated_input, row.max_tokens, state.price)
    row.estimation_method = estimation_method  # type: ignore[attr-defined]
    session.commit()
    state.begin_attempt(reservation_id)
    usage_ledger.reserve(
        session,
        estimated_input_tokens=estimated_input,
        max_tokens=row.max_tokens,
        price=state.price,
        limits=limits,
        now=_now(),
        tz=ZoneInfo(settings.qa_usage_timezone),
        reservation_id=reservation_id,
    )
    state.reserved = True


def _count_attempt(session: Session, question_id: int, attempt: int) -> None:
    _load_question(session, question_id).attempts = attempt
    session.commit()


def _close_attempt(
    session: Session, question_id: int, state: _State, error: QAProviderError
) -> bool:
    """失敗但會重試的嘗試：結束它的預留並把用量累計到問答；預留沒結束成功就回 False（不重試）。"""
    session.rollback()
    closed = _close_reservation(session, state, error)
    if not state.closed:
        return False
    row = session.get(QaQuestion, question_id, populate_existing=True)
    if row is None:
        return False
    if closed is not None:
        _apply_usage(row, closed.totals, closed.usage_unknown, error.request_id)
    session.commit()
    return True


def _try_provider_count_tokens(
    settings: Settings, provider: QAProvider, request: GenerateRequest
) -> int | None:
    """嘗試透過供應商 count_tokens API 取得精確 token 數；失敗時返回 None"""
    try:
        # 設定超時避免 worker 卡住
        return provider.count_tokens(request)
    except Exception:
        # 供應商不支援、逾時或錯誤時降級
        return None


def _load_question(session: Session, question_id: int) -> QaQuestion:
    row = session.get(QaQuestion, question_id)
    if row is None:
        raise QaQuestionNotFoundError("Question not found.")
    return row


@dataclass(frozen=True)
class Retrieval:
    context: ContextResult
    # chunk_id → repository_id、檢索排名（1 起算，相關度由高到低）
    repository_ids: dict[int, int]
    ranks: dict[int, int]


def retrieve_context(
    session: Session,
    settings: Settings,
    snapshot_ids: list[int],
    question: str,
    *,
    file_kinds: list[str] | None = None,
    path_prefix: str | None = None,
    embed: Callable[[str], list[float]] | None = None,
) -> Retrieval:
    """混合檢索 → `build_context`（問答 worker 與評估腳本共用，輸入因此完全相同）。

    `embed` 省略＝worker 路徑（隔離 runtime 的 socket）；評估腳本在主機上執行，傳入經本機 gateway
    的查詢向量函式（與 API 的搜尋相同的路徑）。
    """
    if embed is None:
        if settings.embedding_socket_path is None:
            raise EmbeddingNotConfiguredError("The isolated embedding socket is not configured.")
        client = SocketEmbeddingClient(
            socket_path=settings.embedding_socket_path,
            profile=EmbeddingProfile.from_settings(settings),
        )
        client.verify_identity()
        embed = client.embed
    items = retrieval_search.search(
        session,
        settings,
        snapshot_ids=snapshot_ids,
        query=retrieval_query(question),
        mode="hybrid",
        limit=RETRIEVAL_CANDIDATES,
        file_kinds=file_kinds,
        path_prefix=path_prefix,
        embed=embed,
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
    return Retrieval(build_context(chunks, ContextLimits()), repository_ids, ranks)


def _save_context_chunks(session: Session, question_id: int, retrieval: Retrieval) -> None:
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


def _persist_without_call(session: Session, question_id: int, state: _State) -> None:
    """沒有證據、沒有呼叫：在 job 完成交易內把問答標為成功（用量為 0）。"""
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
    """結算帳本、保存陳述與引用、更新問答；在 job 完成交易內執行（`complete_job` 負責提交）。"""
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
    session.flush()


def _apply_usage(
    row: QaQuestion, totals: usage_ledger.UsageTotals, unknown: bool, request_id: str | None
) -> None:
    """把一次嘗試的有效用量累計到問答（重試的用量也計入）。"""
    row.input_tokens = (row.input_tokens or 0) + totals.input_tokens
    row.output_tokens = (row.output_tokens or 0) + totals.output_tokens
    row.cost_usd = (row.cost_usd or Decimal(0)) + totals.cost_usd
    row.usage_unknown = bool(row.usage_unknown) or unknown
    if request_id is not None:
        row.provider_request_id = request_id[:100]


# --- 失敗與取消 ---


def _describe(exc: BaseException) -> tuple[str, str]:
    """例外 → (穩定錯誤碼, 固定的安全訊息)；不使用例外訊息中可能含內容的部分。"""
    if isinstance(exc, SoftTimeLimitExceeded):
        return "job_time_limit_exceeded", "The job reached the worker's task time limit."
    described = describe_failure(exc)
    if described is not None:
        return described
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
    """結束仍開著的預留；回傳要記在問答上的有效用量（沒有預留或已結束則為 None）。

    有回報的用量（回應或附在例外上的）照實結算；呼叫可能已被處理卻沒有用量 → 沿用預留量並標記；
    供應商明確拒絕、或根本還沒呼叫 → 全數釋放。
    """
    if state.reservation_id is None or not state.reserved or state.closed:
        return None
    now = _now()
    reported = (
        state.response.usage
        if state.response is not None
        else exc.usage
        if isinstance(exc, QAProviderError)
        else None
    )
    try:
        if reported is not None:
            closed = _Closed(
                usage_ledger.settle(
                    session, state.reservation_id, reported, price=state.price, now=now
                ),
                not reported.known,
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
        row.failure_code = code  # type: ignore[attr-defined]
        if closed is not None:
            _apply_usage(row, closed.totals, closed.usage_unknown, _request_id(state, exc))
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


def _request_id(state: _State, exc: BaseException | None) -> str | None:
    if state.response is not None:
        return state.response.request_id
    return exc.request_id if isinstance(exc, QAProviderError) else None


def _record_cancelled(
    session: Session, question_id: int, state: _State, exc: BaseException | None
) -> None:
    """取消：問答標為 `cancelled`（不設錯誤碼）；已發生的用量照實計入，未用到的預留釋放。"""
    try:
        session.rollback()
        closed = _close_reservation(session, state, exc)
        row = session.get(QaQuestion, question_id, populate_existing=True)
        if row is None:
            return
        row.status = "cancelled"
        if closed is not None:
            _apply_usage(row, closed.totals, closed.usage_unknown, _request_id(state, exc))
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
