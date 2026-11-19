"""移除單一快照（切片 3b，D41）：只刪資料庫紀錄，**不碰 Git repo**。

互斥與保護（先擋再刪；擋下時**什麼都不刪**）：

1. 以 `try` 取得該快照的**快照級 advisory lock**（`db/locks.py`，與同步 `POST /imports`、worker
   的匯入 job 是同一把）。活著的匯入者一定持有它，所以取不到 → 409 `snapshot_busy`；取得後，
   匯入者要開始就會被擋（同步 409、worker 延後），直到我們提交。
2. 在鎖內開交易，`SELECT ... FOR UPDATE` 鎖住快照列並重新讀取（等鎖期間可能已被別人刪除 → 404）。
   `FOR UPDATE` 與「設定參考版本」時外鍵檢查取的 `FOR KEY SHARE` 互斥，所以不會一邊刪、
   一邊有人新增參考（新增者先到，我們會等它提交並看到參考；我們先到，新增者會等，
   之後因外鍵失敗而 404）。
3. 未逾時（`IMPORT_STALE_SECONDS`）的 `importing` → 409 `snapshot_busy`；逾時者視為可刪除
   （與 D24、D33 一致；舊匯入若還在跑，完成時的條件式 UPDATE 找不到列而回 409，不會有半套資料）。
4. 進行中（`queued`／`running`）的 job：`repository_id` 相同，且 `snapshot_id` 指向它，**或**
   `dedupe_key` 等於這個快照的匯入鍵（匯入 job 在完成前 `snapshot_id` 是 NULL，D40，只能靠鍵）
   → 409 `snapshot_busy`。
5. 被參考：沒有 `force` → 409 `snapshot_referenced`；`force=true` → 同交易刪除參考。
6. SQL 層級刪除（不載入 ORM 物件）：`snapshot_files`（先刪才有筆數）→ 參考 → `snapshots`。
   `jobs.snapshot_id` 為 `ON DELETE SET NULL`（D39）：歷史 job 紀錄保留、只是不再指向快照。
   刪除後唯一約束 `(repository_id, commit_sha, import_profile_hash)` 釋放，可立刻重新匯入。

已知取捨：一個「不持鎖」的舊匯入者（逾時被視為可刪除、仍在跑）在完成時會得到 409；
快照 `importing` 但持鎖者已崩潰（鎖已自動釋放）時，要等到逾時才可刪除（沿用 D24 的規則，
不另外猜測 `importing` 是否已死）。

.NET 對照：像在 `TransactionScope` 內先取得 `sp_getapplock`，再 `SELECT ... WITH (UPDLOCK)`
檢查狀態後才 `ExecuteDelete`。
"""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, exists, select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import engine_of, snapshot_lock_key, try_advisory_lock
from repopilot.db.models import Job, RepositoryReference, Snapshot, SnapshotFile
from repopilot.errors import SnapshotBusyError, SnapshotNotFoundError, SnapshotReferencedError
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import job_rules, snapshot_indexing
from repopilot.services.import_jobs import import_dedupe_key
from repopilot.services.snapshot_indexing import PURGE_THRESHOLD_SYMBOLS

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.snapshot_removal")


@dataclass(frozen=True)
class SnapshotRemovalResult:
    snapshot_id: int
    repository_id: int
    deleted_files: int
    was_referenced: bool


