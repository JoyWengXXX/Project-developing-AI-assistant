"""outbox 派送與復原掃描（第二階段切片 2，D39）：dispatcher 迴圈每一輪會呼叫的函式。

本切片只有「單輪」函式與可注入的發布器介面；迴圈、程序與 Celery 發布器是切片 3 的內容。
所有函式自己控制交易，且都接受 `now` 以便確定性測試。

派送（規格 5.3）：`SELECT ... FOR UPDATE OF d SKIP LOCKED` 取待派送事件，逐筆呼叫發布器，
**發布成功後**才標記 `sent` 並提交。多個 dispatcher 同時執行時各取不同的列（SKIP LOCKED）。
「發布成功但標記前崩潰」會造成重送，這是允許的：取得租約是條件式 UPDATE，重複訊息只有一個贏家。

復原（規格 5.9）：租約過期的 running 工作、派送後遲遲沒被接收的 queued 工作、已結束工作的殘留事件。
排隊中的工作**不會**因等待太久而自動失敗。
"""

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, delete, exists, select, update
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job, JobDispatch
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import job_rules, qa_questions
from repopilot.services.jobs import db_now, finish_locked, requeue_locked, schedule_dispatch

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.job_dispatch")

DISPATCH_BATCH_SIZE = 50  # 規格 5.3
RECOVERY_BATCH_SIZE = 100


class JobPublisher(Protocol):
    """把 job id 交給 broker 的發布器。成功返回＝broker 已接受；失敗請拋出例外。

    切片 3 提供 Celery 的實作；本切片的測試使用假發布器。訊息只帶 job id（不帶路徑、token）。
    """

    def publish(self, job_id: int) -> None: ...


@dataclass(frozen=True)
class DispatchResult:
    sent: int
    failed: int


@dataclass(frozen=True)
class RecoveryResult:
    requeued: int  # 租約過期 → 重新排隊
    failed: int  # 租約過期且已達上限 → failed（worker_lost）
    cancelled: int  # 租約過期且已有取消請求 → cancelled
    resent: int  # 已派送但未被接收 → 重設為 pending
    removed_dispatches: int  # 終態工作的殘留事件
    healed: int  # queued 卻沒有事件的工作 → 補上事件
    overdue_pending: int  # pending 太久仍未被派送（只回報，不改動）


# --- 派送 ---


