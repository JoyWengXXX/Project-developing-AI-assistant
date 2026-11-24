from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from repopilot.api.job_responses import to_job_response
from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    ReferenceEmbeddingResponse,
    ReferenceListResponse,
    ReferenceRepositoryResponse,
    ReferenceResponse,
    ReferenceSet,
    ReferenceSetResponse,
    RepositoryIdPath,
)
from repopilot.db.session import get_session
from repopilot.request_context import request_id_var
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
    response_model=ReferenceSetResponse,
    responses={
        404: {"description": "repo 不存在，或快照不存在／不屬於該 repo（snapshot_not_found）"},
        409: {"description": "快照尚未 completed（snapshot_not_completed）"},
    },
)
def set_repository_reference(
    repository_id: RepositoryIdPath,
    body: ReferenceSet,
    session: SessionDep,
    settings: SettingsDep,
) -> ReferenceSetResponse:
    """把 repo 的某個 completed 快照設為參考版本（upsert，每個 repo 最多一個；重複設定冪等）。

    成功後若結構索引已完成且 embedding 已設定，會排入檢索 embedding（裝置預設 GPU，
    送出時不可用改用 CPU）；結果寫在 `embedding`，略過時參考版本仍成功。
    """
    result = reference_service.set_reference(
        session, repository_id, body.snapshot_id, settings, request_id=request_id_var.get()
    )
    embedding = result.embedding
    return ReferenceSetResponse(
        **_to_response(result.reference).model_dump(),
        embedding=ReferenceEmbeddingResponse(
            status=embedding.status,
            message=embedding.message,
            reason=embedding.reason,
            job=to_job_response(embedding.job) if embedding.job is not None else None,
            device=embedding.device,
            device_fallback=embedding.device_fallback,
        ),
    )


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
