"""背景工作的狀態機（第二階段切片 2，D39）：建立、租約、令牌柵欄寫入、取消、重試。

純資料庫邏輯：沒有 Celery、Redis、Docker 依賴，也不執行任何實際工作（那是切片 3 起的 worker）。
每個公開函式自己控制交易（成功即 commit、失敗即 rollback），呼叫端拿到的 Session 在回傳時
沒有未結束的交易（`complete_job` 的 `before_publish` 回呼執行期間除外，見其說明）。

三個核心保護：
1. **去重**：`uq_jobs_dedupe_key_active` 部分唯一索引（`queued`／`running` 才唯一）；建立用
   `INSERT ... ON CONFLICT DO NOTHING`，由資料庫仲裁，不是先查再寫。
2. **租約**：取得租約是單一條件式 `UPDATE ... WHERE status='queued'`，競爭時只有一個贏；
   每次取得都產生新的 `lease_token`（uuid）。
3. **令牌柵欄（fencing）**：進度、心跳、完成、失敗一律帶令牌，條件為
   `id = ? AND status = 'running' AND lease_token = ?`；舊令牌（租約已被他人取得、工作已結束）
   一律被拒（`JobLeaseLostError`）且不寫入任何資料。令牌**不看租約是否過期**：過期只代表
   「復原掃描可以接手」，在掃描實際接手前，原擁有者仍可繼續（掃描的條件式更新與鎖會處理競爭）。

取消與完成靠列鎖決定先後：兩者都先 `SELECT ... FOR UPDATE` 同一列，後到者看到前者提交後的狀態。

時間：預設使用資料庫的 `clock_timestamp()`（API 與 worker 在不同機器／容器，時鐘以資料庫為準）；
所有函式接受 `now` 參數注入固定時間，測試不需要 sleep。
"""

