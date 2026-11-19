import logging
from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from repopilot.embedding.local_runtime import EmbeddingInputTooLongError, EmbeddingRuntimeError
from repopilot.errors import (
    DatabaseUnavailableError,
    EmbeddingDeviceUnavailableError,
    EmbeddingNotConfiguredError,
    GitCommandError,
    GitHubApiError,
    GitHubCloneError,
    GitHubCloneTimeoutError,
    GitHubFetchError,
    GitHubFetchTimeoutError,
    GitHubRegistrationDisabledError,
    GitHubRepoNotFoundError,
    GitHubTimeoutError,
    GitHubTokenInvalidError,
    GitHubUnreachableError,
    GitTimeoutError,
    ImportCancelledError,
    ImportLimitExceededError,
    InvalidGitHubRepoError,
    InvalidRepositoryPathError,
    InvalidRevisionError,
    JobLeaseLostError,
    JobNotCancellableError,
    JobNotFoundError,
    JobNotRetryableError,
    ManagedStorageError,
    PathNotMappedError,
    QaEgressNotConsentedError,
    QaModelNotSelectedError,
    QaProviderNotConfiguredError,
    QaQuestionActiveError,
    QaQuestionFailedError,
    QaQuestionNotFoundError,
    QaReservationFinalizedError,
    QaReservationNotFoundError,
    RepoPilotError,
    RepositoryBusyError,
    RepositoryNotFoundError,
    RepositoryNotGitHubError,
    RetrievalIndexNotReadyError,
    RetrievalSnapshotNotCompletedError,
    RetrievalSnapshotNotFoundError,
    SearchScopeTooLargeError,
    SearchTimeoutError,
    SnapshotBusyError,
    SnapshotFileNotFoundError,
    SnapshotImportInProgressError,
    SnapshotIndexNotReadyError,
    SnapshotNotCompletedError,
    SnapshotNotFoundError,
    SnapshotReferencedError,
    SymbolNotFoundError,
    TokenBudgetExceededError,
    TokenEncryptionNotConfiguredError,
    WorkerPathNotMappedError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.request_context import request_id_var

logger = logging.getLogger(f"{APP_LOGGER_NAME}.api.errors")

# 領域例外 → HTTP 狀態碼；依 MRO 尋找，未列出者回 500。後續切片在此加入對應。
ERROR_STATUS: dict[type[RepoPilotError], int] = {
    DatabaseUnavailableError: 503,
    InvalidRepositoryPathError: 422,
    RepositoryNotFoundError: 404,
    InvalidRevisionError: 422,
    ImportLimitExceededError: 422,
    SnapshotImportInProgressError: 409,
    SnapshotNotFoundError: 404,
    SnapshotFileNotFoundError: 404,
    SnapshotNotCompletedError: 409,
    # 參考版本與移除快照（第二階段切片 3b，D41）：兩者都是「稍後重試／帶 force 重試」的衝突
    SnapshotBusyError: 409,
    SnapshotReferencedError: 409,
    GitCommandError: 500,
    GitTimeoutError: 504,
    # 設定問題（服務端缺金鑰）不是使用者輸入錯誤，故 503 而非 422
    TokenEncryptionNotConfiguredError: 503,
    EmbeddingNotConfiguredError: 503,
    EmbeddingDeviceUnavailableError: 503,
    EmbeddingRuntimeError: 503,
    EmbeddingInputTooLongError: 422,
    GitHubTokenInvalidError: 422,
    # 上游（GitHub）問題：連不上／非預期回應為 502，逾時為 504；GitHubTimeoutError 依 MRO 先於父類別
    GitHubUnreachableError: 502,
    GitHubTimeoutError: 504,
    GitHubApiError: 502,
    # GitHub repo 註冊與 clone／fetch（切片 5c-2）
    InvalidGitHubRepoError: 422,
    # 與 token_encryption_not_configured 同理：服務端設定問題，回 503 而非 409／422
    GitHubRegistrationDisabledError: 503,
    GitHubRepoNotFoundError: 404,
    RepositoryNotGitHubError: 409,
    GitHubCloneError: 502,
    GitHubCloneTimeoutError: 504,
    GitHubFetchError: 502,
    GitHubFetchTimeoutError: 504,
    ManagedStorageError: 500,
    # 移除 repo（D33）：有未逾時的匯入、或 GitHub clone／fetch 進行中；稍後重試即可
    RepositoryBusyError: 409,
    # 背景工作（第二階段切片 2，D39）。JobLeaseLostError 只在 worker 端寫入時出現，沒有 HTTP 端點
    # 會拋出它；仍列出對照，避免日後被誤當成 500
    JobNotFoundError: 404,
    JobNotRetryableError: 409,
    JobNotCancellableError: 409,
    JobLeaseLostError: 409,
    # 第二階段切片 3：repo 路徑無法映射到 worker 容器是部署設定問題，與
    # github_registration_disabled 同理回 503。PathNotMappedError／ImportCancelledError 只出現在
    # worker（成為 job 的 error.code），沒有 HTTP 端點會拋出
    WorkerPathNotMappedError: 503,
    PathNotMappedError: 503,
    ImportCancelledError: 409,
    # 結構索引（第二階段切片 6）：索引尚未就緒是「稍後再查」的衝突；符號不在該索引內是 404
    SnapshotIndexNotReadyError: 409,
    RetrievalIndexNotReadyError: 409,
    SymbolNotFoundError: 404,
    # 搜尋（第二階段切片 8）：逾時是 504（同 Git 逾時），不是資料庫不可用的 503
    SearchTimeoutError: 504,
    # 選取範圍太大：在掃描前拒絕，是輸入（選取的快照）的問題，不是伺服器逾時
    SearchScopeTooLargeError: 422,
    # 問答額度（第四階段 4-1b）：超過額度是「目前狀態不允許」的衝突；不用 402，
    # 以免與供應商端的 provider_payment_required 混淆。重複結算同一預留也是衝突
    TokenBudgetExceededError: 409,
    QaReservationNotFoundError: 404,
    QaReservationFinalizedError: 409,
    # 問答（第四階段 4-3a）：供應商未設定是服務端設定（503，同 token_encryption_not_configured）；
    # 未選模型、問答還在進行是目前狀態不允許（409）；未同意資料外送是授權問題（403）
    QaProviderNotConfiguredError: 503,
    QaModelNotSelectedError: 409,
    QaEgressNotConsentedError: 403,
    QaQuestionNotFoundError: 404,
    QaQuestionActiveError: 409,
    # 只在 worker 內部使用（成為 job 的 error.code），沒有 HTTP 端點會拋出
    QaQuestionFailedError: 500,
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
    details: list[dict[str, Any]] | dict[str, Any] | None = None,
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
    return error_response(status_code, exc.code, exc.message, details=_details_of(exc))


def _details_of(exc: RepoPilotError) -> dict[str, Any] | None:
    if isinstance(exc, RetrievalIndexNotReadyError):
        return {"snapshot_id": exc.snapshot_id, "index_status": exc.index_status}
    if isinstance(exc, RetrievalSnapshotNotFoundError | RetrievalSnapshotNotCompletedError):
        return {"snapshot_id": exc.snapshot_id}
    if isinstance(exc, SearchScopeTooLargeError):
        return {"chunk_count": exc.chunk_count, "max_chunks": exc.max_chunks}
    if isinstance(exc, SnapshotIndexNotReadyError):
        return {"index_status": exc.index_status}
    if isinstance(exc, QaEgressNotConsentedError):
        return {
            "repositories": [
                {"repository_id": repo_id, "name": name} for repo_id, name in exc.repositories
            ]
        }
    if isinstance(exc, TokenBudgetExceededError):
        details: dict[str, Any] = {
            "reason": exc.reason,
            "limit": exc.limit,
            "requested": exc.requested,
        }
        if exc.used is not None:
            details["used"] = exc.used
        return details
    return None


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
