"""worker 執行一個匯入 job（第二階段切片 3，規格 5.5，D40）。

Celery 任務（`jobs/worker.py`）只是一層薄殼，把 `job_id` 交給這裡的 `run_job`；Celery 與
Redis 都不在這個模組裡，所以流程可以用真實 PostgreSQL 直接測試。

流程（每一步失敗的去向見各處註解）：

1. **取得租約**（`acquire_lease`，條件式 UPDATE）。拿不到（已被取得、已取消、已結束、
   不存在）→ 丟棄訊息。
2. 啟動**租約心跳執行緒**（`JOB_HEARTBEAT_SECONDS`）：延長租約並讀取取消旗標；令牌不符就
   設定「失去租約」旗標，主流程在下一個**檢查點**中止，**不寫任何結果**。
3. 載入 repo 與設定，驗證匯入設定雜湊（`profile_mismatch`）與固定的 commit SHA；
   **路徑映射與容器端重驗**（`resolve_worker_path`）。
4. 取鎖（規格 5.10，依「repo 鎖→快照鎖」順序，全是 `try`）：repo 級**共享**鎖＋快照級
   獨占鎖。取不到 → `defer_job`（放回佇列、延後 5～15 秒，**不消耗重試額度**）。
5. 呼叫**既有的** `snapshots.import_snapshot`（不複製匯入邏輯），傳入：已驗證的容器路徑、
   持有的快照鎖（`takeover`：可立即接手殘留的 importing 快照）、進度回呼、完成交易接手者。
6. **完成交易**：`complete_job` 先鎖 job 列、比對令牌、檢查取消旗標，再在同一交易內完成
   快照（檔案列＋`completed`）並設 job 為 `succeeded`——兩者一起提交或一起失敗。
7. 例外 → 分類（暫時性 vs 永久性，沿用 `job_rules`）→ `fail_job`（重排或 `failed`）。
   錯誤只留穩定的錯誤碼與固定的安全摘要：不含檔案內容、token、Git stderr；日誌含堆疊
   （不含參數值，D37）。
8. `finally` 停止心跳、釋放鎖。

**檢查點**（取消與租約遺失）：取得租約後、載入設定後、取鎖後、匯入的三個進度回報點
（總數已知／不需讀取的檔案分類完成／全部讀完）、發布結果前（`complete_job` 在列鎖內再檢查
一次，那是決定性的）。匯入的 blob 讀取是單一 `cat-file --batch`（D23），中途無法中止。

.NET 對照：像背景服務（`BackgroundService`）裡處理一則佇列訊息的方法；租約心跳類似可見性
逾時的續租，令牌柵欄像 ETag／樂觀並行：寫入時帶著「我還是不是擁有者」的證明。
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.db.locks import AdvisoryLock, acquire_advisory_lock, repo_lock_key, snapshot_lock_key
from repopilot.db.models import Job
from repopilot.errors import (
    GitCommandError,
    GitTimeoutError,
    ImportCancelledError,
    InvalidRevisionError,
    JobLeaseLostError,
    ProfileMismatchError,
    RepoPilotError,
    SnapshotImportInProgressError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.request_context import request_id_var
from repopilot.services import jobs as job_service
from repopilot.services import snapshots
from repopilot.services.import_profile import ImportProfile
from repopilot.services.repositories import get_repository, repository_key, resolve_worker_path

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.job_runner")

JobOutcome = Literal[
    "discarded",  # 沒有取得租約（重複訊息、已取消、已結束…），訊息丟棄
    "succeeded",
    "deferred",  # 鎖忙碌，放回佇列（不消耗額度）
    "failed",  # 永久性錯誤或已達上限
    "retry_scheduled",  # 暫時性錯誤，退避後重新排隊
    "cancelled",
    "lease_lost",  # 租約被他人取得：不寫任何結果
]

_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

# Git 逾時的固定摘要（R1）：掛載上的 repo 若有大量 loose 物件，讀取可能非常慢
GIT_TIMEOUT_SUMMARY = (
    "Reading the repository timed out. On a slow mount a repository with many loose objects "
    "can be slow; running `git gc` in that repository may speed up reading, or raise "
    "WORKER_GIT_TIMEOUT_SECONDS and WORKER_IMPORT_GIT_TIMEOUT_SECONDS."
)


def apply_worker_timeouts(settings: Settings) -> Settings:
    """worker 使用自己的 Git 逾時，取代給 API 用的 `GIT_TIMEOUT_SECONDS` 等設定。

    對應 `WORKER_GIT_TIMEOUT_SECONDS`、`WORKER_IMPORT_GIT_TIMEOUT_SECONDS`
    （R1：容器讀 Windows 掛載較慢）。
    """
    return settings.model_copy(
        update={
            "git_timeout_seconds": settings.worker_git_timeout_seconds,
            "import_git_timeout_seconds": settings.worker_import_git_timeout_seconds,
        }
    )


class LeaseHeartbeat:
    """租約心跳執行緒（規格 5.5 第 4 步）：延長租約、讀取取消旗標、偵測租約遺失。

    使用自己的 Session（每次心跳一個短交易），不與主流程共用連線。暫時性的資料庫錯誤只記
    日誌、下一輪再試（租約還沒過期之前不算失去）；令牌不符則停止，並讓 `checkpoint()` 拋
    `JobLeaseLostError`。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        job_id: int,
        token: UUID,
        settings: Settings,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._token = token
        self._settings = settings
        self._interval = (
            float(settings.job_heartbeat_seconds) if interval_seconds is None else interval_seconds
        )
        self._stop = threading.Event()
        self._lease_lost = threading.Event()
        self._cancel_requested = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"job-heartbeat-{job_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()

    def checkpoint(self) -> None:
        """檢查點：租約已遺失 → `JobLeaseLostError`；已要求取消 → `ImportCancelledError`。"""
        if self._lease_lost.is_set():
            raise JobLeaseLostError("The job lease was lost while the job was running.")
        if self._cancel_requested.is_set():
            raise ImportCancelledError("The job was cancelled.")

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._session_factory() as session:
                    if job_service.heartbeat(session, self._job_id, self._token, self._settings):
                        self._cancel_requested.set()
            except JobLeaseLostError:
                self._lease_lost.set()
                return
            except Exception as exc:
                logger.warning(
                    "job_heartbeat_failed",
                    extra={"job_id": self._job_id, "error_type": type(exc).__name__},
                )


