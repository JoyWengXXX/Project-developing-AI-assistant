"""worker 執行 GitHub 註冊與 fetch job（第二階段切片 5，規格 5.5、5.8、5.12、9.7，D44）。

由 `job_runner._run_leased` 依 `job.kind` 呼叫；租約、心跳、錯誤分類與 `defer_job` 都在
`job_runner`，這裡只有「這兩種工作怎麼做」。clone、fetch 與資料列的細節**不複製**第一階段的邏輯：
`services/github_repositories.py` 的 `clone_and_publish`／`run_fetch` 與同步端點共用。

註冊（`github_register`），全程持有 repo **獨占** advisory lock（鍵＝owner／name，資料列尚不存在
也能鎖；取不到 → `LockBusyError` → `defer_job`，不消耗額度）：

1. `resolving`：驗證 `params`、讀取 token（有保存但無法解密 → 永久性
   `token_encryption_not_configured`；沒有保存就匿名）→ 查 GitHub（標準大小寫、private、預設分支）。
   已註冊（例如同步端點搶先）→ job 直接成功，`result.already_registered=true`。
2. `cloning`：先把暫存目錄名稱寫入 `work_state.temp_dir`、再建立目錄並 clone；clone 期間每
   `GIT_POLL_SECONDS` 秒檢查取消旗標與租約，每 `CLONE_PROGRESS_SECONDS` 秒量測暫存目錄大小寫入
   `progress.bytes_received`。取消或租約遺失 → 終止 git 程序樹、清掉暫存目錄。
3. `registering`：rename 為最終位置，然後**在完成 job 的同一交易內** INSERT repo 資料列
   （`complete_job(before_publish=…)`）：取消與成功由 job 列鎖決定先後；取消先成功 → 沒有資料列，
   本次 rename 出來的目錄被清掉。

token 只經 `GIT_CONFIG_*` 環境變數交給 git（`git/remote.py`）：不進 argv、日誌、Redis 訊息、
`params`／`work_state`／`result`，也不寫入 `.git/config`。

暫存目錄清理（規格 5.12）：取得鎖之後，先清理**上一個 attempt** 記錄的 `work_state.temp_dir`
（worker 被 SIGKILL 時才會留下；持有獨占鎖代表上一個擁有者已經不在了），並順便清理逾時的孤兒目錄
（`sweep_orphan_temp_dirs`，worker 啟動時也做一次）。**只刪名稱是本系統產生的 16 位 hex、位於
`.tmp` 直接底下的目錄**；已 rename 為最終位置但未註冊的目錄絕不刪除（D32）。

fetch（`github_fetch`）：路徑映射與容器端重驗（`resolve_worker_path`）→ repo 獨占鎖 →
`fetching` → `git fetch --prune`（同樣輪詢取消與租約）→ 完成，`result` 帶最多 100 筆 ref 差異。
"""

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import LockBusyError, acquire_advisory_lock, repo_lock_key
from repopilot.db.models import Job
from repopilot.errors import (
    GitHubRegistrationDisabledError,
    ImportCancelledError,
    PathNotMappedError,
    RepositoryNotFoundError,
    RepositoryNotGitHubError,
)
from repopilot.github.client import GitHubClient, GitHubRepo
from repopilot.github.repo_ref import parse_github_repo
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.path_mapping import to_host_path
from repopilot.services import github_credentials, job_rules, managed_repos
from repopilot.services import github_repositories as github_service
from repopilot.services import jobs as job_service
from repopilot.services.repositories import (
    get_repository,
    github_repo_key,
    repository_key,
    resolve_worker_path,
)

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.github_job_runner")

# 暫存目錄名稱寫入 work_state 的鍵（值是 16 位 hex 的名稱，不是路徑）
TEMP_DIR_KEY = "temp_dir"


class _Checkpoint(Protocol):
    def checkpoint(self) -> None: ...


def _make_poll(
    session: Session,
    job_id: int,
    token: UUID,
    heartbeat: _Checkpoint,
    settings: Settings,
) -> Callable[[Path | None], None]:
    """clone／fetch 子程序執行期間的輪詢：租約、取消旗標，以及（給了目錄時）`bytes_received`。

    在主執行緒（等待 git 的那條）被呼叫，所以可以安全使用 job 的 Session。取消旗標每次都讀
    （單列 SELECT）；位元組量測依 `CLONE_PROGRESS_SECONDS` 限流（遍歷目錄比查旗標貴）。
    """
    next_progress = time.monotonic() + settings.clone_progress_seconds

    def poll(measured_dir: Path | None) -> None:
        nonlocal next_progress
        heartbeat.checkpoint()
        if job_service.read_cancel_requested(session, job_id, token):
            raise ImportCancelledError("The job was cancelled.")
        if measured_dir is None:
            return
        now = time.monotonic()
        if now >= next_progress:
            next_progress = now + settings.clone_progress_seconds
            job_service.update_progress(
                session, job_id, token, bytes_received=managed_repos.directory_size(measured_dir)
            )

    return poll


