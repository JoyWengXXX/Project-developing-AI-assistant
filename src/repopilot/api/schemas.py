from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import Path, Query
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from repopilot.search_query import keyword_terms

RepositoryName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]

# --- 查詢端共用的路徑／查詢參數型別（FastAPI 驗證失敗統一回 422 validation_error）---
# id 欄位的資料庫型別：repositories.id、snapshots.id 為 INTEGER，snapshot_files.id 為 BIGINT；
# 先在 API 層限制範圍，超出範圍的值不會送進資料庫而變成 500。
_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1

RepositoryIdPath = Annotated[int, Path(ge=1, le=_INT32_MAX)]
SnapshotIdPath = Annotated[int, Path(ge=1, le=_INT32_MAX)]
FileIdPath = Annotated[int, Path(ge=1, le=_INT64_MAX)]
# jobs.id 為 BIGINT（第二階段切片 2）；範圍驗證同 D30／D37，超出回 422 而不是 500
JobIdPath = Annotated[int, Path(ge=1, le=_INT64_MAX)]
# 工作列表的篩選條件；範圍同對應的路徑參數（INTEGER 欄位）
RepositoryIdQuery = Annotated[int, Query(ge=1, le=_INT32_MAX, description="只看此 repo 的工作")]
SnapshotIdQuery = Annotated[int, Query(ge=1, le=_INT32_MAX, description="只看此快照的工作")]

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT, description="每頁筆數，最多 200")]
OffsetQuery = Annotated[int, Query(ge=0, le=_INT32_MAX, description="略過筆數")]

SnapshotStatus = Literal["importing", "completed", "failed"]
FileStatus = Literal["imported", "skipped"]


class RepositoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 本機絕對路徑；是否合法由 service 驗證（需存取檔案系統與 Git）
    path: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    name: RepositoryName | None = None


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    canonical_local_path: str
    created_at: datetime
    # 切片 5c-2 新增（純增加）：來源與 GitHub 資訊；local 來源的 github_* 皆為 null。
    # github_is_private／github_default_branch 是註冊當下的值，之後可能過期
    source_type: Literal["local", "github"]
    github_owner: str | None
    github_name: str | None
    github_is_private: bool | None
    github_default_branch: str | None


class GitHubRepositoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `owner/name` 或 `https://github.com/owner/name(.git)`。刻意不設長度限制：格式不合法
    # （含過長）一律由 service 回 422 invalid_github_repo，且不回顯輸入
    repo: str


class RefChangeResponse(BaseModel):
    """fetch 前後的 ref 差異；`name` 可直接作為匯入的 revision（`origin/main`、`v1.0`）。"""

    name: str
    kind: Literal["branch", "tag"]
    change: Literal["added", "updated", "removed"]
    old_sha: str | None
    new_sha: str | None


class FetchResponse(BaseModel):
    repository_id: int
    # 依名稱排序；最多 100 筆，超過時 truncated 為 true
    updated_refs: list[RefChangeResponse]
    truncated: bool


class RepositoryListResponse(BaseModel):
    items: list[RepositoryResponse]


class RepositoryRemovedResponse(BaseModel):
    """移除 repo 的摘要；`removed_clone` 只有 GitHub 來源且目錄真的被刪除時才為 true。"""

    repository_id: int
    source_type: Literal["local", "github"]
    deleted_snapshots: int
    deleted_files: int
    removed_clone: bool


# --- 版本瀏覽（切片 5e-1，D34）---
DEFAULT_COMMIT_LIMIT = 100
MAX_COMMIT_LIMIT = 500
MAX_COMMIT_OFFSET = 100_000
CommitLimitQuery = Annotated[
    int, Query(ge=1, le=MAX_COMMIT_LIMIT, description="每頁筆數，最多 500")
]
CommitOffsetQuery = Annotated[int, Query(ge=0, le=MAX_COMMIT_OFFSET, description="略過筆數")]


class BranchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # 顯示名稱（GitHub 來源已去掉 `origin/`）；`ref` 是完整名稱，可直接作為 commits 的 `ref`
    name: str
    ref: str
    sha: str
    is_default: bool


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    ref: str
    # 附註標籤已還原為 commit SHA
    sha: str


class RefsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # 預設分支的完整 ref；detached HEAD、空 repo 或找不到預設分支時為 null
    default_ref: str | None
    head_sha: str | None
    branches: list[BranchResponse]
    tags: list[TagResponse]
    # 分支或標籤超過 500 個時為 true（只列出依名稱排序的前 500 個）
    truncated: bool


class CommitRefResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    kind: Literal["branch", "tag"]


class CommitResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sha: str
    parents: list[str]
    # 截斷為 200 字元；不含作者 email
    subject: str
    author_name: str
    # UTC（以 Z 結尾的 ISO 8601）
    authored_at: datetime
    refs: list[CommitRefResponse]
    # 此 repo 已有此 SHA 的 completed 快照（以目前匯入設定）；`POST /imports` 會回 200
    imported: bool


class CommitListResponse(BaseModel):
    items: list[CommitResponse]
    total_returned: int
    has_more: bool


# --- repo 概況（切片 5d-1，D35）---


class OverviewHeadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sha: str
    # 截斷為 200 字元；不含作者 email
    subject: str
    author_name: str
    # UTC（以 Z 結尾的 ISO 8601）
    authored_at: datetime


class OverviewSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    commit_sha: str
    status: str


class OverviewStatsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    imported_files: int
    # 非 imported 的檔案數（二進位、過大、被排除、symlink、submodule 等）
    skipped_files: int
    # imported 檔案的 byte_size 總和
    total_bytes: int


class OverviewLanguageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # 依副檔名對照；不認得者與超過上限的尾端語言合併為「其他」（永遠排最後）
    language: str
    files: int
    bytes: int


class OverviewReadmeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    path: str
    # 原文前 4000 字元（不可信內容，只當純文字顯示）
    text: str
    truncated: bool


class ProjectInfoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: Literal["pyproject", "requirements", "package_json", "csproj"]
    path: str
    name: str | None
    version: str | None
    # 最多 50 筆，每筆最多 200 字元；格式依 kind 而異（見 D35）
    dependencies: list[str]
    truncated: bool
    # 只有 csproj 會有值
    target_frameworks: list[str]
    # 檔案超過 256 KiB 而未讀取時為 "too_large"
    skipped_reason: str | None
    # "invalid_toml"、"invalid_json"、"not_an_object"（穩定代碼，不含檔案內容）
    parse_error: str | None


class RepositoryOverviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repository_id: int
    default_ref: str | None
    head: OverviewHeadResponse | None
    # null＝預設分支最新 commit 在目前匯入設定下尚未有 completed 快照（或空 repo）
    snapshot: OverviewSnapshotResponse | None
    # 以下四欄只有 analyzed=true 才有內容：stats 與 readme 為 null、其餘為空陣列
    stats: OverviewStatsResponse | None
    languages: list[OverviewLanguageResponse]
    readme: OverviewReadmeResponse | None
    project_info: list[ProjectInfoResponse]
    analyzed: bool


class ImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 格式與可解析性由 git 層驗證（需存取 repo）；此處只限制長度
    revision: Annotated[str, StringConstraints(min_length=1, max_length=256)] = "HEAD"


class SnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    commit_sha: str
    import_profile_hash: str
    status: str
    file_count: int
    skipped_count: int
    total_bytes: int
    started_at: datetime
    completed_at: datetime | None
    error_summary: str | None


class SnapshotItemResponse(SnapshotResponse):
    """快照列表與詳情的項目（切片 3b 只做增量：在 `SnapshotResponse` 之上加兩個欄位）。

    `POST /imports` 仍回 `SnapshotResponse`（不含這兩欄），契約不變。
    """

    # 是否為所屬 repo 的參考版本
    is_referenced: bool
    # 快照的匯入設定雜湊與「目前」設定不同（設定改過之後匯入的快照才是新的）
    is_stale_profile: bool


