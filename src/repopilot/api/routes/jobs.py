from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from repopilot.api.job_responses import to_job_response
from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    DEFAULT_PAGE_LIMIT,
    JobIdPath,
    JobKind,
    JobListResponse,
    JobResponse,
    JobsStatusResponse,
    JobStatusFilter,
    LimitQuery,
    OffsetQuery,
    RepositoryIdQuery,
    ServiceStateResponse,
    SnapshotIdQuery,
)
from repopilot.db.session import get_session
from repopilot.services import job_queries, service_heartbeats
from repopilot.services import jobs as job_service

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

SessionDep = Annotated[Session, Depends(get_session)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "工作不存在（job_not_found）"}}


@router.get("", response_model=JobListResponse)
def list_jobs(
    session: SessionDep,
    status: JobStatusFilter | None = None,
    kind: JobKind | None = None,
    repository_id: RepositoryIdQuery | None = None,
    snapshot_id: SnapshotIdQuery | None = None,
    limit: LimitQuery = DEFAULT_PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> JobListResponse:
    """分頁列出背景工作（新的在前，`id DESC`）；`status=active` 等於 queued＋running。"""
    items, total = job_queries.list_jobs(
        session,
        status=status,
        kind=kind,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        limit=limit,
        offset=offset,
    )
    return JobListResponse(
        items=[to_job_response(job) for job in items], total=total, limit=limit, offset=offset
    )


# 靜態路徑 `/status` 必須在動態路徑 `/{job_id}` 之前宣告（兩者都是單一路徑區段）
@router.get("/status", response_model=JobsStatusResponse)
def get_jobs_status(session: SessionDep, settings: SettingsDep) -> JobsStatusResponse:
    """dispatcher／worker 是否在線（心跳是否過期）與 queued／running 數量；不連 Redis。"""
    result = service_heartbeats.get_jobs_status(session, settings)
    return JobsStatusResponse(
        dispatcher=ServiceStateResponse(
            status=result.dispatcher.status,
            last_seen_at=result.dispatcher.last_seen_at,
            instances_online=result.dispatcher.instances_online,
        ),
        worker=ServiceStateResponse(
            status=result.worker.status,
            last_seen_at=result.worker.last_seen_at,
            instances_online=result.worker.instances_online,
        ),
        queued=result.queued,
        running=result.running,
        heartbeat_timeout_seconds=result.heartbeat_timeout_seconds,
    )


@router.get("/{job_id}", response_model=JobResponse, responses=_NOT_FOUND)
def get_job(job_id: JobIdPath, session: SessionDep) -> JobResponse:
    """單一工作（階段、進度、錯誤碼、結果與 `diagnostics` 追查摘要）；不含令牌與 work_state。"""
    return to_job_response(job_queries.get_job(session, job_id))


@router.post(
    "/{job_id}/cancel",
    response_model=JobResponse,
    responses={
        202: {"model": JobResponse, "description": "running：已記錄取消請求，於檢查點生效"},
        404: {"description": "工作不存在（job_not_found）"},
        409: {"description": "工作已結束，不能取消（job_not_cancellable）"},
    },
)
def cancel_job(job_id: JobIdPath, response: Response, session: SessionDep) -> JobResponse:
    """取消：queued 立即 cancelled（200）；running 記錄旗標（202）；已 cancelled 冪等（200）。"""
    result = job_service.cancel_job(session, job_id)
    if result.outcome == "requested":
        response.status_code = status.HTTP_202_ACCEPTED
        response.headers["Location"] = f"/api/v1/jobs/{job_id}"
    return to_job_response(result.job)


@router.post(
    "/{job_id}/retry",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "工作不存在（job_not_found）"},
        409: {"description": "只有 failed 可重試（job_not_retryable）"},
    },
)
def retry_job(
    job_id: JobIdPath, response: Response, session: SessionDep, settings: SettingsDep
) -> JobResponse:
    """重試 failed 的工作：同一 job id、attempt 繼續累加，重新排入佇列。"""
    job = job_service.retry_job(session, job_id, settings)
    response.headers["Location"] = f"/api/v1/jobs/{job_id}"
    return to_job_response(job)
