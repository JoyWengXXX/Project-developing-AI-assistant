from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

RepositoryName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


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


class RepositoryListResponse(BaseModel):
    items: list[RepositoryResponse]
