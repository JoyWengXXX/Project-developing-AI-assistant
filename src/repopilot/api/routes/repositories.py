from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from repopilot.api.job_responses import to_job_response
from repopilot.api.schemas import (
    DEFAULT_COMMIT_LIMIT,
    DEFAULT_PAGE_LIMIT,
    CommitLimitQuery,
    CommitListResponse,
    CommitOffsetQuery,
    CommitResponse,
    ImportCreate,
    JobSubmitResponse,
    LimitQuery,
    OffsetQuery,
    RefsResponse,
    RepositoryCreate,
    RepositoryIdPath,
    RepositoryListResponse,
    RepositoryOverviewResponse,
    RepositoryRemovedResponse,
    RepositoryResponse,
    SnapshotListResponse,
    SnapshotResponse,
    SnapshotStatus,
)
from repopilot.api.snapshot_responses import to_snapshot_item
from repopilot.config import Settings
from repopilot.db.session import get_session
from repopilot.request_context import request_id_var
from repopilot.services import import_jobs as import_job_service
from repopilot.services import repositories as repository_service
from repopilot.services import repository_history as history_service
from repopilot.services import repository_overview as overview_service
from repopilot.services import repository_removal as removal_service
from repopilot.services import snapshot_queries
from repopilot.services import snapshots as snapshot_service
from repopilot.services.import_profile import ImportProfile

router = APIRouter(prefix="/api/v1/repositories", tags=["repositories"])

SessionDep = Annotated[Session, Depends(get_session)]


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