def referenced_temp_dirs(engine: Engine) -> set[str]:
    """進行中（`queued`／`running`）的註冊 job 記錄的暫存目錄名稱：清理孤兒時一律保留。"""
    with Session(engine) as session:
        rows = session.execute(
            select(Job.work_state[TEMP_DIR_KEY].as_string()).where(
                Job.kind == "github_register", Job.status.in_(job_rules.ACTIVE_STATUSES)
            )
        )
        return {row[0] for row in rows if row[0]}


def sweep_orphans(engine: Engine, settings: Settings) -> list[str]:
    """清理 `.tmp` 底下逾時且沒有進行中 job 記錄的孤兒暫存目錄（worker 啟動與每次註冊 job 開始）。

    失敗只記錄、不影響 job；沒有設定受管目錄時什麼都不做。
    """
    managed_dir = settings.managed_repos_dir
    if managed_dir is None:
        return []
    try:
        removed = managed_repos.sweep_orphan_temp_dirs(
            managed_dir,
            older_than_seconds=settings.managed_temp_orphan_seconds,
            keep=referenced_temp_dirs(engine),
        )
    except Exception as exc:
        logger.warning("temp_orphan_sweep_failed", extra={"error_type": type(exc).__name__})
        return []
    if removed:
        logger.info("temp_orphans_removed", extra={"count": len(removed)})
    return removed


def run_register(
    session: Session,
    job: Job,
    token: UUID,
    heartbeat: _Checkpoint,
    engine: Engine,
    settings: Settings,
    finished: dict[str, str],
    client: GitHubClient,
) -> None:
    heartbeat.checkpoint()
    raw = job.params.get("github_repo")
    ref = parse_github_repo(
        raw if isinstance(raw, str) else ""
    )  # 不合法 → 永久性 invalid_github_repo
    managed_dir = settings.managed_repos_dir
    if managed_dir is None:
        raise GitHubRegistrationDisabledError(
            "GitHub registration is disabled: set MANAGED_REPOS_DIR in the worker environment."
        )
    github_token = github_credentials.get_required_token(session, settings)
    previous_temp = job.work_state.get(TEMP_DIR_KEY)
    job_id = job.id
    session.commit()  # 結束讀取交易；之後的長時間工作不持有閒置連線

    lock = acquire_advisory_lock(engine, repo_lock_key(github_repo_key(ref.owner, ref.name)))
    if lock is None:
        raise LockBusyError
    try:
        heartbeat.checkpoint()
        if managed_repos.remove_recorded_temp_dir(managed_dir, previous_temp):
            logger.info("previous_attempt_temp_removed")
        if previous_temp is not None:
            job_service.update_progress(session, job_id, token, work_state={TEMP_DIR_KEY: None})
        sweep_orphans(engine, settings)

        job_service.update_progress(session, job_id, token, phase="resolving")
        info = client.get_repository(ref.owner, ref.name, github_token)
        existing = github_service.find_github_repository(session, info.owner, info.name)
        session.commit()
        if existing is not None:
            done = job_service.complete_job(
                session,
                job_id,
                token,
                result=_register_result(info, existing.id, already_registered=True),
                repository_id=existing.id,
            )
            finished["status"] = done.status
            if done.status == "cancelled":
                raise ImportCancelledError("The job was cancelled.")
            return

        heartbeat.checkpoint()
        job_service.update_progress(session, job_id, token, phase="cloning")
        poll = _make_poll(session, job_id, token, heartbeat, settings)

        def record_temp_dir(name: str) -> None:
            job_service.update_progress(session, job_id, token, work_state={TEMP_DIR_KEY: name})

        def before_rename() -> None:
            # 發布前確認鎖連線仍存活（鎖若默默消失，另一個擁有者可能已在 clone 同一個 repo）
            lock.assert_alive()
            heartbeat.checkpoint()
            job_service.update_progress(session, job_id, token, phase="registering")

        hooks = github_service.CloneHooks(
            record_temp_dir=record_temp_dir,
            poll=poll,
            poll_interval=settings.git_poll_seconds,
            checkpoint=before_rename,
        )
        try:
            published = github_service.clone_and_publish(
                info, github_token, settings, managed_dir, hooks
            )
        except github_service.DestinationOccupiedError:
            existing = github_service.find_github_repository(session, info.owner, info.name)
            session.commit()
            if existing is None:
                raise github_service.occupied_destination_error() from None
            done = job_service.complete_job(
                session,
                job_id,
                token,
                result=_register_result(info, existing.id, already_registered=True),
                repository_id=existing.id,
            )
            finished["status"] = done.status
            if done.status == "cancelled":
                raise ImportCancelledError("The job was cancelled.") from None
            return

        # 從這裡起 destination 是本次建立的：成功以外的任何結果都要清掉它（除非資料列指向它）。
        # 資料列要存**主機路徑**（D18）：容器內的 /mnt/managed/… 換回 WORKER_PATH_MAPPINGS 的 host
        try:
            host_path = to_host_path(
                str(published.destination.resolve(strict=True)), settings.worker_path_mappings
            )
        except PathNotMappedError:
            # 設定問題（映射缺漏）：資料列不可能存在，直接清掉本次建立的目錄
            github_service.remove_destination_unless_registered(
                published.destination, None, published.managed_root
            )
            raise
        try:
            job_service.update_progress(session, job_id, token, work_state={TEMP_DIR_KEY: None})
            done = job_service.complete_job(
                session,
                job_id,
                token,
                before_publish=_insert_in_completion(info, published, host_path),
            )
        except BaseException:
            _discard_published(session, info, published, host_path)
            raise
        finished["status"] = done.status
        if done.status == "cancelled":
            # 取消先贏：沒有資料列，目錄也不留
            _discard_published(session, info, published, host_path)
            raise ImportCancelledError("The job was cancelled.")
    finally:
        lock.release()


