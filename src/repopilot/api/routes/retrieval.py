from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    RetrievalSearchItem,
    RetrievalSearchRequest,
    RetrievalSearchResponse,
)
from repopilot.db.session import get_session
from repopilot.services.retrieval_search import search

router = APIRouter(prefix="/api/v1/retrieval", tags=["retrieval"])
SessionDep = Annotated[Session, Depends(get_session)]


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
    )
    return RetrievalSearchResponse(
        mode=request.mode, items=[RetrievalSearchItem(**asdict(item)) for item in items]
    )