@router.post(
    "",
    response_model=RepositoryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RepositoryResponse, "description": "已註冊過的 repo"}},
)
def register_repository(
    body: RepositoryCreate, response: Response, session: SessionDep, settings: SettingsDep
) -> RepositoryResponse:
    """註冊本機 repo；相同路徑重複註冊回 200 與既有資料。"""
    repository, created = repository_service.register_repository(
        session,
        raw_path=body.path,
        name=body.name,
        roots=settings.repository_roots,
        git_timeout=settings.git_timeout_seconds,
        managed_dir=settings.managed_repos_dir,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return RepositoryResponse.model_validate(repository)


@router.get("", response_model=RepositoryListResponse)
def list_repositories(session: SessionDep) -> RepositoryListResponse:
    items = repository_service.list_repositories(session)
    return RepositoryListResponse(items=[RepositoryResponse.model_validate(r) for r in items])


@router.get("/{repository_id}", response_model=RepositoryResponse)
def get_repository(repository_id: RepositoryIdPath, session: SessionDep) -> RepositoryResponse:
    return RepositoryResponse.model_validate(
        repository_service.get_repository(session, repository_id)
    )


@router.delete("/{repository_id}", response_model=RepositoryRemovedResponse)
def remove_repository(
    repository_id: RepositoryIdPath, session: SessionDep, settings: SettingsDep
) -> RepositoryRemovedResponse:
    """移除 repo 與其所有快照、檔案。

    local 來源只刪資料庫紀錄，不碰磁碟上的 repo 目錄；github 來源另外刪除受管目錄內的 clone
    （不影響 GitHub 遠端）。有未逾時的匯入或 clone／fetch 進行中回 409。
    """
    result = removal_service.remove_repository(session, repository_id, settings)
    return RepositoryRemovedResponse(
        repository_id=result.repository_id,
        source_type=result.source_type,
        deleted_snapshots=result.deleted_snapshots,
        deleted_files=result.deleted_files,
        removed_clone=result.removed_clone,
    )


@router.get("/{repository_id}/refs", response_model=RefsResponse)
def list_repository_refs(
    repository_id: RepositoryIdPath, session: SessionDep, settings: SettingsDep
) -> RefsResponse:
    """列出分支與標籤（唯讀 Git）。本機來源列 `refs/heads/*`，GitHub 來源列 `origin/*`。"""
    return RefsResponse.model_validate(history_service.get_refs(session, repository_id, settings))


@router.get("/{repository_id}/commits", response_model=CommitListResponse)
def list_repository_commits(
    repository_id: RepositoryIdPath,
    session: SessionDep,
    settings: SettingsDep,
    ref: str | None = None,
    limit: CommitLimitQuery = DEFAULT_COMMIT_LIMIT,
    offset: CommitOffsetQuery = 0,
) -> CommitListResponse:
    """列出 commit 歷史（`--topo-order`）。

    `ref` 須為 `/refs` 回傳的完整 ref 名稱；省略＝所有分支與標籤。
    """
    result = history_service.get_commits(
        session, repository_id, settings, ref=ref, limit=limit, offset=offset
    )
    items = [CommitResponse.model_validate(item) for item in result.items]
    return CommitListResponse(items=items, total_returned=len(items), has_more=result.has_more)


@router.get("/{repository_id}/overview", response_model=RepositoryOverviewResponse)
def get_repository_overview(
    repository_id: RepositoryIdPath, session: SessionDep, settings: SettingsDep
) -> RepositoryOverviewResponse:
    """預設分支最新 commit 的概況（唯讀，不匯入）。

    該 commit 已有 completed 快照（以目前匯入設定）才有統計、語言、README 與專案資訊；
    否則 `analyzed=false` 且只有 `head`，由呼叫端決定是否 `POST /imports`。
    """
    return RepositoryOverviewResponse.model_validate(
        overview_service.get_overview(session, repository_id, settings)
    )


@router.get("/{repository_id}/snapshots", response_model=SnapshotListResponse)
def list_repository_snapshots(
    repository_id: RepositoryIdPath,
    session: SessionDep,
    settings: SettingsDep,
    status: SnapshotStatus | None = None,
    limit: LimitQuery = DEFAULT_PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> SnapshotListResponse:
    """列出 repo 的快照（新的在前，不含檔案內容）；包含 importing／failed，可用 status 篩選。

    每筆另有 `is_referenced`（是否為參考版本）與 `is_stale_profile`（匯入設定與目前不同）。
    """
    items, total = snapshot_queries.list_snapshots(
        session, repository_id, status=status, limit=limit, offset=offset
    )
    referenced = snapshot_queries.referenced_snapshot_ids(session, (s.id for s in items))
    current_hash = ImportProfile.from_settings(settings).hash()
    return SnapshotListResponse(
        items=[
            to_snapshot_item(s, referenced=s.id in referenced, current_profile_hash=current_hash)
            for s in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{repository_id}/imports",
    response_model=SnapshotResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    responses={200: {"model": SnapshotResponse, "description": "相同快照已匯入完成"}},
)
def import_snapshot(
    repository_id: RepositoryIdPath,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    body: ImportCreate | None = None,
) -> SnapshotResponse:
    """**已棄用（deprecated）**：同步匯入指定 revision（預設 HEAD）的固定版本快照。

    已完成者回 200 與既有快照。請求會一直占用到匯入結束（大型 repo 可能數十秒到數分鐘），
    長時間匯入請改用 `POST /api/v1/repositories/{repository_id}/import-jobs`
    （立即回 202 與 job，由 worker 背景執行）。本端點保留原契約供腳本與既有用戶端使用，
    並與 worker 共用同一把快照鎖（同一快照不會被兩邊同時匯入；另一邊正在匯入時回 409
    `snapshot_import_in_progress`）。
    """
    revision = body.revision if body is not None else "HEAD"
    snapshot, created = snapshot_service.import_snapshot(session, repository_id, revision, settings)
    if not created:
        response.status_code = status.HTTP_200_OK
    return SnapshotResponse.model_validate(snapshot)


@router.post(
    "/{repository_id}/import-jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {
            "model": JobSubmitResponse,
            "description": "相同快照已 completed：不建立 job，reused=true"
            "（job 為最近一次成功的 job，快照來自同步匯入時為 null）",
        },
        404: {"description": "repo 不存在（repository_not_found）"},
        409: {"description": "同步端點正在匯入同一快照（snapshot_import_in_progress）"},
        422: {
            "description": "revision 無效（invalid_revision）"
            "或路徑驗證失敗（invalid_repository_path）"
        },
        503: {"description": "repo 路徑無法映射到 worker 容器（worker_path_not_mapped）"},
    },
)
def create_import_job(
    repository_id: RepositoryIdPath,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    body: ImportCreate | None = None,
) -> JobSubmitResponse:
    """排入固定版本匯入並立即回應（202＋`Location`）；實際匯入由 worker 在背景執行。

    API 在排入前於主機端解析 revision 為完整 commit SHA（Git 呼叫沿用 `GIT_TIMEOUT_SECONDS`），
    worker 只匯入該 SHA，之後分支前進或工作目錄被修改都不影響結果。
    同一份工作（同 repo、同 commit、同匯入設定）已有進行中的 job 時回既有 job；
    快照已完成時回 200 與 `reused=true`。以 `GET /api/v1/jobs/{id}` 輪詢進度。
    """
    revision = body.revision if body is not None else "HEAD"
    submission = import_job_service.submit_import_job(
        session, repository_id, revision, settings, request_id=request_id_var.get()
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
