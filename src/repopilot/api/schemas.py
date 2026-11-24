from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from fastapi import Path, Query
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from repopilot.indexing.file_kinds import FileKind
from repopilot.qa.pricing import MODEL_TIERS
from repopilot.qa.question_rules import (
    MAX_QUESTION_CHARS,
    MAX_SNAPSHOTS,
    MAX_TOKENS_CEILING,
    retrieval_query,
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
    "snapshot_import",
    "snapshot_index",
    "github_register",
    "github_fetch",
    "snapshot_retrieval",
    "qa_question",
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
    "retrieving",
    "generating",
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
    # 檢索索引工作（snapshot_retrieval）：此時 processed_files／total_files 是片段數；
    # 已計算的向量數與估計剩餘秒數（估計值，資料不足時為 null）。其他種類的工作恆為 null
    vectors_written: int | None = None
    estimated_remaining_seconds: int | None = None
    # 本次執行以相同內容的既有向量複製完成的片段數（跨快照、跨 repo 重用）；其他種類為 null
    chunks_reused: int | None = None


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


class ReferenceEmbeddingResponse(BaseModel):
    """設定參考版本後排入檢索 embedding 的結果（issue #25）。

    - `status=job`：`job` 是新排入或沿用的進行中 job，`device` 是它實際使用的裝置
      （預設 `gpu`；送出時 GPU 不可用則為 `cpu` 且 `device_fallback=true`）。
    - `status=completed`：檢索索引已完成，沒有 job。
    - `status=skipped`：沒有排入，`reason` 為 `embedding_not_configured` 或
      `structure_index_pending`（等待結構索引，完成後自動補送）；參考版本仍已設定。
    """

    status: Literal["job", "completed", "skipped"]
    message: str
    reason: Literal["embedding_not_configured", "structure_index_pending"] | None
    job: JobResponse | None
    device: Literal["cpu", "gpu"] | None
    device_fallback: bool


class ReferenceSetResponse(ReferenceResponse):
    """`PUT /repositories/{id}/reference` 的回應：參考版本＋ embedding 排入結果。"""

    embedding: ReferenceEmbeddingResponse


class RetrievalIndexResponse(BaseModel):
    snapshot_id: int
    repository_id: int
    status: Literal["none", "building", "failed", "completed"]
    index_id: int | None
    profile_hash: str
    chunk_count: int | None
    segment_count: int | None
    job: JobResponse | None


class RetrievalJobRequest(BaseModel):
    """建立檢索索引工作的選用請求本文；沒有本文等同 `{"device": "cpu"}`。"""

    model_config = ConfigDict(extra="forbid")

    device: Literal["cpu", "gpu"] = "cpu"


class EmbeddingDeviceState(BaseModel):
    configured: bool
    available: bool


class EmbeddingDevicesResponse(BaseModel):
    cpu: EmbeddingDeviceState
    gpu: EmbeddingDeviceState


class RetrievalSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_ids: list[Annotated[int, Field(strict=True, ge=1, le=_INT32_MAX)]] = Field(
        min_length=1, max_length=20
    )
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
    mode: Literal["keyword", "semantic", "hybrid"]
    limit: int = Field(default=10, ge=1, le=50)
    file_kinds: list[FileKind] | None = Field(default=None, description="篩選檔案種類")
    path_prefix: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="路徑前綴（區分大小寫，不是萬用字元）",
    )

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


# 以定點格式輸出，避免 Decimal 的 "0E-8" 之類指數字串
UsdAmount = Annotated[
    Decimal, PlainSerializer(lambda value: format(value, "f"), return_type=str, when_used="json")
]


class QaUsageTotalsResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    # 美元，JSON 以字串表示（Decimal，避免浮點誤差）
    cost_usd: UsdAmount


class QaUsageResponse(BaseModel):
    today: QaUsageTotalsResponse
    cumulative: QaUsageTotalsResponse
    # 今日視窗 [start, end)，UTC；start 已考慮設定時區的午夜
    window_start: datetime
    window_end: datetime
    last_reset_at: datetime | None
    # 沒有設定上限時為 null
    remaining_tokens: int | None
    remaining_cost_usd: UsdAmount | None


QaTokenLimit = Annotated[int, Field(strict=True, ge=1, le=_INT32_MAX)]
QaDailyTokenLimit = Annotated[int, Field(strict=True, ge=1, le=_INT64_MAX)]
# 美元；Numeric(18, 8) 能保存的範圍，且必須為正（不限制請送 null）
QaCostLimit = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=8)]
# 每百萬 token 的單價（美元）
QaPriceNumber = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)]


class QaModelPriceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_usd_per_million: QaPriceNumber
    output_usd_per_million: QaPriceNumber


class QaSettingsUpdate(BaseModel):
    """部分更新：沒送的欄位不變；`model` 與三個額度明確送 `null` 代表清除（未選／不限制）。"""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    per_request_tokens: QaTokenLimit | None = None
    daily_tokens: QaDailyTokenLimit | None = None
    daily_cost_usd: QaCostLimit | None = None
    # 只需送要改的模型；沒送的模型沿用舊單價
    price_table: dict[str, QaModelPriceInput] | None = None
    # 單價的讀取日期；更新單價但沒送時用今天（設定的時區）
    price_table_as_of: date | None = None

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str | None) -> str | None:
        if value is not None and value not in MODEL_TIERS:
            raise ValueError("Unknown model.")
        return value

    @field_validator("price_table")
    @classmethod
    def _known_price_models(
        cls, value: dict[str, QaModelPriceInput] | None
    ) -> dict[str, QaModelPriceInput] | None:
        if value is not None and (not value or not set(value) <= set(MODEL_TIERS)):
            raise ValueError("The price table must be non-empty and list only known models.")
        return value

    @model_validator(mode="after")
    def _price_fields_are_consistent(self) -> Self:
        for name in ("price_table", "price_table_as_of"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null.")
        if self.price_table_as_of is not None and self.price_table is None:
            raise ValueError("price_table_as_of requires price_table.")
        return self


class QaProviderKeySubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _valid_api_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if not key or any(ch.isspace() or not ch.isprintable() for ch in key):
            raise ValueError("The API key is not valid.")
        return SecretStr(key)


class QaModelPriceResponse(BaseModel):
    input_usd_per_million: UsdAmount
    output_usd_per_million: UsdAmount


class QaModelOptionResponse(BaseModel):
    id: str
    tier: Literal["low", "mid", "high"]
    price: QaModelPriceResponse
    # 假設值（見 estimate_basis），不是實測
    estimated_cost_per_question_usd: UsdAmount
    price_note: str | None


class QaLimitsResponse(BaseModel):
    per_request_tokens: int | None
    daily_tokens: int | None
    daily_cost_usd: UsdAmount | None


class QaEstimateBasisResponse(BaseModel):
    input_tokens: int
    output_tokens: int


class QaSettingsResponse(BaseModel):
    provider: Literal["anthropic"]
    # 未選擇前為 null（沒有預設模型）
    model: str | None
    # 永不回傳 key 或其片段；已保存但無法解密（TOKEN_ENCRYPTION_KEY 遺失或更換）時為 false 並附警告
    provider_key_configured: bool
    provider_key_warning: str | None
    # 已選模型且 API key 可用；逐 repo 的同意另由 qa-consent 端點回報
    can_ask: bool
    limits: QaLimitsResponse
    price_table_as_of: date
    estimate_basis: QaEstimateBasisResponse
    models: list[QaModelOptionResponse]
    usage: QaUsageResponse
    # 尚未儲存過任何設定時為 null
    updated_at: datetime | None


class QaConsentResponse(BaseModel):
    repository_id: int
    consented: bool
    consented_at: datetime | None


class QaConsentItem(BaseModel):
    repository_id: int
    consented_at: datetime


class QaConsentListResponse(BaseModel):
    items: list[QaConsentItem]


# --- 問答（第四階段 4-3a） ---
QaQuestionStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
QaAnswerStatus = Literal["answered", "insufficient_evidence"]
QaStatementKind = Literal["fact", "inference", "open_question"]
QaContextSkipReason = Literal["count_cap", "budget_exhausted", "empty_text"]
QaQuestionIdPath = Annotated[int, Path(ge=1, le=_INT64_MAX)]


class QaQuestionCreate(BaseModel):
    """提問。`max_tokens` 是這題的輸出上限（含思考）；省略用預設，且超過單次額度時 409。
    `file_kinds` 與 `path_prefix` 篩選檢索範圍；規則與 `/retrieval/search` 相同。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_ids: list[Annotated[int, Field(strict=True, ge=1, le=_INT32_MAX)]] = Field(
        min_length=1, max_length=MAX_SNAPSHOTS
    )
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
    ]
    max_tokens: int | None = Field(default=None, strict=True, ge=1, le=MAX_TOKENS_CEILING)
    file_kinds: list[FileKind] | None = Field(default=None, description="篩選檔案種類")
    path_prefix: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="路徑前綴（區分大小寫，不是萬用字元）",
    )

    @field_validator("snapshot_ids")
    @classmethod
    def _unique_snapshots(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("Snapshot IDs must be unique.")
        return value

    @field_validator("question")
    @classmethod
    def _valid_question(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("The question contains an invalid character.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("The question is not valid UTF-8.") from None
        if not retrieval_query(value):
            raise ValueError("The question must contain at least one word.")
        return value


class QaEstimateResponse(BaseModel):
    """提問時的上限估計（假設值，不是實測）；實際用量以供應商回報為準。"""

    input_tokens_upper_bound: int
    max_output_tokens: int
    total_tokens_upper_bound: int
    cost_usd: UsdAmount


class QaQuestionSubmitResponse(BaseModel):
    id: int
    status: QaQuestionStatus
    model: str
    job: JobResponse
    estimate: QaEstimateResponse
    status_url: str


class QaCitationResponse(BaseModel):
    """已驗證的引用：座標取自送出的片段，行號為 1-based 含頭尾。"""

    chunk_code: str
    repository_id: int
    repository: str
    snapshot_id: int
    commit_sha: str
    path: str
    start_line: int
    end_line: int


class QaStatementResponse(BaseModel):
    position: int
    kind: QaStatementKind
    text: str
    citations: list[QaCitationResponse]


class QaClaimedCitationResponse(BaseModel):
    """模型宣稱的引用（未驗證資料）；`valid`／`reasons` 是系統驗證的結果。"""

    chunk: str
    start_line: int
    end_line: int
    valid: bool
    reasons: list[str]


class QaRejectionReasonResponse(BaseModel):
    reason: str
    # 引用層級的原因帶引用在該陳述中的 0 起算位置；陳述層級的原因為 null
    citation_index: int | None


class QaRejectedStatementResponse(BaseModel):
    position: int
    kind: QaStatementKind
    text: str
    reasons: list[QaRejectionReasonResponse]
    claimed_citations: list[QaClaimedCitationResponse]


class QaContextChunkResponse(BaseModel):
    """檢索到的片段與送出當下的來源位置；`code` 為 null 表示沒有送出（`skipped_reason`）。"""

    rank: int
    code: str | None
    skipped_reason: QaContextSkipReason | None
    repository_id: int
    repository: str
    snapshot_id: int
    commit_sha: str
    path: str
    start_line: int
    end_line: int
    chunk_id: int
    truncated: bool


class QaUsageDetailResponse(BaseModel):
    # 供應商回報的實際用量（輸出含思考）；沒有結果前為 null
    input_tokens: int | None
    output_tokens: int | None
    # true＝供應商沒有回報，帳本沿用預留量
    usage_unknown: bool


class QaCostResponse(BaseModel):
    # 美元（字串）；預估是預留的值（worker 預留前是提問時的上限估計）
    estimated_usd: UsdAmount
    estimated_input_tokens: int
    # 預留輸入 token 的來源；預留之前為 null
    estimation_source: Literal["provider_count", "local_estimate"] | None
    actual_usd: UsdAmount | None
    # true＝實際用量超過預留；仍以實際用量計入每日累計
    exceeded_reservation: bool


class QaQuestionListItem(BaseModel):
    id: int
    status: QaQuestionStatus
    answer_status: QaAnswerStatus | None
    question: str
    snapshot_ids: list[int]
    # 當次檢索範圍（CL-01）；null 表示沒有篩選
    file_kinds: list[FileKind] | None
    path_prefix: str | None
    provider: str
    model: str
    error_code: str | None
    # 實際呼叫供應商的次數（含重試）；usage／cost 是各次嘗試的累計
    attempts: int
    usage: QaUsageDetailResponse
    cost: QaCostResponse
    job_id: int | None
    created_at: datetime
    finished_at: datetime | None


class QaQuestionListResponse(PageResponse):
    items: list[QaQuestionListItem]


class QaQuestionResponse(QaQuestionListItem):
    prompt_version: str
    max_tokens: int
    error: JobErrorResponse | None
    provider_request_id: str | None
    duration_ms: int | None
    started_at: datetime | None
    # 驗證後的答案：只含通過驗證的陳述；失敗或證據不足時為空
    statements: list[QaStatementResponse]
    rejected_statements: list[QaRejectedStatementResponse]
    context_chunks: list[QaContextChunkResponse]


class QaQuestionsDeletedResponse(BaseModel):
    deleted: int
    # 排隊或執行中、因此保留的問答數
    skipped_active: int


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
