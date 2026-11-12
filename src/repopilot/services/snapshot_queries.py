"""快照與檔案的唯讀查詢（切片 5）。

所有檔案查詢都先經過「快照為 completed」檢查，且以 `snapshot_id` 限定檔案範圍（歸屬檢查）。
匯入流程在 `services/snapshots.py`，兩者分開，避免查詢端依賴匯入的內部細節。
"""

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from repopilot.db.models import RepositoryReference, Snapshot, SnapshotFile
from repopilot.errors import (
    SnapshotFileNotFoundError,
    SnapshotNotCompletedError,
    SnapshotNotFoundError,
)
from repopilot.services.repositories import get_repository

SNAPSHOT_COMPLETED = "completed"


def list_snapshots(
    session: Session,
    repository_id: int,
    *,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Snapshot], int]:
    """列出 repo 的快照（新的在前）；包含非 completed 者，由 `status` 欄位區分。

    回傳 (本頁項目, 符合條件的總數)。固定以 `id DESC` 排序，分頁才穩定。
    """
    get_repository(session, repository_id)  # 不存在則 404

    conditions = [Snapshot.repository_id == repository_id]
    if status is not None:
        conditions.append(Snapshot.status == status)

    total = session.scalar(select(func.count()).select_from(Snapshot).where(*conditions)) or 0
    items = session.scalars(
        select(Snapshot).where(*conditions).order_by(Snapshot.id.desc()).limit(limit).offset(offset)
    ).all()
    return list(items), total


def get_snapshot(session: Session, snapshot_id: int) -> Snapshot:
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise SnapshotNotFoundError("Snapshot not found.")
    return snapshot


def referenced_snapshot_ids(session: Session, snapshot_ids: Iterable[int]) -> set[int]:
    """`snapshot_ids` 之中，目前是某個 repo 參考版本者（單一查詢，供列表一次判斷整頁）。"""
    ids = list(snapshot_ids)
    if not ids:
        return set()
    return set(
        session.scalars(
            select(RepositoryReference.snapshot_id).where(RepositoryReference.snapshot_id.in_(ids))
        )
    )


def get_completed_snapshot(session: Session, snapshot_id: int) -> Snapshot:
    """取得快照，且必須為 completed；否則拋出領域例外（不存在 404、未完成 409）。

    `completed` 是終態（匯入只會接手 failed 或逾時的 importing），因此檢查後再查檔案不會
    讀到「從 completed 退回未完成」的資料。
    """
    snapshot = get_snapshot(session, snapshot_id)
    if snapshot.status != SNAPSHOT_COMPLETED:
        raise SnapshotNotCompletedError("Snapshot import is not completed.")
    return snapshot


def count_files_by_status(
    session: Session, snapshot: Snapshot
) -> tuple[dict[str, int], dict[str, int]]:
    """回傳 (依 status 計數, 依 skip_reason 計數)；以單一 GROUP BY 查詢完成。

    只有 completed 快照才統計；其他狀態回傳空字典，避免把殘留列當成有效資料。
    """
    if snapshot.status != SNAPSHOT_COMPLETED:
        return {}, {}

    rows = session.execute(
        select(SnapshotFile.status, SnapshotFile.skip_reason, func.count())
        .where(SnapshotFile.snapshot_id == snapshot.id)
        .group_by(SnapshotFile.status, SnapshotFile.skip_reason)
    ).all()
    by_status: dict[str, int] = {"imported": 0, "skipped": 0}
    by_reason: dict[str, int] = {}
    for file_status, skip_reason, count in rows:
        by_status[file_status] = by_status.get(file_status, 0) + count
        if skip_reason is not None:
            by_reason[skip_reason] = by_reason.get(skip_reason, 0) + count
    return by_status, by_reason


def list_files(
    session: Session,
    snapshot_id: int,
    *,
    status: str | None,
    path_prefix: str | None,
    limit: int,
    offset: int,
) -> tuple[list[SnapshotFile], int]:
    """分頁列出已完成快照的檔案；不載入 `content`。

    固定依 `path` 的位元組順序（COLLATE "C"）排序：`path` 在快照內唯一，分頁因此穩定，
    且排序不受資料庫語系影響。
    """
    get_completed_snapshot(session, snapshot_id)

    conditions = [SnapshotFile.snapshot_id == snapshot_id]
    if status is not None:
        conditions.append(SnapshotFile.status == status)
    if path_prefix:
        # autoescape：使用者輸入中的 % 與 _ 視為字面字元，而非 LIKE 萬用字元
        conditions.append(SnapshotFile.path.startswith(path_prefix, autoescape=True))

    total = session.scalar(select(func.count()).select_from(SnapshotFile).where(*conditions)) or 0
    items = session.scalars(
        select(SnapshotFile)
        .where(*conditions)
        .options(defer(SnapshotFile.content))
        .order_by(SnapshotFile.path.collate("C"))
        .limit(limit)
        .offset(offset)
    ).all()
    return list(items), total


def get_file(session: Session, snapshot_id: int, file_id: int) -> SnapshotFile:
    """取得單一檔案（含 content）；file 必須屬於該快照，否則 404。"""
    get_completed_snapshot(session, snapshot_id)

    file = session.scalars(
        select(SnapshotFile).where(
            SnapshotFile.id == file_id, SnapshotFile.snapshot_id == snapshot_id
        )
    ).first()
    if file is None:
        raise SnapshotFileNotFoundError("File not found in this snapshot.")
    return file
