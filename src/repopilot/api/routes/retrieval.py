from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    EmbeddingDevicesResponse,
    RetrievalSearchItem,
    RetrievalSearchRequest,
    RetrievalSearchResponse,
)
from repopilot.db.session import get_session
from repopilot.embedding import gateway_client
from repopilot.services.retrieval_search import search

router = APIRouter(prefix="/api/v1/retrieval", tags=["retrieval"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/devices",
    response_model=EmbeddingDevicesResponse,
    responses={503: {"description": "本機 embedding gateway 沒有設定或沒有回應"}},
)
def get_embedding_devices(settings: SettingsDep) -> EmbeddingDevicesResponse:
    """建立檢索索引可選的運算裝置（CPU／GPU）與各自目前是否可用。"""
    return EmbeddingDevicesResponse.model_validate(gateway_client.embedding_devices(settings))


@router.post("/search", response_model=RetrievalSearchResponse, response_model_exclude_none=True)
def search_retrieval(
    request: RetrievalSearchRequest, session: SessionDep, settings: SettingsDep
) -> RetrievalSearchResponse:
    items = search(
        session,
        settings,
        snapshot_ids=request.snapshot_ids,
        query=request.query,
        mode=request.mode,
        limit=request.limit,
        file_kinds=request.file_kinds,
        path_prefix=request.path_prefix,
    )
    return RetrievalSearchResponse(
        mode=request.mode, items=[RetrievalSearchItem(**asdict(item)) for item in items]
    )