class LockBusyError(Exception):
    """repo 鎖或快照鎖忙碌：延後重排，不消耗重試額度。"""


class UnsupportedJobKindError(RepoPilotError):
    code = "unsupported_job_kind"


def run_job(
    job_id: int,
    engine: Engine,
    settings: Settings,
    *,
    heartbeat_interval_seconds: float | None = None,
) -> JobOutcome:
    """執行一則 job 訊息；`settings` 應已套用 worker 的 Git 逾時（`apply_worker_timeouts`）。"""
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        job = job_service.acquire_lease(session, job_id, settings)
        if job is None:
            logger.info("job_message_discarded", extra={"job_id": job_id})
            return "discarded"
        token = job.lease_token
        if token is None:  # 不應發生：acquire_lease 一定設定令牌
            raise RuntimeError("Lease acquired without a token.")
        request_token = request_id_var.set(job.request_id)
        heartbeat = LeaseHeartbeat(
            session_factory, job.id, token, settings, interval_seconds=heartbeat_interval_seconds
        )
        heartbeat.start()
        try:
            return _run_leased(session, job, token, heartbeat, engine, settings)
        except JobLeaseLostError:
            # 租約已被他人取得（心跳逾時、被復原掃描接手）：不寫任何結果，新的擁有者負責
            logger.warning("job_lease_lost", extra={"job_id": job_id})
            return "lease_lost"
        finally:
            heartbeat.stop()
            request_id_var.reset(request_token)


def _run_leased(
    session: Session,
    job: Job,
    token: UUID,
    heartbeat: LeaseHeartbeat,
    engine: Engine,
    settings: Settings,
) -> JobOutcome:
    finished: dict[str, str] = {}  # 完成交易的結果（由 finalize 回呼寫入）
    try:
        if job.kind != "snapshot_import":
            raise UnsupportedJobKindError("This worker only runs snapshot_import jobs.")
        _run_import(session, job, token, heartbeat, engine, settings, finished)
    except JobLeaseLostError:
        raise
    except (LockBusyError, SnapshotImportInProgressError):
        # SnapshotImportInProgressError：持有快照鎖卻發現快照被他人接手（鎖連線曾中斷？），
        # 視同忙碌，稍後再看
        session.rollback()
        job_service.defer_job(session, job.id, token)
        return "deferred"
    except ImportCancelledError:
        session.rollback()
        if finished.get("status") == "cancelled":
            return "cancelled"  # 完成交易時發現取消旗標：complete_job 已把 job 設為 cancelled
        job_service.acknowledge_cancel(session, job.id, token)
        return "cancelled"
    except Exception as exc:
        session.rollback()
        code, summary = classify_error(exc)
        if code == "internal_error":
            logger.exception("job_unexpected_error", extra={"job_id": job.id})
        else:
            logger.warning("job_error", extra={"job_id": job.id, "error_code": code})
        updated = job_service.fail_job(
            session, job.id, token, settings, error_code=code, error_summary=summary
        )
        return _outcome_of(updated)
    return "succeeded"


