"""移除已註冊的 repo（D33）：刪除資料庫紀錄，GitHub 來源另外刪除受管目錄內的 clone。

順序（失敗時資料列仍在、可重試）：
1. 取得 GitHub 來源的行程內鎖（與 clone／fetch 共用，取不到回 409）。
2. 開交易並以 `SELECT ... FOR UPDATE` 鎖住 repo 資料列：匯入新增快照需要對該列取 `FOR KEY SHARE`，
   會被擋到本交易結束，所以「檢查沒有匯入中」與「刪除」之間不會被插入新的匯入。
3. 有未逾時的 `importing` 快照 → 409（逾時者視為可刪除，與 D24 的接手規則一致）。
4. GitHub 來源：驗證路徑後刪除 clone 目錄（`managed_repos.remove_github_clone`）。
5. 以 SQL 層級依序刪除 snapshot_files → snapshots → repository，同一交易提交。

目錄刪除成功但資料庫提交失敗時，重試會把「目錄已不存在」視為成功，不會卡住。刪除過程持有資料庫
連線與列鎖，但只做本機檔案操作（沒有網路、沒有 Git），對本機 clone 通常是秒級。
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, delete, exists, select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import engine_of, repo_lock_key, try_advisory_lock
from repopilot.db.models import Repository, Snapshot, SnapshotFile
from repopilot.errors import (
    GitHubRegistrationDisabledError,
    RepositoryBusyError,
    RepositoryNotFoundError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import managed_repos
from repopilot.services.github_repositories import exclusive_repo_access
from repopilot.services.repositories import get_repository, repository_key

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.repository_removal")


@dataclass(frozen=True)
class RemovalResult:
    repository_id: int
    source_type: Literal["local", "github"]
    deleted_snapshots: int
    deleted_files: int
    removed_clone: bool


def remove_repository(session: Session, repository_id: int, settings: Settings) -> RemovalResult:
    """移除 repo 與其所有快照、檔案；local 來源絕不碰磁碟上的 repo 目錄。

    第二階段切片 3：先以 `try` 取得 repo 級**獨占** advisory lock（`db/locks.py`）。worker 的
    匯入持有同一把鎖的共享版，所以有 job 正在匯入這個 repo 時取不到 → 409
    `repository_busy`（稍後重試）。這是規格 5.11 的最小接線；「有 queued／running job 就擋」
    與 clone／fetch 取同一把獨占鎖屬切片 5。
    """
    started = time.monotonic()
    repository = get_repository(session, repository_id)  # 不存在 → 404
    source_type = repository.source_type
    lock_key = repo_lock_key(repository_key(repository))
    if source_type == "github" and settings.managed_repos_dir is None:
        # 無法驗證也無法清理 clone：不刪資料列，避免留下之後會擋住重新註冊的孤兒目錄
        raise GitHubRegistrationDisabledError(
            "GitHub support is disabled: set MANAGED_REPOS_DIR to the managed directory "
            "that holds the clone, then retry."
        )
    owner = repository.github_owner or ""
    name = repository.github_name or ""
    session.commit()  # 結束讀取交易
    with try_advisory_lock(engine_of(session), lock_key) as lock:
        if lock is None:
            raise RepositoryBusyError(
                "A background job is using this repository; retry when it finishes."
            )
        if source_type != "github":
            result = _remove_locked(session, repository_id, settings)
        else:
            with exclusive_repo_access(owner, name):
                result = _remove_locked(session, repository_id, settings)
    logger.info(
        "repository_removed",
        extra={
            "repository_id": result.repository_id,
            "source_type": result.source_type,
            "deleted_snapshots": result.deleted_snapshots,
            "deleted_files": result.deleted_files,
            "removed_clone": result.removed_clone,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return result


def _remove_locked(session: Session, repository_id: int, settings: Settings) -> RemovalResult:
    try:
        return _remove_in_transaction(session, repository_id, settings)
    except BaseException:
        session.rollback()  # 任何失敗都放掉列鎖；資料列與快照原封不動
        raise


def _remove_in_transaction(
    session: Session, repository_id: int, settings: Settings
) -> RemovalResult:
    # 鎖住資料列並重新讀取：等鎖期間可能已被另一個刪除請求移除（→ 404）
    row = session.execute(
        select(
            Repository.source_type,
            Repository.github_owner,
            Repository.github_name,
            Repository.canonical_local_path,
        )
        .where(Repository.id == repository_id)
        .with_for_update()
    ).first()
    if row is None:
        raise RepositoryNotFoundError("Repository not found.")

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.import_stale_seconds)
    importing = session.scalar(
        select(
            exists().where(
                Snapshot.repository_id == repository_id,
                Snapshot.status == "importing",
                Snapshot.started_at >= cutoff,
            )
        )
    )
    if importing:
        raise RepositoryBusyError(
            "A snapshot import is in progress for this repository; retry when it finishes."
        )

    removed_clone = False
    if row.source_type == "github":
        if settings.managed_repos_dir is None:
            raise GitHubRegistrationDisabledError("GitHub support is disabled.")
        removed_clone = managed_repos.remove_github_clone(
            settings.managed_repos_dir,
            owner=row.github_owner or "",
            name=row.github_name or "",
            recorded_path=row.canonical_local_path,
        )

    # SQL 層級刪除（不載入 ORM 物件，20,000 檔也只是三個語句）。snapshot_files 的外鍵本來就是
    # ON DELETE CASCADE，但 snapshots → repositories 沒有，所以依序明確刪除，不需要新的 migration
    deleted_files = _rowcount(
        session.execute(
            delete(SnapshotFile).where(
                SnapshotFile.snapshot_id.in_(
                    select(Snapshot.id).where(Snapshot.repository_id == repository_id)
                )
            )
        )
    )
    deleted_snapshots = _rowcount(
        session.execute(delete(Snapshot).where(Snapshot.repository_id == repository_id))
    )
    session.execute(delete(Repository).where(Repository.id == repository_id))
    session.commit()
    return RemovalResult(
        repository_id=repository_id,
        source_type="github" if row.source_type == "github" else "local",
        deleted_snapshots=deleted_snapshots,
        deleted_files=deleted_files,
        removed_clone=removed_clone,
    )


def _rowcount(result: Any) -> int:
    return int(cast(CursorResult[Any], result).rowcount)