class PageResponse(BaseModel):
    """分頁欄位：`total` 為符合條件的總數，不受 limit／offset 影響。"""

    total: int
    limit: int
    offset: int


class SnapshotListResponse(PageResponse):
    # 不含檔案內容；包含非 completed 的快照，以 status 區分
    items: list[SnapshotItemResponse]


class SnapshotDetailResponse(SnapshotItemResponse):
    # 只有 completed 快照有統計；importing／failed 為空字典（不把殘留列當成有效資料）
    file_status_counts: dict[str, int]
    skip_reason_counts: dict[str, int]


class SnapshotRemovedResponse(BaseModel):
    """移除快照的摘要（切片 3b，D41）；`was_referenced`：移除前它是參考版本（需 `force`）。"""

    snapshot_id: int
    repository_id: int
    deleted_files: int
    was_referenced: bool


class ReferenceSet(BaseModel):
    """`PUT /repositories/{id}/reference` 的 body；快照必須屬於該 repo 且為 completed。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: Annotated[int, Field(ge=1, le=_INT32_MAX)]


class ReferenceRepositoryResponse(BaseModel):
    id: int
    name: str
    source_type: Literal["local", "github"]
    github_owner: str | None
    github_name: str | None


class ReferenceResponse(BaseModel):
    """repo 的參考版本（只是標記，尚未連到 Agent）。快照欄位是**現在**的狀態。"""

    id: int
    repository: ReferenceRepositoryResponse
    snapshot_id: int
    commit_sha: str
    snapshot_status: str
    file_count: int
    skipped_count: int
    total_bytes: int
    created_at: datetime


class ReferenceListResponse(BaseModel):
    items: list[ReferenceResponse]


class SnapshotFileSummary(BaseModel):
    """檔案列表項目：不含 `content`。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    snapshot_id: int
    path: str
    git_blob_id: str | None
    sha256: str | None
    byte_size: int | None
    status: str
    skip_reason: str | None


class SnapshotFileDetail(SnapshotFileSummary):
    # imported 才有內容；skipped（二進位、無效 UTF-8、排除檔等）一律為 None
    content: str | None


class SnapshotFileListResponse(PageResponse):
    items: list[SnapshotFileSummary]


# GitHub token：去除前後空白後只允許可見 ASCII（無空白、換行、控制字元；
# HTTP 標頭也不接受非 ASCII）。
# 255 是寬鬆的合理上限（fine-grained PAT 約 93 字元），不是 GitHub 保證的格式。
# 驗證失敗的回應不回顯輸入（見 api/errors.py 的 422 處理）。
GitHubTokenInput = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[\x21-\x7e]+$"
    ),
]


class GitHubTokenSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: GitHubTokenInput


class GitHubAuthResponse(BaseModel):
    """token 狀態；欄位刻意不含 token 與密文。"""

    authenticated: bool
    login: str | None
    token_type: Literal["fine_grained", "classic", "unknown"] | None
    warning: str | None


# --- 背景工作（第二階段切片 2，D39）---
JobKind = Literal[
    "snapshot_import", "snapshot_index", "github_register", "github_fetch", "snapshot_retrieval"
]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
# `active` ＝ queued＋running（供進行中面板輪詢）
JobStatusFilter = Literal["queued", "running", "succeeded", "failed", "cancelled", "active"]
JobPhase = Literal[
    "pending",
    "importing",
    "indexing",
    "resolving",
    "cloning",
    "registering",
    "fetching",
    "embedding",
    "completed",
]


class JobProgressResponse(BaseModel):
    processed_files: int
    # null＝總數尚未知；介面不得偽造百分比
    total_files: int | None
    indexed_files: int
    parse_error_count: int
    # 暫存目錄大小（非百分比）；沒有量測時為 null
    bytes_received: int | None


