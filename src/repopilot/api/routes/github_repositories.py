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
    deprecated=True,
    responses={200: {"model": RepositoryResponse, "description": "已註冊過的 GitHub repo"}},
)
def register_github_repository(
    body: GitHubRepositoryCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    client: GitHubClientDep,
) -> RepositoryResponse:
    """**已棄用（deprecated）**：以 `owner/name` 註冊 GitHub repo 並同步 clone 到受管目錄。

    已註冊回 200 且不重新 clone。流程：驗證輸入 → 確認 MANAGED_REPOS_DIR → 查 GitHub（有保存
    token 時帶上）→ 取 repo 獨占鎖 → clone → 寫入；請求會一直占用到 clone 結束
    （大型 repo 可能數分鐘）。clone 期間不持有資料庫交易；請求 body 與 token 不會被記錄。
    長時間 clone 請改用 `POST /api/v1/github/registration-jobs`
    （立即回 202 與 job，由 worker 背景執行）；本端點保留原契約供腳本與既有用戶端使用，
    並與 worker 共用同一把 repo 鎖（另一邊正在 clone 時會等待，逾時回 409 `repository_busy`）。
    """
    repository, created = github_repository_service.register_github_repository(
        session, raw_repo=body.repo, settings=settings, client=client
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return RepositoryResponse.model_validate(repository)


@router.post("/{repository_id}/fetch", response_model=FetchResponse, deprecated=True)
def fetch_github_repository(
    repository_id: RepositoryIdPath, session: SessionDep, settings: SettingsDep
) -> FetchResponse:
    """**已棄用（deprecated）**：同步更新 GitHub 來源 repo 的遠端 refs（`git fetch --prune`）。

    local 來源回 409。只更新受管目錄內的 clone，不建立快照；之後以 `POST /{id}/imports` 匯入新
    commit。請求會一直占用到 fetch 結束；請改用 `POST /api/v1/repositories/{id}/fetch-jobs`
    （立即回 202 與 job，由 worker 背景執行）。本端點保留原契約，並與 worker 共用同一把 repo 鎖。
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
