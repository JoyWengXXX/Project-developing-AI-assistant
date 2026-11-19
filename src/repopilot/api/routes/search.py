from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    SEARCH_DEFAULT_LIMIT,
    FileMatchResponse,
    FileSearchResponse,
    IndexLanguage,
    SearchLimitQuery,
    SearchOffsetQuery,
    SearchPathPrefixQuery,
    SearchTextQuery,
    SnapshotIdPath,
    SymbolKindFilter,
    SymbolMatchResponse,
    SymbolSearchResponse,
    TextMatchResponse,
    TextSearchResponse,
)
from repopilot.db.session import get_session
from repopilot.services import search as search_service

router = APIRouter(prefix="/api/v1/snapshots", tags=["search"])

SessionDep = Annotated[Session, Depends(get_session)]

_COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"description": "快照不存在（snapshot_not_found）"},
    409: {"description": "快照尚未 completed（snapshot_not_completed）"},
    504: {"description": "搜尋超過時間上限（search_timeout）；縮小 path_prefix 或換更長的 q"},
}


@router.get(
    "/{snapshot_id}/search/files", response_model=FileSearchResponse, responses=_COMMON_ERRORS
)
def search_files(
    snapshot_id: SnapshotIdPath,
    session: SessionDep,
    settings: SettingsDep,
    q: SearchTextQuery,
    path_prefix: SearchPathPrefixQuery = None,
    limit: SearchLimitQuery = SEARCH_DEFAULT_LIMIT,
    offset: SearchOffsetQuery = 0,
) -> FileSearchResponse:
    """路徑含 `q` 的已匯入檔案（字面子字串、區分大小寫；依 path 排序）。不依賴結構索引。"""
    page = search_service.search_files(
        session, snapshot_id, settings, q=q, path_prefix=path_prefix, limit=limit, offset=offset
    )
    return FileSearchResponse(
        snapshot_id=page.snapshot.id,
        repository_id=page.snapshot.repository_id,
        commit_sha=page.snapshot.commit_sha,
        items=[FileMatchResponse(**asdict(item)) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
        has_more=page.has_more,
    )


@router.get(
    "/{snapshot_id}/search/symbols",
    response_model=SymbolSearchResponse,
    responses={
        **_COMMON_ERRORS,
        409: {
            "description": "快照尚未 completed（snapshot_not_completed），或沒有 completed 的索引"
            "（snapshot_index_not_ready，details.index_status 為 none／building／failed）"
        },
    },
)
def search_symbols(
    snapshot_id: SnapshotIdPath,
    session: SessionDep,
    settings: SettingsDep,
    q: SearchTextQuery,
    kind: SymbolKindFilter | None = None,
    language: IndexLanguage | None = None,
    limit: SearchLimitQuery = SEARCH_DEFAULT_LIMIT,
    offset: SearchOffsetQuery = 0,
) -> SymbolSearchResponse:
    """名稱或完整名稱含 `q` 的符號（依 path、起始行排序）。只採用 completed 的索引。"""
    page = search_service.search_symbols(
        session,
        snapshot_id,
        settings,
        q=q,
        kind=kind,
        language=language,
        limit=limit,
        offset=offset,
    )
    return SymbolSearchResponse(
        snapshot_id=page.snapshot.id,
        repository_id=page.snapshot.repository_id,
        commit_sha=page.snapshot.commit_sha,
        items=[SymbolMatchResponse(**asdict(item)) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
        has_more=page.has_more,
    )


@router.get(
    "/{snapshot_id}/search/text", response_model=TextSearchResponse, responses=_COMMON_ERRORS
)
def search_text(
    snapshot_id: SnapshotIdPath,
    session: SessionDep,
    settings: SettingsDep,
    q: SearchTextQuery,
    path_prefix: SearchPathPrefixQuery = None,
    limit: SearchLimitQuery = SEARCH_DEFAULT_LIMIT,
    offset: SearchOffsetQuery = 0,
) -> TextSearchResponse:
    """內容含 `q` 的行（一個命中行一筆；依 path、行號）。`total` 為 null，改用 `has_more`。

    片段（最多 300 字元）是 repo 內容，須當純文字顯示。
    """
    page = search_service.search_text(
        session, snapshot_id, settings, q=q, path_prefix=path_prefix, limit=limit, offset=offset
    )
    return TextSearchResponse(
        snapshot_id=page.snapshot.id,
        repository_id=page.snapshot.repository_id,
        commit_sha=page.snapshot.commit_sha,
        items=[TextMatchResponse(**asdict(item)) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
        has_more=page.has_more,
    )
