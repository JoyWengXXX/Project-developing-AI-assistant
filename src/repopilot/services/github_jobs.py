"""提交 GitHub 註冊與 fetch 的 job（第二階段切片 5，規格 4.5，D44）。

API 在排入前只做**不需網路**的事（clone、查 GitHub、寫入資料列都由 worker 執行）：

註冊（`POST /api/v1/github/registration-jobs`）：
1. `parse_github_repo`（格式不合法 → 422 `invalid_github_repo`，不回顯輸入）。
2. `MANAGED_REPOS_DIR` 已設定（否則 503 `github_registration_disabled`）。
3. 受管目錄能映射到 worker 容器且是**可寫**映射（否則 503 `worker_path_not_mapped`，與 import-jobs
   對 repo 路徑的檢查同一種設定問題）。
4. 已註冊（不分大小寫）→ 不建立 job，200＋`repository_id`。
5. 其餘建立 `github_register` job：`dedupe_key = register:{lower(owner)}/{lower(name)}`（只在
   `queued`／`running` 時唯一），`repository_id` 為 NULL，成功後 `result.repository_id`。
   失敗或取消後再提交會建立**新的** job（不像匯入重新排入舊 job：註冊沒有「接手殘留資料」的語意）。

fetch（`POST /api/v1/repositories/{id}/fetch-jobs`）：僅 GitHub 來源（local → 409
`repository_not_github`）；D28 的主機端路徑重驗與映射檢查；`dedupe_key = fetch:{repo_key}`，fetch 是
會重複發生的事件，所以結束後可再提交。

.NET 對照：像兩個冪等的 `POST` 命令處理器——先查「已經有了」或「已經有人在做」，最後才寫入。
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job
from repopilot.errors import (
    GitHubRegistrationDisabledError,
    PathNotMappedError,
    RepositoryNotGitHubError,
    WorkerPathNotMappedError,
)
from repopilot.github.repo_ref import parse_github_repo
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.path_mapping import map_host_path
from repopilot.services import jobs as job_service
from repopilot.services.github_repositories import find_github_repository
from repopilot.services.repositories import (
    get_repository,
    github_repo_key,
    repository_key,
    resolve_registered_path,
)

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.github_jobs")


@dataclass(frozen=True)
class RegistrationSubmission:
    job: Job | None
    repository_id: int | None
    reused: bool  # True＝已註冊，沒有新的工作（API 回 200）


def registration_dedupe_key(owner: str, name: str) -> str:
    return f"register:{owner.lower()}/{name.lower()}"


def fetch_dedupe_key(repo_key: str) -> str:
    return f"fetch:{repo_key}"


def _require_writable_mapping(host_path: str, settings: Settings) -> None:
    """路徑能映射到 worker 容器，而且該映射是可寫的（clone／fetch 需要寫入）。"""
    try:
        mapped = map_host_path(host_path, settings.worker_path_mappings)
    except PathNotMappedError:
        raise WorkerPathNotMappedError(
            "The managed directory cannot be mapped into the worker container; "
            "check WORKER_PATH_MAPPINGS and the MANAGED_REPOS_DIR mount."
        ) from None
    if mapped.mapping.mode != "rw":
        raise WorkerPathNotMappedError(
            "The managed directory is mapped read-only in WORKER_PATH_MAPPINGS; "
            "the worker needs write access to clone."
        )


def submit_registration_job(
    session: Session,
    raw_repo: str,
    settings: Settings,
    *,
    request_id: str | None = None,
) -> RegistrationSubmission:
    ref = parse_github_repo(raw_repo)
    managed_dir = settings.managed_repos_dir
    if managed_dir is None:
        raise GitHubRegistrationDisabledError(
            "GitHub registration is disabled: set MANAGED_REPOS_DIR to an absolute path."
        )
    _require_writable_mapping(str(managed_dir), settings)

    existing = find_github_repository(session, ref.owner, ref.name)
    session.commit()  # 結束讀取交易
    if existing is not None:
        return RegistrationSubmission(None, existing.id, reused=True)

    created = job_service.create_job(
        session,
        kind="github_register",
        dedupe_key=registration_dedupe_key(ref.owner, ref.name),
        settings=settings,
        repo_key=github_repo_key(ref.owner, ref.name),
        # 只存安全欄位：使用者輸入經 parse_github_repo 驗證後的 owner／name（不是原始字串）
        params={"github_repo": f"{ref.owner}/{ref.name}"},
        request_id=request_id,
    )
    return RegistrationSubmission(created.job, None, reused=False)


def submit_fetch_job(
    session: Session,
    repository_id: int,
    settings: Settings,
    *,
    request_id: str | None = None,
) -> Job:
    repository = get_repository(session, repository_id)  # 不存在 → 404
    if repository.source_type != "github":
        raise RepositoryNotGitHubError("Only GitHub repositories can be fetched.")
    if settings.managed_repos_dir is None:
        raise GitHubRegistrationDisabledError(
            "GitHub support is disabled: set MANAGED_REPOS_DIR to an absolute path."
        )
    resolve_registered_path(repository, settings)  # D28：主機端的完整重驗（不合法 → 422）
    host_path = repository.canonical_local_path
    key = repository_key(repository)
    session.commit()  # 結束讀取交易
    _require_writable_mapping(host_path, settings)

    created = job_service.create_job(
        session,
        kind="github_fetch",
        dedupe_key=fetch_dedupe_key(key),
        settings=settings,
        repository_id=repository_id,
        repo_key=key,
        request_id=request_id,
    )
    return created.job
