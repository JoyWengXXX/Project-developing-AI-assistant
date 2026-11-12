from fastapi import APIRouter, Response, status

from repopilot.api.routes.github import GitHubClientDep, SessionDep
from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    FetchResponse,
    GitHubRepositoryCreate,
    RefChangeResponse,
    RepositoryIdPath,
    RepositoryResponse,
)
from repopilot.services import github_repositories as github_repository_service

# 與本機 repo 共用 /api/v1/repositories 前綴，但獨立成檔：避免 routes/repositories.py 與
# routes/github.py（後者已匯入前者的 SettingsDep）互相匯入
router = APIRouter(prefix="/api/v1/repositories", tags=["repositories"])


@router.post(
    "/github",
    response_model=RepositoryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RepositoryResponse, "description": "已註冊過的 GitHub repo"}},
)
def register_github_repository(
    body: GitHubRepositoryCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    client: GitHubClientDep,
) -> RepositoryResponse:
    """以 `owner/name` 註冊 GitHub repo 並同步 clone 到受管目錄；已註冊回 200 且不重新 clone。

    流程：驗證輸入 → 確認 MANAGED_REPOS_DIR → 查 GitHub（有保存 token 時帶上）→ clone → 寫入。
    clone 期間不持有資料庫交易；請求 body 與 token 不會被記錄。
    """
    repository, created = github_repository_service.register_github_repository(
        session, raw_repo=body.repo, settings=settings, client=client
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return RepositoryResponse.model_validate(repository)


@router.post("/{repository_id}/fetch", response_model=FetchResponse)
def fetch_github_repository(
    repository_id: RepositoryIdPath, session: SessionDep, settings: SettingsDep
) -> FetchResponse:
    """更新 GitHub 來源 repo 的遠端 refs（`git fetch --prune origin`）；local 來源回 409。

    只更新受管目錄內的 clone，不建立快照；之後以 `POST /{id}/imports` 匯入新 commit。
    """
    result = github_repository_service.fetch_github_repository(session, repository_id, settings)
    return FetchResponse(
        repository_id=result.repository_id,
        updated_refs=[
            RefChangeResponse(
                name=c.name,
                kind=c.kind,
                change=c.change,
                old_sha=c.old_sha,
                new_sha=c.new_sha,
            )
            for c in result.updated_refs
        ],
        truncated=result.truncated,
    )
