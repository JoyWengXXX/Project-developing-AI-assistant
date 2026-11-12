from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import GitHubAuthResponse, GitHubTokenSubmit
from repopilot.db.session import get_session
from repopilot.github.client import GitHubClient
from repopilot.services import github_credentials as credential_service

router = APIRouter(prefix="/api/v1/github", tags=["github"])

SessionDep = Annotated[Session, Depends(get_session)]


def get_github_client(settings: SettingsDep) -> GitHubClient:
    """FastAPI dependency；測試以 `app.dependency_overrides` 換成帶 MockTransport 的用戶端。"""
    return GitHubClient(
        base_url=settings.github_api_base_url, timeout_seconds=settings.github_api_timeout_seconds
    )


GitHubClientDep = Annotated[GitHubClient, Depends(get_github_client)]


def _to_response(status_: credential_service.CredentialStatus) -> GitHubAuthResponse:
    return GitHubAuthResponse(
        authenticated=status_.authenticated,
        login=status_.login,
        token_type=status_.token_type,
        warning=status_.warning,
    )


@router.put("/token", response_model=GitHubAuthResponse)
def submit_token(
    body: GitHubTokenSubmit,
    session: SessionDep,
    settings: SettingsDep,
    client: GitHubClientDep,
) -> GitHubAuthResponse:
    """以 `GET /user` 驗證 token，成功才加密保存並覆蓋舊 token；請求 body 不會被記錄。"""
    return _to_response(
        credential_service.connect_token(
            session, token=body.token, settings=settings, client=client
        )
    )


@router.get("/auth", response_model=GitHubAuthResponse)
def get_auth_status(session: SessionDep, settings: SettingsDep) -> GitHubAuthResponse:
    """顯示已保存 token 的狀態（保存當下的資料，不呼叫 GitHub，永不回傳 token）。"""
    return _to_response(credential_service.get_status(session, settings))


@router.delete("/auth", status_code=status.HTTP_204_NO_CONTENT)
def delete_auth(session: SessionDep) -> Response:
    """刪除本機保存的 token（冪等）；不會在 GitHub 端撤銷 token。"""
    credential_service.delete_credential(session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