import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, DateTime, delete, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job, JobDispatch
from repopilot.errors import (
    JobLeaseLostError,
    JobNotCancellableError,
    JobNotFoundError,
    JobNotRetryableError,
    RepositoryNotFoundError,
    SnapshotNotFoundError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import job_rules

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.jobs")

ACTIVE_DEDUPE_INDEX = "uq_jobs_dedupe_key_active"
DISPATCH_UNIQUE_CONSTRAINT = "uq_job_dispatches_job_id_attempt_target"
REPOSITORY_FOREIGN_KEY = "fk_jobs_repository_id_repositories"
SNAPSHOT_FOREIGN_KEY = "fk_jobs_snapshot_id_snapshots"

# 建立時遇到「衝突的進行中 job 在我們查詢前就結束」的競爭，重新嘗試的次數上限
_MAX_CREATE_ATTEMPTS = 5

_ACTIVE_PREDICATE = text("status IN ('queued', 'running')")

# `complete_job` 的 `before_publish` 回呼可以覆寫的欄位（D44）
_PUBLISH_OVERRIDES = frozenset({"repository_id", "snapshot_id", "result"})

# 每次重新排隊（新的執行嘗試）時歸零的進度欄位；`work_state` 不重設（毒藥檔案偵測要跨 attempt）
_PROGRESS_RESET: dict[str, Any] = {
    "processed_files": 0,
    "total_files": None,
    "indexed_files": 0,
    "parse_error_count": 0,
    "bytes_received": None,
}


def db_now(now: datetime | None = None) -> ColumnElement[Any]:
    """目前時間的 SQL 運算式：預設為資料庫時間，測試可傳入固定時間。"""
    if now is None:
        return func.clock_timestamp(type_=DateTime(timezone=True))
    return literal(now, DateTime(timezone=True))


def _log(event: str, job: Job, **extra: Any) -> None:
    # 只記錄識別資訊；絕不記錄令牌、params、work_state
    logger.info(
        event,
        extra={
            "job_id": job.id,
            "job_kind": job.kind,
            "job_status": job.status,
            "attempt": job.attempt,
            **extra,
        },
    )


@dataclass(frozen=True)
class CreatedJob:
    job: Job
    created: bool  # False＝回傳的是同一份工作已存在的進行中 job


@dataclass(frozen=True)
class CancelResult:
    job: Job
    # cancelled：已是（或剛變成）cancelled，API 回 200；requested：running，已記錄旗標，API 回 202
    outcome: Literal["cancelled", "requested"]


# --- 建立 ---


def create_job(
    session: Session,
    *,
    kind: str,
    dedupe_key: str,
    settings: Settings,
    repository_id: int | None = None,
    snapshot_id: int | None = None,
    repo_key: str | None = None,
    params: dict[str, Any] | None = None,
    import_profile_hash: str | None = None,
    index_profile_hash: str | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
) -> CreatedJob:
    """建立 job 與第一筆待派送事件（同一交易）；同 `dedupe_key` 已有進行中 job 時回傳既有者。

    transactional outbox：job 與事件要嘛一起提交、要嘛都不存在，所以不會有「job 排入了卻沒有人
    會派送它」。回傳的 `created` 標明是否新建（供 API 決定回應內容）。
    """
    if kind not in job_rules.JOB_KINDS:
        raise ValueError(f"Unknown job kind: {kind!r}.")
    if not dedupe_key:
        raise ValueError("dedupe_key must not be empty.")
    params = params or {}
    job_rules.assert_no_secret_keys(params, where="params")
    now_expr = db_now(now)

    for _ in range(_MAX_CREATE_ATTEMPTS):
        try:
            job = session.execute(
                pg_insert(Job)
                .values(
                    kind=kind,
                    status="queued",
                    phase="pending",
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                    dedupe_key=dedupe_key,
                    repo_key=repo_key,
                    params=params,
                    import_profile_hash=import_profile_hash,
                    index_profile_hash=index_profile_hash,
                    max_attempts=settings.job_max_attempts,
                    request_id=request_id,
                    created_at=now_expr,
                    updated_at=now_expr,
                )
                # 衝突目標＝部分唯一索引；他人的未提交 INSERT 會讓這裡等到對方結束
                .on_conflict_do_nothing(
                    index_elements=["dedupe_key"], index_where=_ACTIVE_PREDICATE
                )
                .returning(Job)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if job is not None:
                schedule_dispatch(session, job.id, attempt_target=1, not_before=now_expr)
                session.commit()
                _log("job_created", job)
                return CreatedJob(job, True)
        except IntegrityError as exc:
            session.rollback()
            _raise_for_foreign_key(exc)
            raise
        except BaseException:
            session.rollback()  # 例如事件寫入失敗：job 一併回復，不留下沒有事件的 job
            raise

        # 衝突：同一份工作已有進行中的 job
        session.rollback()
        existing = session.execute(
            select(Job)
            .where(Job.dedupe_key == dedupe_key, Job.status.in_(job_rules.ACTIVE_STATUSES))
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        session.rollback()
        if existing is not None:
            return CreatedJob(existing, False)
        # 對方在我們查詢前已結束：回頭再試一次 INSERT
    raise RuntimeError("Could not create or find an active job for the dedupe key.")


def _raise_for_foreign_key(exc: IntegrityError) -> None:
    message = str(exc.orig)
    if REPOSITORY_FOREIGN_KEY in message:
        # repo 在建立期間被移除（D33）：移除提交後外鍵檢查才失敗
        raise RepositoryNotFoundError("Repository not found.") from None
    if SNAPSHOT_FOREIGN_KEY in message:
        raise SnapshotNotFoundError("Snapshot not found.") from None


def schedule_dispatch(
    session: Session, job_id: int, *, attempt_target: int, not_before: ColumnElement[Any] | datetime
) -> None:
    """寫入（或重設為 pending）某次執行嘗試的待派送事件；不 commit，由呼叫端的交易一起提交。

    `(job_id, attempt_target)` 唯一，所以重複排程同一次嘗試（例如鎖忙碌延後、重送）是更新同一列，
    不會產生多筆事件；`send_count`／`failure_count` 保留累計。
    """
    statement = pg_insert(JobDispatch).values(
        job_id=job_id, attempt_target=attempt_target, status="pending", not_before=not_before
    )
    session.execute(
        statement.on_conflict_do_update(
            constraint=DISPATCH_UNIQUE_CONSTRAINT,
            set_={
                "status": "pending",
                "not_before": statement.excluded.not_before,
                "last_error": None,
            },
        )
    )


# --- 租約 ---


def acquire_lease(
    session: Session, job_id: int, settings: Settings, *, now: datetime | None = None
) -> Job | None:
    """worker 取得租約：條件式 UPDATE，多個競爭者中只有一個回傳 job（含新令牌），其餘回傳 None。

    `attempt` 加 1（第一次執行為 1）；每次都產生新令牌，所以前一個擁有者的令牌立即失效。
    回傳 None 代表工作已被取得、已取消、已結束，或尚未到 `next_attempt_at`；呼叫端丟棄該訊息。
    """
    now_expr = db_now(now)
    job = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == "queued",
            or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now_expr),
        )
        .values(
            status="running",
            attempt=Job.attempt + 1,
            lease_token=uuid4(),
            lease_expires_at=now_expr + timedelta(seconds=settings.job_lease_seconds),
            last_heartbeat_at=now_expr,
            started_at=func.coalesce(Job.started_at, now_expr),
            next_attempt_at=None,
            updated_at=now_expr,
        )
        .returning(Job)
        .execution_options(populate_existing=True, synchronize_session=False)
    ).scalar_one_or_none()
    session.commit()
    if job is not None:
        _log("job_lease_acquired", job)
    return job


