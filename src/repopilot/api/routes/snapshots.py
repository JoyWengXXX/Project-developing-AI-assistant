from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    DEFAULT_PAGE_LIMIT,
    FileIdPath,
    FileStatus,
    LimitQuery,
    OffsetQuery,
    SnapshotDetailResponse,
    SnapshotFileDetail,
    SnapshotFileListResponse,
    SnapshotFileSummary,
    SnapshotIdPath,
    SnapshotRemovedResponse,
)
from repopilot.api.snapshot_responses import to_snapshot_item
from repopilot.db.session import get_session
from repopilot.services import snapshot_queries
from repopilot.services import snapshot_removal as removal_service
from repopilot.services.import_profile import ImportProfile

router = APIRouter(prefix="/api/v1/snapshots", tags=["snapshots"])

SessionDep = Annotated[Session, Depends(get_session)]

_NOT_COMPLETED: dict[int | str, dict[str, Any]] = {
    409: {"description": "快照尚未 completed（snapshot_not_completed）"}
}


@router.get("/{snapshot_id}", response_model=SnapshotDetailResponse)
def get_snapshot(
    snapshot_id: SnapshotIdPath, session: SessionDep, settings: SettingsDep
) -> SnapshotDetailResponse:
    """快照詳情與統計；任何狀態都可查詢，但只有 completed 才有檔案統計。

    另有 `is_referenced`（是否為參考版本）與 `is_stale_profile`（匯入設定與目前不同）。
    """
    snapshot = snapshot_queries.get_snapshot(session, snapshot_id)
    by_status, by_reason = snapshot_queries.count_files_by_status(session, snapshot)
    referenced = snapshot.id in snapshot_queries.referenced_snapshot_ids(session, [snapshot.id])
    item = to_snapshot_item(
        snapshot,
        referenced=referenced,
        current_profile_hash=ImportProfile.from_settings(settings).hash(),
    )
    return SnapshotDetailResponse(
        **item.model_dump(), file_status_counts=by_status, skip_reason_counts=by_reason
    )


@router.delete(
    "/{snapshot_id}",
    response_model=SnapshotRemovedResponse,
    responses={
        404: {"description": "快照不存在或已被移除（snapshot_not_found）"},
        409: {
            "description": "快照正在被使用（snapshot_busy：匯入中、進行中的 job 或快照鎖被持有），"
            "或它是參考版本且未帶 force（snapshot_referenced）"
        },
    },
)
def remove_snapshot(
    snapshot_id: SnapshotIdPath,
    session: SessionDep,
    settings: SettingsDep,
    force: Annotated[
        bool, Query(description="快照是參考版本時，帶 true 才會一併清除參考並移除")
    ] = False,
) -> SnapshotRemovedResponse:
    """移除快照與其檔案（只刪資料庫紀錄，不動 Git repo）。

    被匯入中或進行中的 job 使用時回 409 `snapshot_busy`，什麼都不刪；是參考版本且沒有
    `force=true` 回 409 `snapshot_referenced`。移除後同一 commit 可重新匯入；歷史 job 紀錄保留。
    """
    result = removal_service.remove_snapshot(session, snapshot_id, settings, force=force)
    return SnapshotRemovedResponse(
        snapshot_id=result.snapshot_id,
        repository_id=result.repository_id,
        deleted_files=result.deleted_files,
        was_referenced=result.was_referenced,
    )


@router.get(
    "/{snapshot_id}/files", response_model=SnapshotFileListResponse, responses=_NOT_COMPLETED
)
def list_snapshot_files(
    snapshot_id: SnapshotIdPath,
    session: SessionDep,
    status: FileStatus | None = None,
    path_prefix: Annotated[
        str | None, Query(max_length=1024, description="路徑前綴（區分大小寫，不是萬用字元）")
    ] = None,
    limit: LimitQuery = DEFAULT_PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> SnapshotFileListResponse:
    """分頁列出檔案（依 path 排序，不含 content）；快照非 completed 回 409。"""
    items, total = snapshot_queries.list_files(
        session,
        snapshot_id,
        status=status,
        path_prefix=path_prefix,
        limit=limit,
        offset=offset,
    )
    return SnapshotFileListResponse(
        items=[SnapshotFileSummary.model_validate(f) for f in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{snapshot_id}/files/{file_id}", response_model=SnapshotFileDetail, responses=_NOT_COMPLETED
)
def get_snapshot_file(
    snapshot_id: SnapshotIdPath, file_id: FileIdPath, session: SessionDep
) -> SnapshotFileDetail:
    """單一檔案（含 content）；file 不屬於該快照回 404，快照非 completed 回 409。"""
    return SnapshotFileDetail.model_validate(
        snapshot_queries.get_file(session, snapshot_id, file_id)
    )
