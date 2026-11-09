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
