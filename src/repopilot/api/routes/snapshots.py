from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from repopilot.api.index_responses import (
    to_indexed_file_item,
    to_symbol_detail,
    to_symbol_summary,
)
from repopilot.api.job_responses import to_job_response
from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    DEFAULT_PAGE_LIMIT,
    FileIdPath,
    FileStatus,
    IndexCountsResponse,
    IndexedFileIdQuery,
    IndexedFileListResponse,
    IndexLanguage,
    JobSubmitResponse,
    LimitQuery,
    OffsetQuery,
    ParentIdQuery,
    SnapshotDetailResponse,
    SnapshotFileDetail,
    SnapshotFileListResponse,
    SnapshotFileSummary,
    SnapshotIdPath,
    SnapshotIndexResponse,
    SnapshotRemovedResponse,
    SymbolDetailResponse,
    SymbolIdPath,
    SymbolKindFilter,
    SymbolListResponse,
)
from repopilot.api.snapshot_responses import to_snapshot_item
from repopilot.db.session import get_session
from repopilot.request_context import request_id_var
from repopilot.services import index_jobs as index_job_service
from repopilot.services import index_queries as index_query_service
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


@router.post(
    "/{snapshot_id}/index-jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {
            "model": JobSubmitResponse,
            "description": "目前設定的索引已 completed：不建立 job，reused=true",
        },
        404: {"description": "快照不存在（snapshot_not_found）"},
        409: {"description": "快照尚未 completed（snapshot_not_completed）"},
    },
)
def create_index_job(
    snapshot_id: SnapshotIdPath,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> JobSubmitResponse:
    """為已完成的快照排入結構索引建構（202＋`Location`）；索引只讀資料庫，由 worker 在背景執行。

    同一份工作（同快照、同索引設定）已有進行中的 job 時回既有 job；目前設定的索引已完成時回
    200 與 `reused=true`；最近的 job 失敗或取消則重新排入同一個 job。
    """
    submission = index_job_service.submit_index_job(
        session, snapshot_id, settings, request_id=request_id_var.get()
    )
    if submission.reused:
        response.status_code = status.HTTP_200_OK
    elif submission.job is not None:
        response.headers["Location"] = f"/api/v1/jobs/{submission.job.id}"
    return JobSubmitResponse(
        job=to_job_response(submission.job) if submission.job is not None else None,
        snapshot_id=submission.snapshot_id,
        repository_id=submission.repository_id,
        reused=submission.reused,
    )


_INDEX_NOT_READY: dict[int | str, dict[str, Any]] = {
    409: {
        "description": "快照尚未 completed（snapshot_not_completed），或沒有 completed 的索引"
        "（snapshot_index_not_ready，details.index_status 為 none／building／failed）"
    }
}


@router.get(
    "/{snapshot_id}/index",
    response_model=SnapshotIndexResponse,
    responses={409: {"description": "快照尚未 completed（snapshot_not_completed）"}},
)
def get_snapshot_index(
    snapshot_id: SnapshotIdPath, session: SessionDep, settings: SettingsDep
) -> SnapshotIndexResponse:
    """快照在目前設定下的結構索引狀態；沒有索引時 200＋`status="none"`（不是錯誤）。

    只有 `completed` 才有 `counts`。`building`／`failed` 的索引不會被符號查詢採用。
    """
    index = index_query_service.get_current_index(session, snapshot_id, settings)
    if index is None:
        return SnapshotIndexResponse(
            snapshot_id=snapshot_id,
            status="none",
            index_id=None,
            started_at=None,
            completed_at=None,
            analyzers={},
            counts=None,
            error_summary=None,
        )
    counts = (
        IndexCountsResponse(**asdict(index_query_service.count_index(session, index)))
        if index.status == "completed"
        else None
    )
    return SnapshotIndexResponse(
        snapshot_id=snapshot_id,
        status=index.status,
        index_id=index.id,
        started_at=index.started_at,
        completed_at=index.completed_at,
        analyzers=index.analyzers,
        counts=counts,
        error_summary=index.error_summary,
    )


@router.get(
    "/{snapshot_id}/index/files",
    response_model=IndexedFileListResponse,
    responses=_INDEX_NOT_READY,
)
def list_index_files(
    snapshot_id: SnapshotIdPath,
    session: SessionDep,
    settings: SettingsDep,
    has_problems: Annotated[bool, Query(description="只列「部分解析」或解析失敗的檔案")] = False,
    language: IndexLanguage | None = None,
    limit: LimitQuery = DEFAULT_PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> IndexedFileListResponse:
    """分頁列出被索引的檔案與解析狀態（依 path 排序）；不含任何程式碼片段或錯誤訊息。"""
    rows, total = index_query_service.list_indexed_files(
        session,
        snapshot_id,
        settings,
        has_problems=has_problems,
        language=language,
        limit=limit,
        offset=offset,
    )
    return IndexedFileListResponse(
        items=[to_indexed_file_item(file, path, file_id) for file, path, file_id in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{snapshot_id}/symbols", response_model=SymbolListResponse, responses=_INDEX_NOT_READY)
def list_symbols(
    snapshot_id: SnapshotIdPath,
    session: SessionDep,
    settings: SettingsDep,
    kind: SymbolKindFilter | None = None,
    parent_id: ParentIdQuery | None = None,
    top_level: Annotated[bool, Query(description="只列沒有父符號者（各檔的模組）")] = False,
    file_id: IndexedFileIdQuery | None = None,
    path_prefix: Annotated[
        str | None, Query(max_length=1024, description="路徑前綴（區分大小寫，不是萬用字元）")
    ] = None,
    limit: LimitQuery = DEFAULT_PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> SymbolListResponse:
    """分頁列出符號（依 path、起始行排序）。`parent_id` 或 `top_level` 用來逐層載入符號樹。"""
    rows, total = index_query_service.list_symbols(
        session,
        snapshot_id,
        settings,
        kind=kind,
        parent_id=parent_id,
        top_level=top_level,
        file_id=file_id,
        path_prefix=path_prefix,
        limit=limit,
        offset=offset,
    )
    return SymbolListResponse(
        items=[to_symbol_summary(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/{snapshot_id}/symbols/{symbol_id}",
    response_model=SymbolDetailResponse,
    responses={
        **_INDEX_NOT_READY,
        404: {"description": "符號不存在，或不屬於該快照目前的索引（symbol_not_found）"},
    },
)
def get_symbol(
    snapshot_id: SnapshotIdPath,
    symbol_id: SymbolIdPath,
    session: SessionDep,
    settings: SettingsDep,
) -> SymbolDetailResponse:
    """單一符號：精確位置、修飾詞、同檔 import 與關係（未解析者保留原始名稱）。"""
    row, imports, relations = index_query_service.get_symbol_detail(
        session, snapshot_id, settings, symbol_id
    )
    return to_symbol_detail(row, imports, relations)


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
