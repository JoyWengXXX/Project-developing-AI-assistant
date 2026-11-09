from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from repopilot.api.schemas import (
    ImportCreate,
    RepositoryCreate,
    RepositoryListResponse,
    RepositoryResponse,
    SnapshotResponse,
)
from repopilot.config import Settings
from repopilot.db.session import get_session
from repopilot.services import repositories as repository_service
from repopilot.services import snapshots as snapshot_service

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
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return RepositoryResponse.model_validate(repository)


@router.get("", response_model=RepositoryListResponse)
def list_repositories(session: SessionDep) -> RepositoryListResponse:
    items = repository_service.list_repositories(session)
    return RepositoryListResponse(items=[RepositoryResponse.model_validate(r) for r in items])


@router.get("/{repository_id}", response_model=RepositoryResponse)
def get_repository(repository_id: int, session: SessionDep) -> RepositoryResponse:
    return RepositoryResponse.model_validate(
        repository_service.get_repository(session, repository_id)
    )


@router.post(
    "/{repository_id}/imports",
    response_model=SnapshotResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": SnapshotResponse, "description": "相同快照已匯入完成"}},
)
def import_snapshot(
    repository_id: int,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    body: ImportCreate | None = None,
) -> SnapshotResponse:
    """同步匯入指定 revision（預設 HEAD）的固定版本快照；已完成者回 200 與既有快照。"""
    revision = body.revision if body is not None else "HEAD"
    snapshot, created = snapshot_service.import_snapshot(session, repository_id, revision, settings)
    if not created:
        response.status_code = status.HTTP_200_OK
    return SnapshotResponse.model_validate(snapshot)
