import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RepoPilot"
    log_level: LogLevel = "INFO"
    # 預設值對應 docker-compose.yml 的本機開發資料庫（假密碼，僅限本機）
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://repopilot:repopilot_dev@127.0.0.1:5432/repopilot"
    )
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    # 連線池預設值與 SQLAlchemy 相同（5＋10、等待 30 秒），尚未經實測調整
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # 允許註冊的本機目錄；環境變數以 os.pathsep 分隔（Windows 為 ;）。預設為空＝拒絕所有註冊
    repository_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    git_timeout_seconds: float = Field(default=30.0, gt=0, le=600)

    @field_validator("repository_roots", mode="before")
    @classmethod
    def _split_repository_roots(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(os.pathsep) if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
