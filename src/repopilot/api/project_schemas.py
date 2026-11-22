"""新專案（第五階段）的請求與回應模型。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ProjectConfidence = Literal["high", "medium"]


class ProjectConfig(BaseModel):
    """目標專案的配置；`/projects/init`（5-0a）與 `/projects/create-with-llm`（5-0b）共用。

    `root_path` 與 `detected_at` 只有偵測既有專案時才有值；`init_instructions` 只有 5-0b 會設定。
    `dependencies` 是依賴名稱 → 版本規格（沒有版本規格時為空字串）。
    """

    model_config = ConfigDict(extra="forbid")

    language: str
    framework: str | None = None
    orm: str | None = None
    test_framework: str | None = None
    database: str | None = None
    dependencies: dict[str, str] = Field(default_factory=dict)
    root_path: str | None = None
    detected_at: datetime | None = None
    init_instructions: str | None = None


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
