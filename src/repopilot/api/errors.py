import logging
from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from repopilot.errors import (
    DatabaseUnavailableError,
    GitCommandError,
    GitTimeoutError,
    InvalidRepositoryPathError,
    RepoPilotError,
    RepositoryNotFoundError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.request_context import request_id_var

logger = logging.getLogger(f"{APP_LOGGER_NAME}.api.errors")

# 領域例外 → HTTP 狀態碼；依 MRO 尋找，未列出者回 500。後續切片在此加入對應。
ERROR_STATUS: dict[type[RepoPilotError], int] = {
    DatabaseUnavailableError: 503,
    InvalidRepositoryPathError: 422,
    RepositoryNotFoundError: 404,
    GitCommandError: 500,
    GitTimeoutError: 504,
}

_HTTP_ERROR_CODES = {
    404: "not_found",
    405: "method_not_allowed",
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id_var.get(),
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


def _status_for(exc: RepoPilotError) -> int:
    for cls in type(exc).__mro__:
        if cls in ERROR_STATUS:
            return ERROR_STATUS[cls]
    return 500


def _handle_domain_error(_: Request, raw_exc: Exception) -> JSONResponse:
    exc = cast(RepoPilotError, raw_exc)
    status_code = _status_for(exc)
    if status_code >= 500:
        logger.error("domain_error", extra={"error_code": exc.code})
    return error_response(status_code, exc.code, exc.message)


def _handle_validation_error(_: Request, raw_exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, raw_exc)
    # 不回傳 input／ctx，避免把使用者送出的原始值（可能含秘密）回顯
    details = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return error_response(422, "validation_error", "Request validation failed.", details=details)


def _handle_http_exception(_: Request, raw_exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, raw_exc)
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error."
    return error_response(exc.status_code, code, message, headers=exc.headers)


def _handle_database_error(_: Request, raw_exc: Exception) -> JSONResponse:
    # 只記錄例外型別，避免連線資訊進入日誌或回應
    logger.error("database_error", extra={"error_type": type(raw_exc).__name__})
    return error_response(503, DatabaseUnavailableError.code, "Database is unavailable.")


# Starlette 依註冊的例外型別分派，handler 收到的必為對應型別
def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RepoPilotError, _handle_domain_error)
    app.add_exception_handler(OperationalError, _handle_database_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