def heartbeat(
    session: Session,
    job_id: int,
    token: UUID,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    """延長租約並回報取消旗標（`True`＝使用者已要求取消，worker 應在檢查點中止）。

    單一條件式 UPDATE（令牌柵欄）；令牌不符拋 `JobLeaseLostError`，不寫入任何資料。
    """
    now_expr = db_now(now)
    row = session.execute(
        update(Job)
        .where(*_fence(job_id, token))
        .values(
            last_heartbeat_at=now_expr,
            lease_expires_at=now_expr + timedelta(seconds=settings.job_lease_seconds),
            updated_at=now_expr,
        )
        .returning(Job.cancel_requested_at)
        .execution_options(synchronize_session=False)
    ).first()
    session.commit()
    if row is None:
        raise JobLeaseLostError("The job lease is no longer held by this worker.")
    return row[0] is not None


def read_cancel_requested(session: Session, job_id: int, token: UUID) -> bool:
    """讀取取消旗標（令牌柵欄；單列 SELECT，不鎖列、不延長租約）。

    給 clone／fetch 的輪詢使用（D44）：子程序執行期間每 `GIT_POLL_SECONDS` 秒問一次，
    比等心跳執行緒（`JOB_HEARTBEAT_SECONDS`）快。令牌不符拋 `JobLeaseLostError`。
    """
    row = session.execute(select(Job.cancel_requested_at).where(*_fence(job_id, token))).first()
    session.commit()  # 結束讀取交易，輪詢之間不持有連線
    if row is None:
        raise JobLeaseLostError("The job lease is no longer held by this worker.")
    return row[0] is not None


def _fence(job_id: int, token: UUID) -> list[ColumnElement[bool]]:
    """令牌柵欄條件：只有目前的租約擁有者能寫入。"""
    return [Job.id == job_id, Job.status == "running", Job.lease_token == token]


def _lock_fenced(session: Session, job_id: int, token: UUID) -> Job:
    """以列鎖取得「令牌仍有效」的 running job；否則 rollback 並拒絕。

    WHERE 條件在取得鎖之後會被資料庫重新檢查：等鎖期間若工作已被取消、完成或被他人接手，
    這裡會看不到該列。
    """
    job = session.execute(
        select(Job)
        .where(*_fence(job_id, token))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if job is None:
        session.rollback()
        raise JobLeaseLostError("The job lease is no longer held by this worker.")
    return job


def _lock_job(session: Session, job_id: int) -> Job:
    job = session.execute(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if job is None:
        session.rollback()
        raise JobNotFoundError("Job not found.")
    return job


def _update_locked(
    session: Session, job_id: int, now_expr: ColumnElement[Any], **values: Any
) -> Job:
    """更新已持有列鎖的 job 並回傳更新後的資料列（不 commit）。"""
    return session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(updated_at=now_expr, **values)
        .returning(Job)
        .execution_options(populate_existing=True, synchronize_session=False)
    ).scalar_one()


# --- 進度 ---


def update_progress(
    session: Session,
    job_id: int,
    token: UUID,
    *,
    phase: str | None = None,
    processed_files: int | None = None,
    total_files: int | None = None,
    indexed_files: int | None = None,
    parse_error_count: int | None = None,
    bytes_received: int | None = None,
    snapshot_id: int | None = None,
    work_state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job:
    """以令牌為柵欄更新階段與進度；計數只增不減（「進度單調不倒退」），階段只能前進。

    階段須屬於該種工作（規格 5.2），`completed` 只能由 `complete_job` 設定；違反是程式錯誤
    （`ValueError`）。`work_state` 以淺層合併更新，且不得含秘密鍵名。不延長租約（心跳另計）。
    """
    if work_state is not None:
        job_rules.assert_no_secret_keys(work_state, where="work_state")
    if phase == "completed":
        raise ValueError("The completed phase is set by complete_job only.")
    job = _lock_fenced(session, job_id, token)
    now_expr = db_now(now)
    values: dict[str, Any] = {}
    try:
        if phase is not None and phase != job.phase:
            if job_rules.phase_rank(job.kind, phase) < job_rules.phase_rank(job.kind, job.phase):
                raise ValueError(f"Phase cannot move backwards: {job.phase!r} -> {phase!r}.")
            values["phase"] = phase
            values["phase_started_at"] = now_expr
        elif phase is not None:
            job_rules.phase_rank(job.kind, phase)  # 仍檢查階段屬於該種工作
        if processed_files is not None:
            values["processed_files"] = max(job.processed_files, processed_files)
        if indexed_files is not None:
            values["indexed_files"] = max(job.indexed_files, indexed_files)
        if parse_error_count is not None:
            values["parse_error_count"] = max(job.parse_error_count, parse_error_count)
        if total_files is not None:
            values["total_files"] = total_files
        if bytes_received is not None:
            values["bytes_received"] = max(job.bytes_received or 0, bytes_received)
        if snapshot_id is not None:
            values["snapshot_id"] = snapshot_id
        if work_state is not None:
            values["work_state"] = {**job.work_state, **work_state}
        updated = _update_locked(session, job_id, now_expr, **values)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        _raise_for_foreign_key(exc)
        raise
    except BaseException:
        session.rollback()
        raise
    return updated


# --- 結束狀態 ---


def finish_locked(
    session: Session,
    job: Job,
    status: Literal["succeeded", "failed", "cancelled"],
    now_expr: ColumnElement[Any],
    **values: Any,
) -> Job:
    """把已持有列鎖的 job 設為終態（清除租約、記錄結束時間、刪除其待派送事件）；不 commit。"""
    session.execute(delete(JobDispatch).where(JobDispatch.job_id == job.id))
    return _update_locked(
        session,
        job.id,
        now_expr,
        status=status,
        lease_token=None,
        lease_expires_at=None,
        next_attempt_at=None,
        finished_at=now_expr,
        **values,
    )


def requeue_locked(
    session: Session,
    job: Job,
    now_expr: ColumnElement[Any],
    *,
    delay_seconds: float,
    attempt_delta: int = 0,
    max_attempts: int | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> Job:
    """把已持有列鎖的 job 放回 `queued` 並寫入下一次嘗試的待派送事件；不 commit。

    清除租約、取消旗標與結束時間，進度歸零（`work_state` 保留）。`delay_seconds` > 0 時寫入
    `next_attempt_at`，事件的 `not_before` 與它相同（退避由資料庫控制，不使用 broker 的 ETA）。
    `attempt_delta` 為 -1 時不消耗重試額度（repo 鎖忙碌）。錯誤欄位只在有給時覆寫，
    因此排隊中的 job 可以保留「上一次失敗的原因」。
    """
    values: dict[str, Any] = dict(_PROGRESS_RESET)
    if error_code is not None:
        values["error_code"] = error_code
        values["error_summary"] = job_rules.truncate_summary(error_summary)
    if max_attempts is not None:
        values["max_attempts"] = max_attempts
    new_attempt = job.attempt + attempt_delta
    # 只保留「下一次嘗試」的事件：舊嘗試的事件（已 sent）不再有意義，留著會被誤當成未接收而重送
    session.execute(
        delete(JobDispatch).where(
            JobDispatch.job_id == job.id, JobDispatch.attempt_target != new_attempt + 1
        )
    )
    next_attempt_at: ColumnElement[Any] | None = None
    if delay_seconds > 0:
        next_attempt_at = now_expr + timedelta(seconds=delay_seconds)
    updated = _update_locked(
        session,
        job.id,
        now_expr,
        status="queued",
        phase="pending",
        phase_started_at=None,
        attempt=new_attempt,
        lease_token=None,
        lease_expires_at=None,
        cancel_requested_at=None,
        finished_at=None,
        next_attempt_at=next_attempt_at,
        **values,
    )
    schedule_dispatch(
        session,
        job.id,
        attempt_target=new_attempt + 1,
        not_before=updated.next_attempt_at or now_expr,
    )
    return updated


def complete_job(
    session: Session,
    job_id: int,
    token: UUID,
    *,
    result: dict[str, Any] | None = None,
    snapshot_id: int | None = None,
    repository_id: int | None = None,
    before_publish: Callable[[Session], Mapping[str, Any] | None] | None = None,
    now: datetime | None = None,
) -> Job:
    """發布結果：單一交易內鎖定 job 列、比對令牌、檢查取消旗標，再設為 `succeeded`。

    - 令牌不符 → `JobLeaseLostError`，不寫入。
    - 已有取消請求 → 同一交易內把 job 設為 `cancelled` 並回傳（**不**呼叫 `before_publish`、
      不發布結果）；呼叫端（worker）據此回復自己建立的未完成資料。
    - `before_publish(session)` 在持有列鎖、確認可發布之後、狀態變更之前執行，供切片 3 把
      快照／索引的完成寫入放進同一交易（規格 5.8 的「提交前檢查點」）。它拋出例外則整個交易回復。
      這段期間列鎖被持有，所以並行的取消會等到本交易結束；結束後它會看到 `succeeded`，
      回 409 `job_not_cancellable`——**已發布成功的 job 不會被改成 cancelled**。
    - 回呼可以回傳 `repository_id`／`snapshot_id`／`result` 三個鍵（切片 5，D44）：GitHub 註冊在
      同一交易內才 INSERT repo 資料列，id 要到那時才知道，所以回呼提供要寫入 job 的值
      （取代呼叫端傳入的同名參數）。其他鍵是程式錯誤（`ValueError`）。
    """
    if result is not None:
        job_rules.assert_no_secret_keys(result, where="result")
    job = _lock_fenced(session, job_id, token)
    now_expr = db_now(now)
    try:
        if job.cancel_requested_at is not None:
            finished = finish_locked(session, job, "cancelled", now_expr)
        else:
            overrides: dict[str, Any] = {}
            if before_publish is not None:
                overrides = dict(before_publish(session) or {})
                unknown = overrides.keys() - _PUBLISH_OVERRIDES
                if unknown:
                    raise ValueError(f"before_publish returned unsupported keys: {sorted(unknown)}")
                if "result" in overrides:
                    job_rules.assert_no_secret_keys(overrides["result"], where="result")
            values: dict[str, Any] = {
                "phase": "completed",
                "phase_started_at": now_expr,
                "result": overrides.get("result", result),
                "error_code": None,
                "error_summary": None,
            }
            snapshot_id = overrides.get("snapshot_id", snapshot_id)
            repository_id = overrides.get("repository_id", repository_id)
            if snapshot_id is not None:
                values["snapshot_id"] = snapshot_id
            if repository_id is not None:
                values["repository_id"] = repository_id
            finished = finish_locked(session, job, "succeeded", now_expr, **values)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        _raise_for_foreign_key(exc)
        raise
    except BaseException:
        session.rollback()
        raise
    _log("job_finished", finished)
    return finished


def fail_job(
    session: Session,
    job_id: int,
    token: UUID,
    settings: Settings,
    *,
    error_code: str,
    error_summary: str | None = None,
    now: datetime | None = None,
    rand: Callable[[], float] = random.random,
) -> Job:
    """worker 回報失敗：依錯誤碼分類，暫時性且未達上限 → 退避後重新排隊；否則 `failed`。

    - 令牌不符 → `JobLeaseLostError`，不寫入。
    - 已有取消請求 → 不重試，直接 `cancelled`（使用者的意圖優先）。
    - 暫時性（`job_rules.TRANSIENT_ERROR_CODES`）且 `attempt < max_attempts` → `queued`，
      `next_attempt_at` ＝ now ＋ 退避，並寫入下一次嘗試的待派送事件。
    - 其餘（永久性、或達上限）→ `failed`，`error_code` 保留最後一次的錯誤碼。
    """
    job = _lock_fenced(session, job_id, token)
    now_expr = db_now(now)
    try:
        if job.cancel_requested_at is not None:
            updated = finish_locked(session, job, "cancelled", now_expr)
        elif (
            job_rules.classify_failure(error_code) == "transient" and job.attempt < job.max_attempts
        ):
            updated = requeue_locked(
                session,
                job,
                now_expr,
                delay_seconds=job_rules.backoff_seconds(
                    job.attempt,
                    base_seconds=settings.job_retry_base_seconds,
                    max_seconds=settings.job_retry_max_seconds,
                    rand=rand,
                ),
                error_code=error_code,
                error_summary=error_summary,
            )
        else:
            updated = finish_locked(
                session,
                job,
                "failed",
                now_expr,
                error_code=error_code,
                error_summary=job_rules.truncate_summary(error_summary),
            )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    _log("job_failed_reported", updated, error_code=error_code)
    return updated


def defer_job(
    session: Session,
    job_id: int,
    token: UUID,
    *,
    now: datetime | None = None,
    rand: Callable[[], float] = random.random,
) -> Job:
    """repo 鎖忙碌：放回 `queued` 並延後 5～15 秒，**不消耗重試額度**（attempt 減回）。

    已有取消請求時改為 `cancelled`。令牌不符 → `JobLeaseLostError`。
    """
    job = _lock_fenced(session, job_id, token)
    now_expr = db_now(now)
    try:
        if job.cancel_requested_at is not None:
            updated = finish_locked(session, job, "cancelled", now_expr)
        else:
            updated = requeue_locked(
                session,
                job,
                now_expr,
                delay_seconds=job_rules.defer_seconds(rand),
                attempt_delta=-1,
            )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    _log("job_deferred", updated)
    return updated


def acknowledge_cancel(
    session: Session, job_id: int, token: UUID, *, now: datetime | None = None
) -> Job:
    """worker 在檢查點發現取消旗標後確認：running → cancelled（合作式取消生效）。

    沒有取消請求就呼叫是程式錯誤（`ValueError`）；令牌不符 → `JobLeaseLostError`。
    """
    job = _lock_fenced(session, job_id, token)
    if job.cancel_requested_at is None:
        session.rollback()
        raise ValueError("Cancellation was not requested for this job.")
    try:
        cancelled = finish_locked(session, job, "cancelled", db_now(now))
        session.commit()
    except BaseException:
        session.rollback()
        raise
    _log("job_finished", cancelled)
    return cancelled


# --- 使用者操作：取消與重試 ---


def cancel_job(session: Session, job_id: int, *, now: datetime | None = None) -> CancelResult:
    """取消：`queued` 立即 `cancelled`；`running` 只記錄旗標；已 `cancelled` 冪等；其餘 409。

    先 `SELECT ... FOR UPDATE` 鎖列：與取得租約、發布結果互斥，後到者看到前者提交後的狀態。
    """
    job = _lock_job(session, job_id)
    status = job.status
    now_expr = db_now(now)
    try:
        if status == "queued":
            cancelled = finish_locked(session, job, "cancelled", now_expr)
            session.commit()
            _log("job_cancelled", cancelled)
            return CancelResult(cancelled, "cancelled")
        if status == "running":
            if job.cancel_requested_at is None:
                job = _update_locked(session, job_id, now_expr, cancel_requested_at=now_expr)
            session.commit()
            _log("job_cancel_requested", job)
            return CancelResult(job, "requested")
        session.rollback()  # 終態：沒有要寫的東西，放掉列鎖
    except BaseException:
        session.rollback()
        raise
    if status == "cancelled":
        return CancelResult(job, "cancelled")  # 冪等
    raise JobNotCancellableError("The job has already finished and cannot be cancelled.")


def retry_job(
    session: Session, job_id: int, settings: Settings, *, now: datetime | None = None
) -> Job:
    """重試 `failed` 的工作：同一 job id，`attempt` 繼續累加，`max_attempts` ＝ attempt ＋額度。

    其他狀態 → 409 `job_not_retryable`。若已有相同 `dedupe_key` 的進行中 job（使用者失敗後又重新
    提交過），重新排隊會違反唯一索引，同樣回 409。上一次的錯誤碼保留到下一次結束為止。
    """
    job = _lock_job(session, job_id)
    if job.status != "failed":
        session.rollback()
        raise JobNotRetryableError("Only failed jobs can be retried.")
    now_expr = db_now(now)
    try:
        updated = requeue_locked(
            session,
            job,
            now_expr,
            delay_seconds=0,
            max_attempts=job.attempt + settings.job_max_attempts,
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if ACTIVE_DEDUPE_INDEX in str(exc.orig):
            raise JobNotRetryableError(
                "An identical job is already in progress; retry is not needed."
            ) from None
        raise
    except BaseException:
        session.rollback()
        raise
    _log("job_retried", updated)
    return updated


def resubmit_job(
    session: Session,
    job_id: int,
    settings: Settings,
    *,
    request_id: str | None = None,
    params: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Job | None:
    """使用者再次提交同一份工作，而最近的 job 是 `failed` 或 `cancelled`：重新排入**同一個 job**
    （規格 4.4；`attempt` 繼續累加、`max_attempts` ＝ attempt ＋額度），回傳更新後的 job。

    與 `retry_job` 的差別：`retry_job` 只給 `failed`（使用者按「重試」），這裡也接受 `cancelled`
    （已取消的工作以建立端點重新提交，規格 4.6），並可更新 `request_id` 與 `params`（例如最新請求的
    revision 文字）。若 job 在此期間已不是這兩種狀態，或違反 `dedupe_key` 唯一性（同時有別人提交了
    相同工作），回傳 `None`，呼叫端應重新查詢目前的進行中 job。
    """
    job = _lock_job(session, job_id)
    if job.status not in ("failed", "cancelled"):
        session.rollback()
        return None
    now_expr = db_now(now)
    try:
        updated = requeue_locked(
            session,
            job,
            now_expr,
            delay_seconds=0,
            max_attempts=job.attempt + settings.job_max_attempts,
        )
        extra: dict[str, Any] = {}
        if request_id is not None:
            extra["request_id"] = request_id
        if params is not None:
            job_rules.assert_no_secret_keys(params, where="params")
            extra["params"] = params
        if extra:
            updated = _update_locked(session, job_id, now_expr, **extra)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if ACTIVE_DEDUPE_INDEX in str(exc.orig):
            return None
        raise
    except BaseException:
        session.rollback()
        raise
    _log("job_resubmitted", updated)
    return updated
