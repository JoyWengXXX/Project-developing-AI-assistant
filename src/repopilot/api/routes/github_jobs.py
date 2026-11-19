from fastapi import APIRouter, Response, status

from repopilot.api.job_responses import to_job_response
from repopilot.api.routes.github import SessionDep
from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import GitHubRepositoryCreate, JobSubmitResponse, RepositoryIdPath
from repopilot.request_context import request_id_var
from repopilot.services import github_jobs as github_job_service

# 兩個端點的前綴不同（/github 與 /repositories），所以路徑寫完整、不設 prefix
router = APIRouter(tags=["github"])


@router.post(
    "/api/v1/github/registration-jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {
            "model": JobSubmitResponse,
            "description": "repo 已註冊（不分大小寫）：不建立 job，reused=true 並回 repository_id",
        },
        422: {"description": "repo 格式不合法（invalid_github_repo）"},
        503: {
            "description": "MANAGED_REPOS_DIR 未設定（github_registration_disabled）"
            "或受管目錄無法映射到 worker（worker_path_not_mapped）"
        },
    },
)
def create_registration_job(
    body: GitHubRepositoryCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> JobSubmitResponse:
    """排入 GitHub repo 的註冊並立即回應（202＋`Location`）；查 GitHub、clone、寫入由 worker 執行。

    API 只做不需網路的驗證（格式、受管目錄與路徑映射）。同一個 owner/name（不分大小寫）已有進行中的
    job 時回既有 job；已註冊時回 200 與 `repository_id`。job 成功後 `GET /api/v1/jobs/{id}` 的
    `result.repository_id` 是新資料列的 id。GitHub 相關的錯誤碼
    （`github_repo_not_found_or_private`、`github_token_invalid`、`github_clone_failed`…）
    成為 job 的 `error.code`，不再由 HTTP 回應傳回。
    取代已棄用的 `POST /api/v1/repositories/github`（同步 clone）。
    """
    submission = github_job_service.submit_registration_job(
        session, body.repo, settings, request_id=request_id_var.get()
    )
    if submission.reused:
        response.status_code = status.HTTP_200_OK
    elif submission.job is not None:
        response.headers["Location"] = f"/api/v1/jobs/{submission.job.id}"
    return JobSubmitResponse(
        job=to_job_response(submission.job) if submission.job is not None else None,
        snapshot_id=None,
        repository_id=submission.repository_id,
        reused=submission.reused,
    )


@router.post(
    "/api/v1/repositories/{repository_id}/fetch-jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "repo 不存在（repository_not_found）"},
        409: {"description": "不是 GitHub 來源（repository_not_github）"},
        422: {"description": "repo 路徑驗證失敗（invalid_repository_path）"},
        503: {
            "description": "MANAGED_REPOS_DIR 未設定（github_registration_disabled）"
            "或 repo 路徑無法映射到 worker（worker_path_not_mapped）"
        },
    },
)
def create_fetch_job(
    repository_id: RepositoryIdPath,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> JobSubmitResponse:
    """排入 GitHub 來源 repo 的 `git fetch --prune origin` 並立即回應（202＋`Location`）。

    同一 repo 已有進行中（queued／running）的 fetch 時回既有 job；結束後可再提交
    （fetch 會重複發生）。只更新受管目錄內的 clone、不建立快照；`job.result.updated_refs` 的
    `name` 可直接作為之後匯入的 revision（`origin/main`）。取代已棄用的同步
    `POST /repositories/{id}/fetch`。
    """
    job = github_job_service.submit_fetch_job(
        session, repository_id, settings, request_id=request_id_var.get()
    )
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return JobSubmitResponse(
        job=to_job_response(job), snapshot_id=None, repository_id=repository_id, reused=False
    )