def remove_snapshot(
    session: Session, snapshot_id: int, settings: Settings, *, force: bool = False
) -> SnapshotRemovalResult:
    started = time.monotonic()
    identity = session.execute(
        select(Snapshot.repository_id, Snapshot.commit_sha, Snapshot.import_profile_hash).where(
            Snapshot.id == snapshot_id
        )
    ).first()
    session.rollback()  # 結束讀取交易；取鎖用的是另一條專用連線
    if identity is None:
        raise SnapshotNotFoundError("Snapshot not found.")
    repository_id, commit_sha, profile_hash = identity

    # 鎖的鍵由快照**自己**的設定雜湊算出（不是目前的設定），才會與當初匯入它的人用同一把鎖
    key = snapshot_lock_key(repository_id, commit_sha, profile_hash)
    with try_advisory_lock(engine_of(session), key) as lock:
        if lock is None:
            raise SnapshotBusyError(
                "The snapshot is being imported or used by another operation; retry later."
            )
        try:
            # 大型結構索引先分批清掉（每批各自一個短交易，不持有快照列鎖）；小型索引沒有動作。
            # 先檢查再清，所以「被擋下」時什麼都不刪
            _lock_and_check(session, snapshot_id, settings, force=force)
            if snapshot_indexing.count_symbols(session, [snapshot_id]) >= PURGE_THRESHOLD_SYMBOLS:
                snapshot_indexing.purge_index_data(session, [snapshot_id])
            else:
                session.rollback()
            result = _remove_in_transaction(session, snapshot_id, settings, force=force)
        except BaseException:
            session.rollback()  # 任何失敗都放掉列鎖；快照、檔案與參考原封不動
            raise
    logger.info(
        "snapshot_removed",
        extra={
            "repository_id": result.repository_id,
            "snapshot_id": result.snapshot_id,
            "deleted_files": result.deleted_files,
            "was_referenced": result.was_referenced,
            "force": force,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return result


def _lock_and_check(
    session: Session, snapshot_id: int, settings: Settings, *, force: bool
) -> tuple[int, bool]:
    """鎖住快照列並做「可以移除嗎」的檢查；回傳 (repository_id, 是否為參考版本)，不 commit。"""
    row = session.execute(
        select(
            Snapshot.repository_id,
            Snapshot.commit_sha,
            Snapshot.import_profile_hash,
            Snapshot.status,
            Snapshot.started_at,
        )
        .where(Snapshot.id == snapshot_id)
        .with_for_update()
    ).first()
    if row is None:  # 等鎖期間已被另一個刪除請求移除
        raise SnapshotNotFoundError("Snapshot not found.")
    repository_id, commit_sha, profile_hash, status, started_at = row

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.import_stale_seconds)
    if status == "importing" and started_at >= cutoff:
        raise SnapshotBusyError("The snapshot is being imported; retry when it finishes.")

    dedupe_key = import_dedupe_key(repository_id, commit_sha, profile_hash)
    has_active_job = session.scalar(
        select(
            exists().where(
                Job.repository_id == repository_id,
                Job.status.in_(job_rules.ACTIVE_STATUSES),
                (Job.snapshot_id == snapshot_id) | (Job.dedupe_key == dedupe_key),
            )
        )
    )
    if has_active_job:
        raise SnapshotBusyError("A background job is using this snapshot; retry when it finishes.")

    referenced = bool(
        session.scalar(
            select(exists().where(RepositoryReference.snapshot_id == snapshot_id)),
        )
    )
    if referenced and not force:
        raise SnapshotReferencedError(
            "The snapshot is the reference version of its repository; "
            "retry with force=true to remove it and clear the reference."
        )
    return repository_id, referenced


def _remove_in_transaction(
    session: Session, snapshot_id: int, settings: Settings, *, force: bool
) -> SnapshotRemovalResult:
    repository_id, referenced = _lock_and_check(session, snapshot_id, settings, force=force)

    # SQL 層級刪除：不載入任何 ORM 物件，與檔案數無關。大型結構索引已由 purge_index_data 分批清掉
    # （見 remove_snapshot）；snapshot_files、repository_references 的外鍵本來就是
    # ON DELETE CASCADE，但明確刪除才有筆數
    deleted_files = _rowcount(
        session.execute(delete(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot_id))
    )
    session.execute(
        delete(RepositoryReference).where(RepositoryReference.snapshot_id == snapshot_id)
    )
    session.execute(delete(Snapshot).where(Snapshot.id == snapshot_id))
    session.commit()
    return SnapshotRemovalResult(
        snapshot_id=snapshot_id,
        repository_id=repository_id,
        deleted_files=deleted_files,
        was_referenced=referenced,
    )


def _rowcount(result: Any) -> int:
    return int(cast(CursorResult[Any], result).rowcount)
