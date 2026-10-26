class RepoPilotError(Exception):
    """領域例外基底類別；每個子類別有穩定的 `code`，由 API 層轉為 HTTP 狀態碼。"""

    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DatabaseUnavailableError(RepoPilotError):
    """資料庫無法連線或查詢失敗。"""

    code = "database_unavailable"
