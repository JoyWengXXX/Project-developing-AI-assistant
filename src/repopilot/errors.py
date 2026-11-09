class RepoPilotError(Exception):
    """領域例外基底類別；每個子類別有穩定的 `code`，由 API 層轉為 HTTP 狀態碼。"""

    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DatabaseUnavailableError(RepoPilotError):
    """資料庫無法連線或查詢失敗。"""

    code = "database_unavailable"


class InvalidRepositoryPathError(RepoPilotError):
    """路徑不存在、不在 `REPOSITORY_ROOTS` 內，或不是 Git repo 根目錄。"""

    code = "invalid_repository_path"


class RepositoryNotFoundError(RepoPilotError):
    """指定的 Repository 不存在。"""

    code = "repository_not_found"


class InvalidRevisionError(RepoPilotError):
    """revision 格式不合法、無法解析，或解析結果不是 commit。"""

    code = "invalid_revision"


class SnapshotImportInProgressError(RepoPilotError):
    """相同快照正由另一個請求匯入中。"""

    code = "snapshot_import_in_progress"


class ImportLimitExceededError(RepoPilotError):
    """快照超過檔案數或總大小上限。"""

    code = "import_limit_exceeded"


class GitCommandError(RepoPilotError):
    """Git 指令執行失敗（非使用者輸入造成）。"""

    code = "git_command_failed"


class GitTimeoutError(GitCommandError):
    """Git 指令逾時。"""

    code = "git_timeout"
