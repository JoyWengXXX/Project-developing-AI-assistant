from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from repopilot.api.schemas import (
    ReferenceListResponse,
    ReferenceRepositoryResponse,
    ReferenceResponse,
    ReferenceSet,
    RepositoryIdPath,
)
from repopilot.db.session import get_session
from repopilot.services import references as reference_service

# 參考版本（切片 3b，D41）：只是標記與持久化，尚未連到 Agent
router = APIRouter(prefix="/api/v1", tags=["references"])

SessionDep = Annotated[Session, Depends(get_session)]


def _to_response(view: reference_service.ReferenceView) -> ReferenceResponse:
    return ReferenceResponse(
        id=view.id,
        repository=ReferenceRepositoryResponse(
            id=view.repository_id,
            name=view.repository_name,
            source_type=view.source_type,
            github_owner=view.github_owner,
            github_name=view.github_name,
        ),
        snapshot_id=view.snapshot_id,
        commit_sha=view.commit_sha,
        snapshot_status=view.snapshot_status,
        file_count=view.file_count,
        skipped_count=view.skipped_count,
        total_bytes=view.total_bytes,
        created_at=view.created_at,
    )


@router.get("/references", response_model=ReferenceListResponse)
def list_references(session: SessionDep) -> ReferenceListResponse:
    """所有 repo 的參考版本（依 repo id 排序）；只讀資料庫，不呼叫 Git。"""
    return ReferenceListResponse(
        items=[_to_response(view) for view in reference_service.list_references(session)]
    )


@router.put(
    "/repositories/{repository_id}/reference",
    response_model=ReferenceResponse,
    responses={
        404: {"description": "repo 不存在，或快照不存在／不屬於該 repo（snapshot_not_found）"},
        409: {"description": "快照尚未 completed（snapshot_not_completed）"},
    },
)
def set_repository_reference(
    repository_id: RepositoryIdPath, body: ReferenceSet, session: SessionDep
) -> ReferenceResponse:
    """把 repo 的某個 completed 快照設為參考版本（upsert，每個 repo 最多一個；重複設定冪等）。"""
    return _to_response(reference_service.set_reference(session, repository_id, body.snapshot_id))


@router.delete(
    "/repositories/{repository_id}/reference",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={404: {"description": "repo 不存在（repository_not_found）"}},
)
def clear_repository_reference(repository_id: RepositoryIdPath, session: SessionDep) -> Response:
    """取消 repo 的參考版本（冪等：本來就沒有也回 204）。"""
    reference_service.clear_reference(session, repository_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