class JobErrorResponse(BaseModel):
    """只含穩定的錯誤碼與安全摘要（不含路徑、token、檔案內容或 Git stderr）。"""

    code: str
    message: str


class JobUnitResponse(BaseModel):
    """worker 目前處理的單位（只在 running 時有值）：種類與第幾個，沒有識別、路徑或內容。"""

    kind: str
    index: int | None
    count: int | None


class JobPoisonResponse(BaseModel):
    """毒藥單位的計數（D43）：同一單位連續讓 worker 崩潰的次數、被略過的單位數、總崩潰次數。"""

    suspect_crashes: int
    skipped_units: int
    total_crashes: int


class JobDiagnosticsResponse(BaseModel):
    """失敗追查用的 `work_state` 安全摘要（第二階段切片 4）；`work_state` 本身不外露。"""

    current_unit: JobUnitResponse | None
    poison: JobPoisonResponse


class JobResponse(BaseModel):
    """背景工作。刻意**不含**租約令牌與 `work_state`（worker 內部資料）。"""

    id: int
    kind: JobKind
    status: JobStatus
    phase: JobPhase
    repository_id: int | None
    snapshot_id: int | None
    params: dict[str, Any]
    attempt: int
    max_attempts: int
    next_attempt_at: datetime | None
    cancel_requested: bool
    # 使用者要求取消的時間（切片 4 新增）；null＝沒有要求。cancel_requested 是它的布林版本
    cancel_requested_at: datetime | None
    progress: JobProgressResponse
    diagnostics: JobDiagnosticsResponse
    error: JobErrorResponse | None
    result: dict[str, Any] | None
    request_id: str | None
    created_at: datetime
    started_at: datetime | None
    phase_started_at: datetime | None
    last_heartbeat_at: datetime | None
    finished_at: datetime | None
    # 輪詢時用來判斷回應新舊（規格 10.2）
    updated_at: datetime
    status_url: str


class JobListResponse(PageResponse):
    items: list[JobResponse]


class JobSubmitResponse(BaseModel):
    """建立工作端點的回應（第二階段切片 3，規格 4.4）。

    - 202：`job` 為新建、重新排入或既有的進行中 job（`reused=false`）。
    - 200：相同快照已 `completed`，不需要 job：`snapshot_id` 有值、`reused=true`；`job` 是
      產生該快照的最近一次成功 job，若快照來自第一階段的同步匯入（沒有 job）則為 `null`。
    - 切片 5（GitHub 註冊）：`registration-jobs` 回 202 時 `repository_id` 為 `null`（資料列要到
      worker 成功後才存在，之後讀 `job.result.repository_id`）；repo 已註冊則 200、`job` 為 `null`、
      `reused=true`、`repository_id` 有值。
    """

    job: JobResponse | None
    snapshot_id: int | None
    repository_id: int | None
    reused: bool


class RetrievalIndexResponse(BaseModel):
    snapshot_id: int
    repository_id: int
    status: Literal["none", "building", "failed", "completed"]
    index_id: int | None
    profile_hash: str
    chunk_count: int | None
    segment_count: int | None
    job: JobResponse | None


class RetrievalSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_ids: list[Annotated[int, Field(strict=True, ge=1, le=_INT32_MAX)]] = Field(
        min_length=1, max_length=20
    )
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
    mode: Literal["keyword", "semantic", "hybrid"]
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("snapshot_ids")
    @classmethod
    def _unique_snapshots(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("Snapshot IDs must be unique.")
        return value

    @field_validator("query")
    @classmethod
    def _valid_query(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("The search query contains an invalid character.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("The search query is not valid UTF-8.") from None
        return value

    @model_validator(mode="after")
    def _valid_keyword_query(self) -> Self:
        if self.mode != "semantic":
            keyword_terms(self.query)
        return self


class RetrievalSearchItem(BaseModel):
    chunk_id: int
    repository_id: int
    snapshot_id: int
    commit_sha: str
    path: str
    start_line: int
    end_line: int
    content: str
    split_kind: Literal["structure", "paragraph", "text"]
    support_level: Literal["structure", "text"]
    keyword_score: int | None = None
    semantic_score: float | None = None
    hybrid_score: float | None = None


class RetrievalSearchResponse(BaseModel):
    mode: Literal["keyword", "semantic", "hybrid"]
    items: list[RetrievalSearchItem]


class ServiceStateResponse(BaseModel):
    # online：至少一個實例的心跳未過期；stale：有紀錄但都過期；absent：從來沒有紀錄
    status: Literal["online", "stale", "absent"]
    last_seen_at: datetime | None
    instances_online: int


class JobsStatusResponse(BaseModel):
    """是否有 dispatcher／worker 在線（C8）與佇列數量；API 不連 Redis，只讀資料庫。"""

    dispatcher: ServiceStateResponse
    worker: ServiceStateResponse
    queued: int
    running: int
    # 心跳超過此秒數未更新視為離線（SERVICE_HEARTBEAT_SECONDS 的 3 倍）
    heartbeat_timeout_seconds: int


# --- 結構索引（第二階段切片 6，規格 4.7）---
IndexStatus = Literal["none", "building", "completed", "failed"]
IndexLanguage = Literal["python", "csharp", "typescript", "javascript", "java", "go", "rust", "sql"]
ParseStatusValue = Literal["parsed", "parsed_with_errors", "failed"]
SymbolIdPath = Annotated[int, Path(ge=1, le=_INT64_MAX)]
IndexedFileIdQuery = Annotated[int, Query(ge=1, le=_INT64_MAX, description="只看此快照檔案的符號")]
ParentIdQuery = Annotated[int, Query(ge=1, le=_INT64_MAX, description="只看此符號的直接子符號")]
SymbolKindFilter = Literal[
    "module",
    "namespace",
    "class",
    "struct",
    "record",
    "interface",
    "type_alias",
    "enum",
    "enum_member",
    "delegate",
    "function",
    "method",
    "constructor",
    "property",
    "field",
    "table",
    "view",
    "procedure",
]


class IndexCountsResponse(BaseModel):
    files_by_language: dict[str, int]
    files_by_parse_status: dict[str, int]
    symbols: int
    symbols_by_kind: dict[str, int]
    # 符號數超過單檔上限而被截斷的檔案數
    truncated_files: int


class SnapshotIndexResponse(BaseModel):
    """快照在**目前設定**下的結構索引狀態；沒有索引時 200＋`status="none"`（不是錯誤）。

    只有 `completed` 才有 `counts`。`analyzers` 是抽取器與版本（Python 主次版本、演算法版本），
    不含秘密；`error_summary` 是固定的安全摘要。
    """

    snapshot_id: int
    status: IndexStatus
    index_id: int | None
    started_at: datetime | None
    completed_at: datetime | None
    analyzers: dict[str, Any]
    counts: IndexCountsResponse | None
    error_summary: str | None


class FirstErrorResponse(BaseModel):
    line: int
    # 0-based UTF-8 位元組位移（規格 7.1）
    column: int


class IndexedFileResponse(BaseModel):
    """被索引的檔案與解析狀態；**不含任何原始程式碼片段或語法錯誤訊息**。"""

    indexed_file_id: int
    file_id: int
    path: str
    language: IndexLanguage
    parse_status: ParseStatusValue
    error_kind: str | None
    error_count: int
    first_error: FirstErrorResponse | None
    symbol_count: int
    symbols_truncated: bool


class IndexedFileListResponse(PageResponse):
    items: list[IndexedFileResponse]


class SymbolSummaryResponse(BaseModel):
    symbol_id: int
    kind: str
    language: IndexLanguage
    name: str
    qualified_name: str
    file_id: int
    path: str
    start_line: int
    end_line: int
    parent_id: int | None
    modifiers: str | None
    is_async: bool
    has_errors: bool
    conditional: bool
    # 是否有直接子符號（符號樹逐層載入用）
    has_children: bool


class SymbolListResponse(PageResponse):
    items: list[SymbolSummaryResponse]


class ImportReferenceResponse(BaseModel):
    kind: str
    target: str
    imported_name: str | None
    alias: str | None
    relative_level: int | None
    line: int


class SymbolRelationResponse(BaseModel):
    relation_type: str
    target_name: str
    # 只有 resolved_same_file 才有值；ambiguous 與 unresolved 一律為 null（不猜測）
    target_symbol_id: int | None
    resolution: str
    line: int


class SymbolDetailResponse(SymbolSummaryResponse):
    # 0-based UTF-8 位元組位移
    start_column: int
    end_column: int
    # Python 裝飾器的起始行（沒有則 null）
    leading_start_line: int | None
    type_parameter_count: int
    # 同一檔案的 import（最多 200 筆，依行號排序）
    imports: list[ImportReferenceResponse]
    relations: list[SymbolRelationResponse]


# --- 搜尋（第二階段切片 8，規格 8）---
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
SEARCH_MAX_OFFSET = 10_000


def _validate_search_text(value: str) -> str:
    # 不 strip：字面比對時前後空白有意義；但全空白、換行與 NUL 沒有搜尋意義（或無法比對）
    if not value.strip():
        raise ValueError("q must not be blank.")
    if any(char in value for char in ("\n", "\r", "\x00")):
        raise ValueError("q must not contain line breaks or NUL.")
    return value


SearchTextQuery = Annotated[
    str,
    Query(min_length=1, max_length=200, description="字面子字串（區分大小寫），1～200 字元"),
    AfterValidator(_validate_search_text),
]
SearchLimitQuery = Annotated[
    int, Query(ge=1, le=SEARCH_MAX_LIMIT, description="每頁筆數，最多 100")
]
SearchOffsetQuery = Annotated[int, Query(ge=0, le=SEARCH_MAX_OFFSET, description="略過筆數")]
SearchPathPrefixQuery = Annotated[
    str | None, Query(max_length=1024, description="路徑前綴（區分大小寫，不是萬用字元）")
]


class SearchResponse(BaseModel):
    """搜尋回應外框：URL 已釘住同一個快照，所以 repo 與完整 commit SHA 放在外框而不是每筆命中。

    `total` 在內容搜尋為 null（精確總數需要掃完所有命中）；任何端點都可用 `has_more` 分頁。
    """

    snapshot_id: int
    repository_id: int
    commit_sha: str
    total: int | None
    limit: int
    offset: int
    has_more: bool


class FileMatchResponse(BaseModel):
    file_id: int
    path: str
    byte_size: int | None


class FileSearchResponse(SearchResponse):
    items: list[FileMatchResponse]


class SymbolMatchResponse(BaseModel):
    symbol_id: int
    kind: str
    language: IndexLanguage
    name: str
    qualified_name: str
    file_id: int
    path: str
    start_line: int
    end_line: int
    has_errors: bool


class SymbolSearchResponse(SearchResponse):
    items: list[SymbolMatchResponse]


class TextMatchResponse(BaseModel):
    """一個命中行；`snippet` 是 repo 內容（不可信資料），消費端須當純文字處理。

    `snippet_offset`、`match_offset` 以 Unicode 碼點計（不是 UTF-16 或位元組）。
    """

    file_id: int
    path: str
    # 1-based
    line: int
    snippet: str
    snippet_truncated: bool
    # 片段在該行中的字元位移
    snippet_offset: int
    # 第一個命中在片段中的字元位移
    match_offset: int


class TextSearchResponse(SearchResponse):
    items: list[TextMatchResponse]