def dispatch_pending(
    session: Session,
    publisher: JobPublisher,
    *,
    batch_size: int = DISPATCH_BATCH_SIZE,
    now: datetime | None = None,
) -> DispatchResult:
    """派送一批待派送事件：發布成功 → `sent`（`send_count`＋1）；失敗 → 保持 `pending` 並計次。

    只派送 job 仍是 `queued` 且 `not_before` 已到的事件。整批在一個交易內，列鎖持有到提交，
    所以其他 dispatcher 的 SKIP LOCKED 不會取到同一批。發布器失敗只記錄例外**型別名稱**
    （`last_error`），不記錄訊息；其他事件照常處理。
    """
    now_expr = db_now(now)
    sent = failed = 0
    try:
        dispatches = (
            session.execute(
                select(JobDispatch)
                .join(Job, Job.id == JobDispatch.job_id)
                .where(
                    JobDispatch.status == "pending",
                    JobDispatch.not_before <= now_expr,
                    Job.status == "queued",
                )
                .order_by(JobDispatch.id)
                .limit(batch_size)
                .with_for_update(of=JobDispatch, skip_locked=True)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        for dispatch in dispatches:
            dispatch_id, job_id = dispatch.id, dispatch.job_id
            try:
                publisher.publish(job_id)
            except Exception as exc:
                failed += 1
                session.execute(
                    update(JobDispatch)
                    .where(JobDispatch.id == dispatch_id)
                    .values(
                        failure_count=JobDispatch.failure_count + 1,
                        last_error=type(exc).__name__[:100],
                    )
                )
                logger.warning(
                    "dispatch_publish_failed",
                    extra={"job_id": job_id, "error_type": type(exc).__name__},
                )
            else:
                sent += 1
                session.execute(
                    update(JobDispatch)
                    .where(JobDispatch.id == dispatch_id)
                    .values(
                        status="sent",
                        send_count=JobDispatch.send_count + 1,
                        last_sent_at=now_expr,
                        last_error=None,
                    )
                )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return DispatchResult(sent=sent, failed=failed)


# --- 復原掃描 ---


def recover_expired_leases(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    rand: Callable[[], float] = random.random,
    batch_size: int = RECOVERY_BATCH_SIZE,
) -> tuple[int, int, int]:
    """租約過期的 running 工作：回傳 (重新排隊, failed, cancelled) 的數量。

    - 已有取消請求 → `cancelled`（不重試一個使用者已經要取消的工作）。
    - `attempt < max_attempts` → `queued`（`worker_lost`、退避、新的待派送事件）。
    - 否則 → `failed`（`worker_lost`）。
    - `qa_question` 且問答已在執行中 → 直接 `failed`（重跑只會得到 `qa_interrupted`）；問答跟著
      結束（`qa_worker_lost` 或 `cancelled`），開著的預留在同一交易內結算（C3）。
    `FOR UPDATE SKIP LOCKED`：正在被別的交易處理（例如擁有者正在發布結果）的列這一輪跳過，
    不與正常完成競爭；列鎖加上「租約過期」的 WHERE 條件，也保證擁有者若已延長租約就不會被接手。
    """
    now_expr = db_now(now)
    requeued = failed = cancelled = 0
    try:
        jobs = (
            session.execute(
                select(Job)
                .where(Job.status == "running", Job.lease_expires_at < now_expr)
                .order_by(Job.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        for job in jobs:
            # 執行中的問答被中斷後，重跑也只會得到 qa_interrupted：不重新排隊，直接結束並結算預留
            question = (
                qa_questions.lock_active_question(session, job.id)
                if job.kind == "qa_question"
                else None
            )
            lost_running_question = question is not None and question.status == "running"
            if job.cancel_requested_at is not None:
                finish_locked(session, job, "cancelled", now_expr)
                if question is not None:
                    qa_questions.close_lost_question(
                        session, question, status="cancelled", now=now or datetime.now(UTC)
                    )
                cancelled += 1
            elif job.attempt < job.max_attempts and not lost_running_question:
                requeue_locked(
                    session,
                    job,
                    now_expr,
                    delay_seconds=job_rules.backoff_seconds(
                        job.attempt,
                        base_seconds=settings.job_retry_base_seconds,
                        max_seconds=settings.job_retry_max_seconds,
                        rand=rand,
                    ),
                    error_code="worker_lost",
                    error_summary="The worker stopped sending heartbeats; the job was rescheduled.",
                )
                requeued += 1
            else:
                finish_locked(
                    session,
                    job,
                    "failed",
                    now_expr,
                    error_code="worker_lost",
                    error_summary="The worker stopped sending heartbeats and no attempts remain.",
                )
                if question is not None:
                    qa_questions.close_lost_question(
                        session, question, status="failed", now=now or datetime.now(UTC)
                    )
                failed += 1
            logger.info(
                "job_lease_recovered",
                extra={"job_id": job.id, "job_kind": job.kind, "attempt": job.attempt},
            )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return requeued, failed, cancelled


def resend_unreceived_dispatches(
    session: Session, settings: Settings, *, now: datetime | None = None
) -> int:
    """已派送但 job 仍是 `queued` 的事件：超過等待時間就重設為 `pending`（再送）。

    等待時間 ＝ `DISPATCH_RESEND_AFTER_SECONDS`，每多送一次加倍，上限 60 分鐘
    （`job_rules.resend_interval_seconds`）。Redis 接受訊息不等於 worker 收到它
    （清空 Redis、訊息遺失），所以需要這個補強。排隊中的 job 本身不會因此失敗。
    """
    now_expr = db_now(now)
    try:
        current = session.execute(select(now_expr)).scalar_one()
        base = settings.dispatch_resend_after_seconds
        candidates = (
            session.execute(
                select(JobDispatch)
                .join(Job, Job.id == JobDispatch.job_id)
                .where(
                    JobDispatch.status == "sent",
                    Job.status == "queued",
                    JobDispatch.last_sent_at < now_expr - timedelta(seconds=base),
                )
                .order_by(JobDispatch.id)
                .limit(RECOVERY_BATCH_SIZE)
                .with_for_update(of=JobDispatch, skip_locked=True)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        due = [
            d.id
            for d in candidates
            if d.last_sent_at is not None
            and current - d.last_sent_at
            >= timedelta(seconds=job_rules.resend_interval_seconds(base, d.send_count))
        ]
        if due:
            session.execute(
                update(JobDispatch)
                .where(JobDispatch.id.in_(due))
                .values(status="pending", not_before=now_expr)
            )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return len(due)


def remove_finished_dispatches(session: Session) -> int:
    """刪除已結束（succeeded／failed／cancelled）工作的殘留事件（正常流程已在結束時刪除，這是補強）。"""
    try:
        result = session.execute(
            delete(JobDispatch).where(
                JobDispatch.job_id.in_(
                    select(Job.id).where(Job.status.in_(job_rules.TERMINAL_STATUSES))
                )
            )
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return int(cast(CursorResult[Any], result).rowcount)


def heal_missing_dispatches(
    session: Session, *, now: datetime | None = None, batch_size: int = RECOVERY_BATCH_SIZE
) -> int:
    """`queued` 的工作一定要有待派送事件（不能永久卡住）：沒有就補一筆（目標＝attempt＋1）。"""
    now_expr = db_now(now)
    try:
        jobs = (
            session.execute(
                select(Job)
                .where(
                    Job.status == "queued",
                    ~exists().where(JobDispatch.job_id == Job.id),
                )
                .order_by(Job.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .all()
        )
        for job in jobs:
            schedule_dispatch(
                session,
                job.id,
                attempt_target=job.attempt + 1,
                not_before=job.next_attempt_at or now_expr,
            )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return len(jobs)


def count_overdue_pending(
    session: Session, settings: Settings, *, now: datetime | None = None
) -> int:
    """`pending` 超過 `DISPATCH_RESEND_AFTER_SECONDS` 仍未被派送的事件數（job 仍 queued）。

    只回報與記錄警告，**不改動資料**：事件仍然有效，dispatcher 一恢復就會派送。
    這個數字大代表 dispatcher 或 broker 長時間不可用。
    """
    now_expr = db_now(now)
    threshold = now_expr - timedelta(seconds=settings.dispatch_resend_after_seconds)
    try:
        overdue = session.execute(
            select(Job.id)
            .join(JobDispatch, JobDispatch.job_id == Job.id)
            .where(
                JobDispatch.status == "pending",
                Job.status == "queued",
                JobDispatch.not_before < threshold,
            )
        ).all()
    finally:
        session.rollback()
    if overdue:
        logger.warning("dispatch_overdue", extra={"overdue_pending": len(overdue)})
    return len(overdue)


def run_recovery_scan(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    rand: Callable[[], float] = random.random,
) -> RecoveryResult:
    """一輪復原掃描（dispatcher 每 `RECOVERY_SCAN_SECONDS` 呼叫一次）。各步驟各自一個交易。"""
    requeued, failed, cancelled = recover_expired_leases(session, settings, now=now, rand=rand)
    return RecoveryResult(
        requeued=requeued,
        failed=failed,
        cancelled=cancelled,
        resent=resend_unreceived_dispatches(session, settings, now=now),
        removed_dispatches=remove_finished_dispatches(session),
        healed=heal_missing_dispatches(session, now=now),
        overdue_pending=count_overdue_pending(session, settings, now=now),
    )
