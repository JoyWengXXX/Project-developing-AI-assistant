"""新專案（第五階段）的請求與回應模型。"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from repopilot.api.schemas import UsdAmount

ProjectConfidence = Literal["high", "medium"]


class ProjectConfig(BaseModel):
    """目標專案的配置；`/projects/init`（5-0a）與 `/projects/create-with-llm`（5-0b）共用。

    `root_path` 與 `detected_at` 只有偵測既有專案時才有值；`init_instructions` 只有 5-0b 會設定。
    `dependencies` 是依賴名稱 → 版本規格（沒有版本規格時為空字串）。
    """

    model_config = ConfigDict(extra="forbid")

    language: str = Field(max_length=60)
    framework: str | None = Field(default=None, max_length=60)
    orm: str | None = Field(default=None, max_length=60)
    test_framework: str | None = Field(default=None, max_length=60)
    database: str | None = Field(default=None, max_length=60)
    dependencies: dict[str, str] = Field(default_factory=dict, max_length=200)
    root_path: str | None = Field(default=None, max_length=4096)
    detected_at: datetime | None = None
    init_instructions: str | None = Field(default=None, max_length=4000)


class ProjectInitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class ProjectInitResponse(BaseModel):
    project_config: ProjectConfig
    confidence: ProjectConfidence
    manifests: list[str]
    language_files: dict[str, int]
    directories: list[str]


_INT32_MAX = 2**31 - 1


class ProjectConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ProjectCreateWithLlmRequest(BaseModel):
    """第 N 則 user 訊息回答第 N 個問題；assistant 訊息只是前端的對話紀錄，後端不採用。"""

    model_config = ConfigDict(extra="forbid")

    reference_snapshot_id: int | None = Field(default=None, strict=True, ge=1, le=_INT32_MAX)
    conversation: list[ProjectConversationMessage] = Field(default_factory=list, max_length=30)


class ProjectRecommendationResponse(BaseModel):
    value: str
    reason: str | None
    source: Literal["reference", "llm"]


class ProjectLlmStatusResponse(BaseModel):
    """LLM 只負責推薦；`skipped`／`failed` 時對話照常進行，沒有 LLM 推薦。"""

    state: Literal["used", "skipped", "failed", "not_needed"]
    code: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProjectTemplateResponse(BaseModel):
    name: str
    template_path: str
    files: list[str]


class ProjectPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_project_config: ProjectConfig
    reference_snapshot_id: int = Field(strict=True, ge=1, le=_INT32_MAX)
    project_requirements: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]

    @field_validator("project_requirements")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("The requirements contain an invalid character.")
        return value


class ProjectPlanReferenceResponse(BaseModel):
    """計畫的參考來源。`snapshot_id` 為 null 代表快照已被移除，計畫不可再當作有效依據。"""

    snapshot_id: int | None
    repository_name: str
    commit_sha: str
    available: bool


class ProjectPlanUsageResponse(BaseModel):
    provider: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: UsdAmount
    usage_known: bool


class ProjectPlanResponse(BaseModel):
    id: int
    status: Literal["draft", "confirmed"]
    locked: bool
    created_at: datetime
    confirmed_at: datetime | None
    target_project_config: ProjectConfig
    project_requirements: str
    reference: ProjectPlanReferenceResponse
    plan: dict[str, Any]
    plan_markdown: str
    usage: ProjectPlanUsageResponse


class ProjectCreateWithLlmResponse(BaseModel):
    done: bool
    step: str | None
    question: str | None
    options: list[str]
    collected: dict[str, str | None]
    recommendation: ProjectRecommendationResponse | None
    llm: ProjectLlmStatusResponse
    project_config: ProjectConfig | None
    template: ProjectTemplateResponse | None