def _register_result(
    info: GitHubRepo, repository_id: int, *, already_registered: bool
) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "github_repo": f"{info.owner}/{info.name}",
        "already_registered": already_registered,
    }


def _insert_in_completion(
    info: GitHubRepo, published: github_service.PublishedClone, host_path: str
) -> Callable[[Session], dict[str, Any]]:
    """`complete_job` 的 `before_publish`：在持有 job 列鎖的同一交易內 INSERT repo 資料列。"""

    def insert(completion_session: Session) -> dict[str, Any]:
        repository = github_service.new_repository_row(
            info, published.destination, host_path=host_path
        )
        completion_session.add(repository)
        completion_session.flush()  # 取得 id；提交由 complete_job 負責
        return {
            "repository_id": repository.id,
            "result": _register_result(info, repository.id, already_registered=False),
        }

    return insert


def _discard_published(
    session: Session, info: GitHubRepo, published: github_service.PublishedClone, host_path: str
) -> None:
    """清掉本次 rename 出來的最終位置，除非資料列已指向它。

    查詢資料列失敗（資料庫中斷）時**不刪**：無法確定它是否已註冊，寧可留下（之後的 attempt 會
    回報 `managed_storage_error`，由使用者處理）也不刪掉已註冊的 clone。
    """
    try:
        session.rollback()
        existing = github_service.find_github_repository(session, info.owner, info.name)
        session.commit()
    except Exception as exc:
        logger.error("published_clone_kept", extra={"error_type": type(exc).__name__})
        return
    github_service.remove_destination_unless_registered(
        published.destination, existing, published.managed_root, registered_path=host_path
    )


def run_fetch(
    session: Session,
    job: Job,
    token: UUID,
    heartbeat: _Checkpoint,
    engine: Engine,
    settings: Settings,
    finished: dict[str, str],
) -> None:
    heartbeat.checkpoint()
    if job.repository_id is None:
        raise RepositoryNotFoundError("The job has no repository.")
    repository = get_repository(session, job.repository_id)
    if repository.source_type != "github":
        raise RepositoryNotGitHubError("Only GitHub repositories can be fetched.")
    if settings.managed_repos_dir is None:
        raise GitHubRegistrationDisabledError(
            "GitHub support is disabled: set MANAGED_REPOS_DIR in the worker environment."
        )
    path = resolve_worker_path(repository, settings)  # 路徑映射＋容器端重驗（規格 9.6）
    repository_id = repository.id
    key = repository_key(repository)
    job_id = job.id
    github_token = github_credentials.get_required_token(session, settings)
    session.commit()  # 結束讀取交易

    lock = acquire_advisory_lock(engine, repo_lock_key(key))
    if lock is None:
        raise LockBusyError
    try:
        heartbeat.checkpoint()
        job_service.update_progress(session, job_id, token, phase="fetching")
        poll = _make_poll(session, job_id, token, heartbeat, settings)
        result, changed = github_service.run_fetch(
            path,
            github_token,
            settings,
            repository_id=repository_id,
            poll=lambda: poll(None),
        )
        lock.assert_alive()
        done = job_service.complete_job(
            session,
            job_id,
            token,
            result={
                "repository_id": repository_id,
                "changed_refs": changed,
                "truncated": result.truncated,
                "updated_refs": [
                    {
                        "name": c.name,
                        "kind": c.kind,
                        "change": c.change,
                        "old_sha": c.old_sha,
                        "new_sha": c.new_sha,
                    }
                    for c in result.updated_refs
                ],
            },
        )
        finished["status"] = done.status
        if done.status == "cancelled":
            raise ImportCancelledError("The job was cancelled.")
    finally:
        lock.release()
