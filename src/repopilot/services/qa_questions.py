"""問答的提問驗證、建立與查詢（第四階段 4-3a）。

`submit_question` 依序做所有檢查，**全部在建立問答與工作之前**，任何一步失敗都不會有外部呼叫、
帳本事件或資料列：快照 → 檢索索引 → 供應商 key → 模型 → 逐 repo 同意 → 粗略額度預檢。
精確的額度預留在 worker 組完上下文之後（`qa_question_runner`）。worker 開工前用
`check_prerequisites` 再驗一次（設定與同意可能在排隊期間改變）。

問答與 job 在同一交易建立（`jobs.create_job` 的 `on_created` 在 commit 前連結 `job_id`）。

.NET 對照：像 MediatR 的 command handler 先跑一串 validation behavior，最後才寫資料並發布訊息
（transactional outbox）。
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import (
    Job,
    QaCitation,
    QaContextChunk,
    QaQuestion,
    QaStatement,
    Repository,
    RepositoryQaConsent,
    Snapshot,
)
from repopilot.errors import (
    QaEgressNotConsentedError,
    QaModelNotSelectedError,
    QaProviderNotConfiguredError,
    QaQuestionActiveError,
    QaQuestionNotFoundError,
    RetrievalSnapshotNotCompletedError,
    RetrievalSnapshotNotFoundError,
    TokenBudgetExceededError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.qa import usage_ledger
from repopilot.qa.pricing import PROVIDER
from repopilot.qa.prompts import PROMPT_VERSION
from repopilot.qa.question_rules import input_tokens_upper_bound
from repopilot.qa.usage_ledger import ModelPrice, UsageLimits, token_cost_usd
from repopilot.services import jobs, retrieval_search
from repopilot.services import qa_settings as settings_service

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.qa_questions")

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class Prerequisites:
    model: str
    price: ModelPrice
    limits: UsageLimits


@dataclass(frozen=True)
class QuestionEstimate:
    """提問時的上限估計（還沒檢索）；實際預留在 worker 組完上下文後依真實片段重算。"""

    input_tokens_upper_bound: int
    max_output_tokens: int
    total_tokens_upper_bound: int
    cost_usd: Decimal


@dataclass(frozen=True)
class SubmittedQuestion:
    question: QaQuestion
    job: Job
    estimate: QuestionEstimate


def check_prerequisites(
    session: Session, settings: Settings, snapshot_ids: list[int]
) -> Prerequisites:
    """送出前的所有檢查（不含額度）；任何失敗都拋領域例外，且沒有任何外部呼叫。"""
    snapshots = {
        snapshot.id: snapshot
        for snapshot in session.scalars(select(Snapshot).where(Snapshot.id.in_(snapshot_ids)))
    }
    for snapshot_id in snapshot_ids:
        snapshot = snapshots.get(snapshot_id)
        if snapshot is None:
            raise RetrievalSnapshotNotFoundError(snapshot_id)
        if snapshot.status != "completed":
            raise RetrievalSnapshotNotCompletedError(snapshot_id)
    retrieval_search.ensure_searchable(session, settings, snapshot_ids)

    view = settings_service.get_view(session, settings)
    if not view.provider_key_configured:
        raise QaProviderNotConfiguredError(
            view.provider_key_warning or "The provider API key is not configured."
        )
    price = None if view.model is None else view.prices.get(view.model)
    if view.model is None or price is None:
        raise QaModelNotSelectedError("No model is selected; choose one in the QA settings.")

    repository_ids = {snapshot.repository_id for snapshot in snapshots.values()}
    consented = set(
        session.scalars(
            select(RepositoryQaConsent.repository_id).where(
                RepositoryQaConsent.repository_id.in_(repository_ids)
            )
        )
    )
    missing = repository_ids - consented
    if missing:
        rows = session.execute(
            select(Repository.id, Repository.name)
            .where(Repository.id.in_(missing))
            .order_by(Repository.id)
        ).all()
        raise QaEgressNotConsentedError([(row[0], row[1]) for row in rows])
    session.rollback()
    return Prerequisites(view.model, price, view.limits)


def _coarse_budget_check(
    session: Session, limits: UsageLimits, max_tokens: int, now: datetime, tz: tzinfo
) -> None:
    """粗略預檢：單次上限小於 `max_tokens`，或今日剩餘額度已經用完。只讀，不寫帳本。"""
    if limits.per_request_tokens is not None and limits.per_request_tokens < max_tokens:
        raise TokenBudgetExceededError(
            "per_request_tokens", limit=limits.per_request_tokens, requested=max_tokens
        )
    summary = usage_ledger.get_usage(session, limits, now=now, tz=tz)
    if summary.remaining_tokens is not None and summary.remaining_tokens <= 0:
        raise TokenBudgetExceededError(
            "daily_tokens",
            limit=limits.daily_tokens or 0,
            requested=max_tokens,
            used=summary.today.total_tokens,
        )
    if summary.remaining_cost_usd is not None and summary.remaining_cost_usd <= 0:
        raise TokenBudgetExceededError(
            "daily_cost_usd",
            limit=str(limits.daily_cost_usd),
            requested="0",
            used=str(summary.today.cost_usd),
        )


def submit_question(
    session: Session,
    settings: Settings,
    *,
    snapshot_ids: list[int],
    question: str,
    max_tokens: int,
    request_id: str | None = None,
    now: datetime | None = None,
    file_kinds: list[str] | None = None,
    path_prefix: str | None = None,
) -> SubmittedQuestion:
    now = now or datetime.now(UTC)
    prerequisites = check_prerequisites(session, settings, snapshot_ids)
    _coarse_budget_check(
        session, prerequisites.limits, max_tokens, now, ZoneInfo(settings.qa_usage_timezone)
    )

    input_bound = input_tokens_upper_bound(question)
    estimate = QuestionEstimate(
        input_tokens_upper_bound=input_bound,
        max_output_tokens=max_tokens,
        total_tokens_upper_bound=input_bound + max_tokens,
        cost_usd=token_cost_usd(input_bound, max_tokens, prerequisites.price),
    )

    # 構造 retrieval_filters JSON
    retrieval_filters: dict[str, object] = {}
    if file_kinds:
        retrieval_filters["file_kinds"] = file_kinds
    if path_prefix:
        retrieval_filters["path_prefix"] = path_prefix

    row = QaQuestion(
        question=question,
        snapshot_ids=snapshot_ids,
        retrieval_filters=retrieval_filters,
        provider=PROVIDER,
        model=prerequisites.model,
        prompt_version=PROMPT_VERSION,
        max_tokens=max_tokens,
        estimated_input_tokens=input_bound,
        estimated_cost_usd=estimate.cost_usd,
        request_id=request_id,
        created_at=now,
    )
    try:
        session.add(row)
        session.flush()
        question_id = row.id

        def link(_: Session, job: Job) -> None:
            row.job_id = job.id

        created = jobs.create_job(
            session,
            kind="qa_question",
            dedupe_key=f"qa_question:{question_id}",
            settings=settings,
            params={"question_id": question_id},
            request_id=request_id,
            on_created=link,
        )
    except BaseException:
        session.rollback()
        raise
    logger.info(
        "qa_question_submitted",
        extra={
            "question_id": question_id,
            "job_id": created.job.id,
            "snapshot_count": len(snapshot_ids),
        },
    )
    return SubmittedQuestion(row, created.job, estimate)


# --- 查詢 ---


@dataclass(frozen=True)
class StatementDetail:
    statement: QaStatement
    citations: list[QaCitation]


@dataclass(frozen=True)
class QuestionDetail:
    question: QaQuestion
    chunks: list[QaContextChunk]
    statements: list[StatementDetail]


def get_question(session: Session, question_id: int) -> QuestionDetail:
    question = session.get(QaQuestion, question_id, populate_existing=True)
    if question is None:
        raise QaQuestionNotFoundError("Question not found.")
    chunks = list(
        session.scalars(
            select(QaContextChunk)
            .where(QaContextChunk.question_id == question_id)
            .order_by(QaContextChunk.rank)
        )
    )
    statements = list(
        session.scalars(
            select(QaStatement)
            .where(QaStatement.question_id == question_id)
            .order_by(QaStatement.position)
        )
    )
    citations: dict[int, list[QaCitation]] = {}
    for citation in session.scalars(
        select(QaCitation)
        .join(QaStatement, QaStatement.id == QaCitation.statement_id)
        .where(QaStatement.question_id == question_id)
        .order_by(QaCitation.statement_id, QaCitation.position)
    ):
        citations.setdefault(citation.statement_id, []).append(citation)
    session.rollback()
    return QuestionDetail(
        question,
        chunks,
        [StatementDetail(item, citations.get(item.id, [])) for item in statements],
    )


def list_questions(
    session: Session, *, status: str | None, limit: int, offset: int
) -> tuple[list[QaQuestion], int]:
    """新的在前（`id DESC`）；`total` 不受分頁影響。"""
    conditions: list[ColumnElement[bool]] = []
    if status is not None:
        conditions.append(QaQuestion.status == status)
    total = session.scalar(select(func.count()).select_from(QaQuestion).where(*conditions)) or 0
    items = list(
        session.scalars(
            select(QaQuestion)
            .where(*conditions)
            .order_by(QaQuestion.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    session.rollback()
    return items, total


# --- 刪除（D8：使用者可刪除單筆或全部） ---


def delete_question(session: Session, question_id: int) -> None:
    """刪除單筆（陳述、引用、片段記錄一併刪除；帳本事件是用量事實，不刪）。

    排隊或執行中拒絕（409）：worker 還會寫入它；先取消（已取消即為終態）再刪。
    """
    deleted = session.execute(
        delete(QaQuestion)
        .where(QaQuestion.id == question_id, QaQuestion.status.in_(TERMINAL_STATUSES))
        .returning(QaQuestion.id)
    ).scalar_one_or_none()
    session.commit()
    if deleted is not None:
        logger.info("qa_question_deleted", extra={"question_id": question_id})
        return
    exists = session.scalar(select(QaQuestion.id).where(QaQuestion.id == question_id))
    session.rollback()
    if exists is None:
        raise QaQuestionNotFoundError("Question not found.")
    raise QaQuestionActiveError("The question is still queued or running; try again when it ends.")


def delete_all_questions(session: Session) -> tuple[int, int]:
    """刪除所有已結束的問答；回傳 (刪除筆數, 因排隊或執行中而保留的筆數)。"""
    deleted = session.execute(
        delete(QaQuestion).where(QaQuestion.status.in_(TERMINAL_STATUSES)).returning(QaQuestion.id)
    ).all()
    session.commit()
    skipped = session.scalar(select(func.count()).select_from(QaQuestion)) or 0
    session.rollback()
    logger.info("qa_questions_deleted", extra={"deleted": len(deleted), "skipped_active": skipped})
    return len(deleted), skipped