def _outcome_of(job: Job) -> JobOutcome:
    if job.status == "queued":
        return "retry_scheduled"
    if job.status == "cancelled":
        return "cancelled"
    return "failed"


def classify_error(exc: BaseException) -> tuple[str, str]:
    """例外 → (穩定的錯誤碼, 固定的安全摘要)。

    暫時性或永久性由 `job_rules.classify_failure` 依錯誤碼決定。摘要**不含**路徑、檔案內容、
    token 或 Git stderr：領域例外的訊息本來就是固定文字，但 `GitCommandError` 的訊息夾帶
    stderr 片段，所以改用固定文字；Git 逾時另附 R1 的 `git gc` 提示。
    """
    if isinstance(exc, GitTimeoutError):
        return "git_timeout", GIT_TIMEOUT_SUMMARY
    if isinstance(exc, GitCommandError):
        return exc.code, "Git failed to read the repository; see the worker log for details."
    if isinstance(exc, RepoPilotError):
        return exc.code, exc.message
    if isinstance(exc, OperationalError | InterfaceError):
        return "database_unavailable", "The database was unavailable; the job will be retried."
    return "internal_error", f"Unexpected {type(exc).__name__}."


def _run_import(
    session: Session,
    job: Job,
    token: UUID,
    heartbeat: LeaseHeartbeat,
    engine: Engine,
    settings: Settings,
    finished: dict[str, str],
) -> None:
    heartbeat.checkpoint()
    if job.repository_id is None:
        raise InvalidRevisionError("The job has no repository.")
    repository = get_repository(session, job.repository_id)
    repository_id = repository.id
    repo_key = repository_key(repository)

    profile_hash = ImportProfile.from_settings(settings).hash()
    if job.import_profile_hash != profile_hash:
        raise ProfileMismatchError(
            "The worker's IMPORT_* settings differ from the API's; align them and retry."
        )
    commit_sha = job.params.get("commit_sha")
    if not isinstance(commit_sha, str) or not _FULL_SHA.match(commit_sha):
        raise InvalidRevisionError("The job has no valid commit SHA.")

    # 路徑映射＋容器端重驗（規格 9.6）；結果是容器內可讀的路徑
    path = resolve_worker_path(repository, settings)
    session.commit()  # 結束讀取交易；之後的長時間工作不持有閒置連線
    heartbeat.checkpoint()

    repo_lock: AdvisoryLock | None = None
    snapshot_lock: AdvisoryLock | None = None
    try:
        repo_lock = acquire_advisory_lock(engine, repo_lock_key(repo_key), shared=True)
        if repo_lock is None:
            raise LockBusyError
        snapshot_lock = acquire_advisory_lock(
            engine, snapshot_lock_key(repository_id, commit_sha, profile_hash)
        )
        if snapshot_lock is None:
            raise LockBusyError
        held_repo_lock = repo_lock
        heartbeat.checkpoint()
        job_service.update_progress(session, job.id, token, phase="importing")

        def on_progress(processed: int, total: int | None) -> None:
            heartbeat.checkpoint()
            job_service.update_progress(
                session, job.id, token, processed_files=processed, total_files=total
            )

        def finalize(snapshot_id: int, complete: Callable[[Session], None]) -> bool:
            # 發布前確認 repo 鎖連線仍存活（規格 5.10；快照鎖由匯入服務確認）
            held_repo_lock.assert_alive()
            # 完成交易：鎖 job 列、比對令牌、檢查取消旗標，
            # 再（在同一交易內）完成快照並設 succeeded
            done = job_service.complete_job(
                session,
                job.id,
                token,
                result={"snapshot_id": snapshot_id, "commit_sha": commit_sha},
                snapshot_id=snapshot_id,
                before_publish=complete,
            )
            finished["status"] = done.status
            return done.status == "succeeded"

        started = time.monotonic()
        snapshot, _created = snapshots.import_snapshot(
            session,
            repository_id,
            commit_sha,
            settings,
            path=path,
            takeover=snapshot_lock,
            on_progress=on_progress,
            finalize=finalize,
        )
        if "status" not in finished:
            # 快照在先前的 attempt 已完成（例如完成快照後、設 succeeded 前崩潰）：
            # 直接重用，補上 job 的完成
            done = job_service.complete_job(
                session,
                job.id,
                token,
                result={"snapshot_id": snapshot.id, "commit_sha": commit_sha, "reused": True},
                snapshot_id=snapshot.id,
            )
            finished["status"] = done.status
            if done.status == "cancelled":
                raise ImportCancelledError("The job was cancelled.")
        logger.info(
            "job_import_finished",
            extra={
                "job_id": job.id,
                "snapshot_id": snapshot.id,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
    finally:
        for lock in (snapshot_lock, repo_lock):  # 與取得順序相反
            if lock is not None:
                lock.release()
