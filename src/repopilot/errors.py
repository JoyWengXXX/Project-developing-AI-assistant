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


class SnapshotNotFoundError(RepoPilotError):
    """指定的快照不存在。"""

    code = "snapshot_not_found"


class SnapshotFileNotFoundError(RepoPilotError):
    """指定的檔案不存在，或不屬於該快照。"""

    code = "snapshot_file_not_found"


class SnapshotNotCompletedError(RepoPilotError):
    """快照尚未匯入完成（importing 或 failed），不得讀取其檔案內容。"""

    code = "snapshot_not_completed"


class RetrievalSnapshotNotFoundError(SnapshotNotFoundError):
    def __init__(self, snapshot_id: int) -> None:
        super().__init__("A requested snapshot was not found.")
        self.snapshot_id = snapshot_id


class RetrievalSnapshotNotCompletedError(SnapshotNotCompletedError):
    def __init__(self, snapshot_id: int) -> None:
        super().__init__("A requested snapshot has not been imported completely.")
        self.snapshot_id = snapshot_id


class RetrievalIndexNotReadyError(RepoPilotError):
    code = "retrieval_index_not_ready"

    def __init__(self, snapshot_id: int, index_status: str) -> None:
        super().__init__("A requested snapshot has no compatible completed retrieval index.")
        self.snapshot_id = snapshot_id
        self.index_status = index_status


class SnapshotIndexNotReadyError(RepoPilotError):
    """快照已完成，但沒有（目前設定下）`completed` 的結構索引（第二階段切片 6）。

    `index_status` 是 `none`（尚未建立）、`building` 或 `failed`；API 把它放在錯誤的 `details`。
    """

    code = "snapshot_index_not_ready"

    def __init__(self, message: str, *, index_status: str) -> None:
        super().__init__(message)
        self.index_status = index_status


class SearchTimeoutError(RepoPilotError):
    """搜尋超過 `SEARCH_STATEMENT_TIMEOUT_SECONDS`（第二階段切片 8）；不回成功的空列表。"""

    code = "search_timeout"


class SearchScopeTooLargeError(RepoPilotError):
    """選取快照的片段總數超過 `SEARCH_MAX_CHUNKS`：關鍵字比對會掃描全部片段，預期會逾時。

    在執行掃描之前就拒絕（不是等到 504）；`chunk_count`／`max_chunks` 放在錯誤的 `details`。
    """

    code = "search_scope_too_large"

    def __init__(self, chunk_count: int, max_chunks: int) -> None:
        super().__init__(
            "The selected snapshots contain too many chunks for keyword search; "
            "select fewer snapshots or raise SEARCH_MAX_CHUNKS."
        )
        self.chunk_count = chunk_count
        self.max_chunks = max_chunks


class SymbolNotFoundError(RepoPilotError):
    """指定的符號不存在，或不屬於該快照目前的索引。"""

    code = "symbol_not_found"


class SnapshotBusyError(RepoPilotError):
    """快照正被使用（未逾時的匯入、進行中的 job，或快照鎖被持有），暫時不能移除。"""

    code = "snapshot_busy"


class SnapshotReferencedError(RepoPilotError):
    """快照是某個 repo 的參考版本；移除需帶 `force=true`（同時清除參考）。"""

    code = "snapshot_referenced"


class ImportLimitExceededError(RepoPilotError):
    """快照超過檔案數或總大小上限。"""

    code = "import_limit_exceeded"


class TokenEncryptionNotConfiguredError(RepoPilotError):
    """`TOKEN_ENCRYPTION_KEY` 未設定或不是有效的 Fernet 金鑰，無法保存 GitHub token。"""

    code = "token_encryption_not_configured"


class EmbeddingNotConfiguredError(RepoPilotError):
    """模型或執行器的可信識別尚未設定。"""

    code = "embedding_not_configured"


class TokenBudgetExceededError(RepoPilotError):
    """預留額度被拒絕：超過單次上限或每日上限（token 或費用）；帳本沒有寫入任何事件。

    `reason` 為 `per_request_tokens`、`daily_tokens` 或 `daily_cost_usd`；數值以 `int`（token）或
    字串（美元，避免浮點誤差）放在錯誤的 `details`。
    """

    code = "token_budget_exceeded"

    def __init__(
        self, reason: str, *, limit: int | str, requested: int | str, used: int | str | None = None
    ) -> None:
        super().__init__("The token budget does not allow this request.")
        self.reason = reason
        self.limit = limit
        self.requested = requested
        self.used = used


class QaReservationNotFoundError(RepoPilotError):
    """結算或歸還的預留 id 在帳本中沒有 reserve 事件。"""

    code = "qa_reservation_not_found"


class QaReservationFinalizedError(RepoPilotError):
    """預留已經結算或歸還過；同一個預留只能有一個結束事件。"""

    code = "qa_reservation_finalized"


class EmbeddingDeviceUnavailableError(RepoPilotError):
    """選取的 embedding 運算裝置（目前只有 `gpu` 可能不可用）沒有設定或沒有回應。

    不會悄悄改用另一個裝置：使用者以為在用 GPU 卻實際跑 CPU（或反過來）會誤導耗時與資源預期。
    """

    code = "embedding_device_unavailable"


