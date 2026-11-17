"""worker 執行一個匯入 job（第二階段切片 3，規格 5.5，D40）。

Celery 任務（`jobs/worker.py`）只是一層薄殼，把 `job_id` 交給這裡的 `run_job`；Celery 與
Redis 都不在這個模組裡（唯一例外：錯誤分類引用 Celery 的 soft time limit 例外），
所以流程可以用真實 PostgreSQL 直接測試。

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

**檢查點**（取消與租約遺失）：取得租約後、載入設定後、取鎖後、匯入的進度回報點（總數已知／不需讀取
的檔案分類完成／**每一批 blob 讀完**）、每一批讀取前、發布結果前（`complete_job` 在列鎖內再檢查
一次，那是決定性的）。blob 以分批方式讀取（D43，每批一個 `cat-file --batch`），所以取消與租約遺失
最多延遲「一批」的時間；進度回報本身會讀取取消旗標（不必等心跳執行緒的下一輪）。

.NET 對照：像背景服務（`BackgroundService`）裡處理一則佇列訊息的方法；租約心跳類似可見性
逾時的續租，令牌柵欄像 ETag／樂觀並行：寫入時帶著「我還是不是擁有者」的證明。
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import Engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.db.locks import (
    AdvisoryLock,
    LockBusyError,
    acquire_advisory_lock,
    index_lock_key,
    repo_lock_key,
    snapshot_lock_key,
)
from repopilot.db.models import Job, Snapshot
from repopilot.errors import (
    GitCommandError,
    GitHubApiError,
    GitHubCloneError,
    GitHubCloneTimeoutError,
    GitHubFetchError,
    GitHubFetchTimeoutError,
    GitTimeoutError,
    ImportCancelledError,
    InvalidRevisionError,
    JobLeaseLostError,
    ProfileMismatchError,
    RepoPilotError,
    SnapshotImportInProgressError,
    SnapshotNotFoundError,
)
from repopilot.github.client import GitHubClient
from repopilot.indexing.profile import IndexProfile
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.request_context import request_id_var
from repopilot.services import (
    github_job_runner,
    job_poison,
    retrieval_job_runner,
    snapshot_indexing,
    snapshots,
)
from repopilot.services import jobs as job_service
from repopilot.services.import_profile import ImportProfile
from repopilot.services.repositories import get_repository, repository_key, resolve_worker_path
from repopilot.services.retrieval_build import SliceExhausted

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
# clone／fetch 逾時（D44）：錯誤碼與 Git 逾時相同（暫時性，會退避重試），摘要改指向對應的設定
NETWORK_TIMEOUT_SUMMARY = (
    "git clone or fetch timed out. For a large repository raise GITHUB_CLONE_TIMEOUT_SECONDS "
    "(and WORKER_TASK_TIME_LIMIT_SECONDS); the job will be retried."
)
# GitHub API 回 5xx（暫時性）；摘要固定，不含 GitHub 回應內容
GITHUB_SERVER_ERROR_SUMMARY = "GitHub returned a server error; the job will be retried."
# worker 的單一任務時限（Celery soft time limit）已到：永久性失敗，不自動重試；摘要固定
TIME_LIMIT_SUMMARY = (
    "The job reached the worker's task time limit before it finished. "
    "WORKER_TASK_TIME_LIMIT_SECONDS sets that limit."
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


class UnsupportedJobKindError(RepoPilotError):
    code = "unsupported_job_kind"


def run_job(
    job_id: int,
    engine: Engine,
    settings: Settings,
    *,
    heartbeat_interval_seconds: float | None = None,
    github_client: GitHubClient | None = None,
) -> JobOutcome:
    """執行一則 job 訊息；`settings` 應已套用 worker 的 Git 逾時（`apply_worker_timeouts`）。

    `github_client` 只給 `github_register` 用；省略時依設定建立（測試以 `MockTransport` 注入）。
    """
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
            return _run_leased(session, job, token, heartbeat, engine, settings, github_client)
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
    github_client: GitHubClient | None = None,
) -> JobOutcome:
    finished: dict[str, str] = {}  # 完成交易的結果（由 finalize 回呼寫入）
    try:
        if job.kind == "snapshot_import":
            _run_import(session, job, token, heartbeat, engine, settings, finished)
        elif job.kind == "snapshot_index":
            _run_index(session, job, token, heartbeat, engine, settings, finished)
        elif job.kind == "snapshot_retrieval":
            retrieval_job_runner.run_retrieval(
                session, job, token, engine, settings, heartbeat.checkpoint, finished
            )
        elif job.kind == "github_register":
            client = github_client or GitHubClient(
                base_url=settings.github_api_base_url,
                timeout_seconds=settings.github_api_timeout_seconds,
            )
            github_job_runner.run_register(
                session, job, token, heartbeat, engine, settings, finished, client
            )
        elif job.kind == "github_fetch":
            github_job_runner.run_fetch(session, job, token, heartbeat, engine, settings, finished)
        else:
            raise UnsupportedJobKindError("This worker does not run this kind of job.")
    except JobLeaseLostError:
        raise
    except (LockBusyError, SnapshotImportInProgressError):
        # SnapshotImportInProgressError：持有快照鎖卻發現快照被他人接手（鎖連線曾中斷？），
        # 視同忙碌，稍後再看
        session.rollback()
        job_service.defer_job(session, job.id, token)
        return "deferred"
    except SliceExhausted:
        # 檢索索引的時間切片用完：已完成的片段都已寫入（下一次接手時續跑）。放回佇列、不消耗
        # 重試額度；先讓出 worker，排在佇列中的其他工作可以先執行
        session.rollback()
        job_service.defer_job(session, job.id, token)
        logger.info("retrieval_slice_deferred", extra={"job_id": job.id})
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
    if isinstance(exc, SoftTimeLimitExceeded):
        return "job_time_limit_exceeded", TIME_LIMIT_SUMMARY
    if isinstance(exc, GitHubCloneTimeoutError | GitHubFetchTimeoutError):
        return "git_timeout", NETWORK_TIMEOUT_SUMMARY
    if isinstance(exc, GitTimeoutError):
        return "git_timeout", GIT_TIMEOUT_SUMMARY
    if isinstance(exc, GitHubCloneError | GitHubFetchError):
        # 訊息本來就是固定文字（不含 stderr）；stderr 已遮蔽後只進 worker 日誌
        return exc.code, exc.message
    if isinstance(exc, GitCommandError):
        return exc.code, "Git failed to read the repository; see the worker log for details."
    if isinstance(exc, GitHubApiError) and exc.status is not None and exc.status >= 500:
        return "github_server_error", GITHUB_SERVER_ERROR_SUMMARY
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
    _verify_index_profile(job, settings)
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
        # 毒藥單位框架（D43）：先結算上一個 attempt 留下的「目前單位」（上次以 worker_lost 結束
        # 且指標還在＝崩潰在那個單位），之後每一批 blob 讀取前寫入新的目前單位。匯入不可略過
        # 任何一批（會讓快照殘缺），所以這裡只留下紀錄；重複崩潰的結局仍是達上限後 failed
        # （worker_lost）
        tracker = _poison_tracker(session, job, token)
        tracker.begin_attempt(job.error_code)
        if tracker.summary.suspect_crashes >= job_poison.SKIP_THRESHOLD:
            logger.warning(
                "job_unit_crashed_repeatedly",
                extra={"job_id": job.id, "crashes": tracker.summary.suspect_crashes},
            )
        job_service.update_progress(session, job.id, token, phase="importing")

        def on_progress(processed: int, total: int | None) -> None:
            heartbeat.checkpoint()
            updated = job_service.update_progress(
                session, job.id, token, processed_files=processed, total_files=total
            )
            # 進度寫入本來就要鎖 job 列，順便讀取取消旗標：取消在下一次進度回報（最多一批之後）
            # 生效，不必等心跳執行緒的下一輪（JOB_HEARTBEAT_SECONDS）
            if updated.cancel_requested_at is not None:
                raise ImportCancelledError("The job was cancelled.")

        def on_batch(batch_id: str, number: int, count: int) -> None:
            heartbeat.checkpoint()
            tracker.enter(
                job_poison.WorkUnit(batch_id, "blob_batch", "worker_crash", number, count)
            )

        def finalize(snapshot_id: int, complete: Callable[[Session], None]) -> bool:
            # 發布前確認 repo 鎖連線仍存活（規格 5.10；快照鎖由匯入服務確認）
            held_repo_lock.assert_alive()
            # 發布快照：鎖 job 列、比對令牌、檢查取消旗標，再（在同一交易內）完成快照並把
            # snapshot_id 記到 job。job 仍是 running——之後還有索引階段（切片 6）；已發布的快照
            # 不會因為索引階段的失敗或取消而失效
            done = job_service.publish_step(
                session, job.id, token, snapshot_id=snapshot_id, before_publish=complete
            )
            if done.status == "cancelled":
                finished["status"] = "cancelled"
                return False
            return True

        started = time.monotonic()
        snapshot, created = snapshots.import_snapshot(
            session,
            repository_id,
            commit_sha,
            settings,
            path=path,
            takeover=snapshot_lock,
            on_progress=on_progress,
            on_batch=on_batch,
            finalize=finalize,
        )
        logger.info(
            "job_import_finished",
            extra={
                "job_id": job.id,
                "snapshot_id": snapshot.id,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
        snapshot_lock.release()  # 索引階段不需要快照鎖（只有同一快照的匯入者需要它）
        snapshot_lock = None
        result: dict[str, Any] = {"snapshot_id": snapshot.id, "commit_sha": commit_sha}
        if not created:
            # 快照在先前的 attempt 已完成（例如完成快照後、索引階段崩潰）：直接重用
            result["reused"] = True
        _run_index_phase(
            session,
            job,
            token,
            heartbeat,
            engine,
            settings,
            snapshot.id,
            tracker,
            held_repo_lock,
            finished,
            result,
        )
    finally:
        for lock in (snapshot_lock, repo_lock):  # 與取得順序相反
            if lock is not None:
                lock.release()


def _verify_index_profile(job: Job, settings: Settings) -> None:
    """索引設定雜湊必須與 API 排入時一致（`profile_mismatch`，永久性）。

    `None` 是切片 6 之前建立的 job（沒有記錄索引雜湊）：不比對，直接用 worker 目前的設定。
    """
    if job.index_profile_hash is None:
        return
    if job.index_profile_hash != IndexProfile.from_settings(settings).hash():
        raise ProfileMismatchError(
            "The worker's INDEX_* settings or Python version differ from the API's; "
            "align them and retry."
        )


def _poison_tracker(session: Session, job: Job, token: UUID) -> job_poison.PoisonTracker:
    """毒藥單位追蹤器：寫入是單一語句的令牌柵欄更新（每個檔案一次，必須便宜）。"""
    return job_poison.PoisonTracker(
        job.work_state, lambda patch: job_service.merge_work_state(session, job.id, token, patch)
    )


def _run_index(
    session: Session,
    job: Job,
    token: UUID,
    heartbeat: LeaseHeartbeat,
    engine: Engine,
    settings: Settings,
    finished: dict[str, str],
) -> None:
    """`snapshot_index` job：只讀資料庫裡的快照內容，建立（或重建）結構索引；不碰 Git。"""
    heartbeat.checkpoint()
    if job.snapshot_id is None:
        raise SnapshotNotFoundError("The job has no snapshot.")
    snapshot = session.get(Snapshot, job.snapshot_id)
    if snapshot is None:
        raise SnapshotNotFoundError("Snapshot not found.")
    snapshot_id = snapshot.id
    repository = get_repository(session, snapshot.repository_id)
    repo_key = repository_key(repository)
    _verify_index_profile(job, settings)
    session.commit()

    repo_lock: AdvisoryLock | None = None
    try:
        # 索引只讀資料庫，不需要 repo 的獨占存取；取共享鎖是為了讓移除 repo（獨占鎖）等不到
        repo_lock = acquire_advisory_lock(engine, repo_lock_key(repo_key), shared=True)
        if repo_lock is None:
            raise LockBusyError
        tracker = _poison_tracker(session, job, token)
        tracker.begin_attempt(job.error_code)
        _run_index_phase(
            session,
            job,
            token,
            heartbeat,
            engine,
            settings,
            snapshot_id,
            tracker,
            repo_lock,
            finished,
            {"snapshot_id": snapshot_id},
        )
    finally:
        if repo_lock is not None:
            repo_lock.release()


def _run_index_phase(
    session: Session,
    job: Job,
    token: UUID,
    heartbeat: LeaseHeartbeat,
    engine: Engine,
    settings: Settings,
    snapshot_id: int,
    tracker: job_poison.PoisonTracker,
    repo_lock: AdvisoryLock,
    finished: dict[str, str],
    base_result: dict[str, Any],
) -> None:
    """索引階段（匯入 job 的最後一段，或 `index-jobs` 的全部）；結束時 job 為 `succeeded`。

    索引的完成與 job 的 `succeeded` 在同一個交易提交（`finalize` → `complete_job`）。目前設定的
    索引已經 `completed`（先前的 attempt 做完、設 `succeeded` 前崩潰）時直接完成 job。
    """
    profile_hash = IndexProfile.from_settings(settings).hash()
    heartbeat.checkpoint()
    job_service.update_progress(session, job.id, token, phase="indexing")
    if tracker.summary.suspect_crashes >= job_poison.SKIP_THRESHOLD:
        logger.warning(
            "job_unit_crashed_repeatedly",
            extra={"job_id": job.id, "crashes": tracker.summary.suspect_crashes},
        )

    index_lock = acquire_advisory_lock(engine, index_lock_key(snapshot_id, profile_hash))
    if index_lock is None:
        raise LockBusyError
    try:

        def on_total(total: int) -> None:
            job_service.update_progress(session, job.id, token, total_files=total)

        def on_batch(indexed: int, parse_errors: int) -> None:
            heartbeat.checkpoint()
            updated = job_service.update_progress(
                session, job.id, token, indexed_files=indexed, parse_error_count=parse_errors
            )
            if updated.cancel_requested_at is not None:
                raise ImportCancelledError("The job was cancelled.")

        def finalize(
            summary: snapshot_indexing.IndexSummary, complete: Callable[[Session], None]
        ) -> bool:
            repo_lock.assert_alive()
            done = job_service.complete_job(
                session,
                job.id,
                token,
                result={**base_result, **summary.as_result()},
                snapshot_id=snapshot_id,
                before_publish=complete,
            )
            finished["status"] = done.status
            return done.status == "succeeded"

        started = time.monotonic()
        summary = snapshot_indexing.build_index(
            session,
            snapshot_id,
            settings,
            owner_token=token,
            lock=index_lock,
            tracker=tracker,
            on_total=on_total,
            on_batch=on_batch,
            finalize=finalize,
        )
        if "status" not in finished:
            # 索引在先前的 attempt 已完成：直接完成 job
            done = job_service.complete_job(
                session,
                job.id,
                token,
                result={**base_result, **summary.as_result()},
                snapshot_id=snapshot_id,
            )
            finished["status"] = done.status
            if done.status == "cancelled":
                raise ImportCancelledError("The job was cancelled.")
        logger.info(
            "job_index_finished",
            extra={
                "job_id": job.id,
                "snapshot_id": snapshot_id,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
    finally:
        index_lock.release()
    snapshot_indexing.delete_outdated_indexes(session, snapshot_id, profile_hash)
