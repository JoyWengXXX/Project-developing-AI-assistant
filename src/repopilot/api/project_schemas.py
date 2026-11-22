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