class GitHubTokenInvalidError(RepoPilotError):
    """GitHub 拒絕該 token（401）：無效、過期或已撤銷。"""

    code = "github_token_invalid"


class GitHubUnreachableError(RepoPilotError):
    """無法連線到 GitHub API（DNS、連線被拒、TLS 等）。"""

    code = "github_unreachable"


class GitHubTimeoutError(GitHubUnreachableError):
    """呼叫 GitHub API 逾時。"""

    code = "github_timeout"


class GitHubApiError(RepoPilotError):
    """GitHub 回了非預期的結果（403／速率限制、5xx、非 JSON 等）。

    `status` 是 GitHub 回應的 HTTP 狀態碼（沒有時為 None）；worker 依它把 5xx 歸為暫時性（D44）。
    HTTP 回應的錯誤碼不受影響，仍是 `github_api_error`。
    """

    code = "github_api_error"

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class InvalidGitHubRepoError(RepoPilotError):
    """GitHub repo 參考（`owner/name` 或 URL）格式不合法；訊息不回顯輸入。"""

    code = "invalid_github_repo"


class GitHubRegistrationDisabledError(RepoPilotError):
    """`MANAGED_REPOS_DIR` 未設定，GitHub 註冊功能停用（服務端設定問題）。"""

    code = "github_registration_disabled"


class GitHubRepoNotFoundError(RepoPilotError):
    """GitHub 回 404：repo 不存在，或 token（未提供／權限不足）看不到該 repo。"""

    code = "github_repo_not_found_or_private"


class RepositoryNotGitHubError(RepoPilotError):
    """該 repo 不是 GitHub 來源，不能 fetch。"""

    code = "repository_not_github"


class RepositoryBusyError(RepoPilotError):
    """repo 正在被使用（未逾時的匯入，或 GitHub 來源的 clone／fetch 進行中），暫時不能移除。"""

    code = "repository_busy"


class ManagedStorageError(RepoPilotError):
    """受管目錄的檔案系統操作失敗（建立目錄、rename 等）。"""

    code = "managed_storage_error"


class JobNotFoundError(RepoPilotError):
    """指定的背景工作不存在（含 repo 被移除而連帶刪除）。"""

    code = "job_not_found"


class JobNotRetryableError(RepoPilotError):
    """只有 `failed` 的工作可以重試；或已有相同工作正在進行，重試會違反 dedupe 唯一性。"""

    code = "job_not_retryable"


class JobNotCancellableError(RepoPilotError):
    """工作已結束（`succeeded`／`failed`），不能取消。"""

    code = "job_not_cancellable"


class JobLeaseLostError(RepoPilotError):
    """呼叫者持有的令牌不再是該工作的租約擁有者（租約已被他人取得、工作已結束或不存在）。

    只出現在 worker 端的寫入（進度、心跳、完成、失敗）；被拒絕的寫入不會改動任何資料。
    """

    code = "job_lease_lost"


class PathNotMappedError(RepoPilotError):
    """資料庫中的主機路徑無法換成 worker 容器內的路徑。

    原因是沒有符合的 `WORKER_PATH_MAPPINGS`，或路徑不合格。worker 端出現時是 job 的
    `error.code`（永久性）；API 在建立 job 前檢查，轉成 `WorkerPathNotMappedError`（503）。
    """

    code = "path_not_mapped"


class WorkerPathNotMappedError(RepoPilotError):
    """API 建立 job 前發現 repo 路徑不能映射到 worker 容器。

    這是服務端設定問題，不是使用者輸入錯誤，故 503。
    """

    code = "worker_path_not_mapped"


class ImportCancelledError(RepoPilotError):
    """worker 在檢查點發現使用者已要求取消，中止匯入。

    快照會被標記為 failed（可被後續匯入接手）。
    """

    code = "import_cancelled"


class ProfileMismatchError(RepoPilotError):
    """worker 重新計算的匯入設定雜湊與 job 記錄的不同。

    代表 API 與 worker 的 `IMPORT_*` 設定不一致（永久性）。
    """

    code = "profile_mismatch"


class GitCommandError(RepoPilotError):
    """Git 指令執行失敗（非使用者輸入造成）。"""

    code = "git_command_failed"


class GitTimeoutError(GitCommandError):
    """Git 指令逾時。"""

    code = "git_timeout"


class GitHubCloneError(GitCommandError):
    """`git clone` 失敗（遠端不存在、認證失敗、網路錯誤等）；訊息不含 stderr 原文。"""

    code = "github_clone_failed"


class GitHubCloneTimeoutError(GitTimeoutError):
    """`git clone` 逾時。"""

    code = "github_clone_timeout"


class GitHubFetchError(GitCommandError):
    """`git fetch` 失敗；訊息不含 stderr 原文。"""

    code = "github_fetch_failed"


class GitHubFetchTimeoutError(GitTimeoutError):
    """`git fetch` 逾時。"""

    code = "github_fetch_timeout"
