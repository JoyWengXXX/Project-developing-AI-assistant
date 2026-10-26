import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from repopilot.db.session import get_session
from repopilot.errors import DatabaseUnavailableError
from repopilot.logging_config import APP_LOGGER_NAME

router = APIRouter(tags=["health"])
logger = logging.getLogger(f"{APP_LOGGER_NAME}.api.ready")


class ReadyResponse(BaseModel):
    status: Literal["ready"]


@router.get("/ready", response_model=ReadyResponse)
def ready(session: Annotated[Session, Depends(get_session)]) -> ReadyResponse:
    """驗證資料庫連線；無法連線回 503。"""
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        # 只記錄例外型別，避免連線資訊進入日誌或回應
        logger.warning("database_check_failed", extra={"error_type": type(exc).__name__})
        raise DatabaseUnavailableError("Database is unavailable.") from None
    return ReadyResponse(status="ready")
