"""提交固定版本匯入 job（第二階段切片 3，規格 4.4，D40）。

API 在排入前**同步**完成這些事（所以不能宣稱 API 完全不執行 Git；Git 呼叫沿用
`GIT_TIMEOUT_SECONDS`）：

1. `resolve_registered_path`（D28，主機端的完整路徑重驗：junction／symlink 展開、根目錄、
   GitHub 固定位置）。
2. `ensure_repository_root` 與 `resolve_commit`：revision 解析為**完整 commit SHA**（無效回
   422，不建立 job）。固定版本原則（規則 2）：worker 之後只用這個 SHA，**不重新解析
   `HEAD`**，所以提交後即使分支前進或工作目錄被修改，匯入的仍是提交當下的 commit。
3. 路徑能映射到 worker 容器（否則 503 `worker_path_not_mapped`，這是部署設定問題）。
4. 匯入設定雜湊（D26）。

冪等規則（`dedupe_key` ＝ `import:{repository_id}:{commit_sha}:{import_profile_hash}`），
順序即優先序：

| 目前狀態 | 結果 |
| --- | --- |
| 有 `queued`／`running` 的同鍵 job | 回既有 job（202）；唯一性由部分唯一索引保證 |
| 快照已 `completed` | 不建立 job，200＋`reused=true` |
| 最近的 job 是 `failed`／`cancelled` | 重新排入**同一個 job**（202；延續第一階段的接手） |
| 快照 `importing` 且新鮮、這個鍵從來沒有 job | 409 `snapshot_import_in_progress`（同步匯入中） |
| 其他 | 建立新 job（202） |

本切片沒有索引（切片 6），所以規格 4.4 的「快照 completed、索引缺少或失敗 → 建立只含
indexing 階段的 job」那一列尚不存在：completed 快照一律回 200；切片 6 加入索引後再調整。

.NET 對照：像一個冪等的 `POST` 命令處理器——先查「已經有人在做／已經做完」，最後才真的寫入。
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job
from repopilot.errors import (
    GitCommandError,
    InvalidRepositoryPathError,
    PathNotMappedError,
    SnapshotImportInProgressError,
    WorkerPathNotMappedError,
)
from repopilot.git.objects import resolve_commit
from repopilot.git.repository import ensure_repository_root
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.path_mapping import to_container_path
from repopilot.services import job_rules
from repopilot.services import jobs as job_service
from repopilot.services.import_profile import ImportProfile
from repopilot.services.repositories import (
    get_repository,
    repository_key,
    resolve_registered_path,
)
from repopilot.services.snapshots import find_snapshot, raise_if_repository_removed

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.import_jobs")

_MAX_SUBMIT_ATTEMPTS = 5


@dataclass(frozen=True)
class ImportJobSubmission:
    job: Job | None
    snapshot_id: int | None
    repository_id: int
    reused: bool  # True＝快照已 completed，沒有新的工作（API 回 200）


def import_dedupe_key(repository_id: int, commit_sha: str, import_profile_hash: str) -> str:
    return f"import:{repository_id}:{commit_sha}:{import_profile_hash}"


def submit_import_job(
    session: Session,
    repository_id: int,
    revision: str,
    settings: Settings,
    *,
    request_id: str | None = None,
) -> ImportJobSubmission:
    repository = get_repository(session, repository_id)
    path = resolve_registered_path(repository, settings)  # D28：主機端的完整重驗
    host_path = repository.canonical_local_path
    key_for_repo = repository_key(repository)
    session.commit()  # 結束讀取交易，Git 作業期間不持有閒置連線

    try:
        ensure_repository_root(path, timeout=settings.git_timeout_seconds)
        commit_sha = resolve_commit(path, revision, timeout=settings.git_timeout_seconds)
    except (GitCommandError, InvalidRepositoryPathError):
        raise_if_repository_removed(session, repository_id)  # 與移除 repo（D33）競爭 → 404
        raise

    try:
        to_container_path(host_path, settings.worker_path_mappings)
    except PathNotMappedError:
        # 設定問題（WORKER_PATH_MAPPINGS 沒涵蓋這個 repo），不是使用者輸入錯誤
        raise WorkerPathNotMappedError(
            "The repository path cannot be mapped into the worker container; "
            "check WORKER_PATH_MAPPINGS and the REPOSITORY_ROOTS mounts."
        ) from None

    profile_hash = ImportProfile.from_settings(settings).hash()
    dedupe_key = import_dedupe_key(repository_id, commit_sha, profile_hash)
    params = {"requested_revision": revision, "commit_sha": commit_sha}

    for _ in range(_MAX_SUBMIT_ATTEMPTS):
        active = _find_active_job(session, dedupe_key)
        if active is not None:
            return ImportJobSubmission(active, active.snapshot_id, repository_id, reused=False)

        snapshot = find_snapshot(session, repository_id, commit_sha, profile_hash)
        session.rollback()
        if snapshot is not None and snapshot.status == "completed":
            return ImportJobSubmission(
                _latest_succeeded_job(session, snapshot.id), snapshot.id, repository_id, True
            )

        latest = _latest_job(session, dedupe_key)
        if latest is not None and latest.status in ("failed", "cancelled"):
            resubmitted = job_service.resubmit_job(
                session, latest.id, settings, request_id=request_id, params=params
            )
            if resubmitted is not None:
                return ImportJobSubmission(
                    resubmitted, resubmitted.snapshot_id, repository_id, reused=False
                )
            continue  # 狀態在這期間變了：重新判斷

        if latest is None and snapshot is not None and snapshot.status == "importing":
            fresh_after = datetime.now(UTC) - timedelta(seconds=settings.import_stale_seconds)
            if snapshot.started_at >= fresh_after:
                raise SnapshotImportInProgressError("The same snapshot is already being imported.")

        created = job_service.create_job(
            session,
            kind="snapshot_import",
            dedupe_key=dedupe_key,
            settings=settings,
            repository_id=repository_id,
            repo_key=key_for_repo,
            params=params,
            import_profile_hash=profile_hash,
            request_id=request_id,
        )
        return ImportJobSubmission(created.job, created.job.snapshot_id, repository_id, False)
    raise RuntimeError("Could not submit the import job.")


def _find_active_job(session: Session, dedupe_key: str) -> Job | None:
    job = session.scalars(
        select(Job)
        .where(Job.dedupe_key == dedupe_key, Job.status.in_(job_rules.ACTIVE_STATUSES))
        .execution_options(populate_existing=True)
    ).first()
    session.rollback()
    return job


def _latest_job(session: Session, dedupe_key: str) -> Job | None:
    job = session.scalars(
        select(Job)
        .where(Job.dedupe_key == dedupe_key)
        .order_by(Job.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    ).first()
    session.rollback()
    return job


def _latest_succeeded_job(session: Session, snapshot_id: int) -> Job | None:
    job = session.scalars(
        select(Job)
        .where(Job.snapshot_id == snapshot_id, Job.status == "succeeded")
        .order_by(Job.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    ).first()
    session.rollback()
    return job
