import logging
from dataclasses import dataclass
from typing import Any

import httpx

from repopilot.errors import (
    GitHubApiError,
    GitHubRepoNotFoundError,
    GitHubTimeoutError,
    GitHubTokenInvalidError,
    GitHubUnreachableError,
)
from repopilot.github.repo_ref import is_valid_owner_and_name
from repopilot.logging_config import APP_LOGGER_NAME

logger = logging.getLogger(f"{APP_LOGGER_NAME}.github")

# GitHub REST API 版本標頭；值取自官方文件目前列出的版本（2026-11-09 查證）
GITHUB_API_VERSION = "2026-03-10"
# 與 github_credentials.login 欄位長度一致；GitHub 帳號名稱實際上限為 39
_MAX_LOGIN_LENGTH = 100
# 與 repositories.github_default_branch 欄位長度一致
_MAX_BRANCH_LENGTH = 255


@dataclass(frozen=True)
class GitHubUser:
    login: str


@dataclass(frozen=True)
class GitHubRepo:
    """`GET /repos/{owner}/{name}` 的結果；owner／name 為 GitHub 的標準大小寫。"""

    owner: str
    name: str
    is_private: bool
    default_branch: str


class GitHubClient:
    """呼叫 GitHub REST API 的最小用戶端：`GET /user`（驗證 token）與 `GET /repos/{owner}/{name}`。

    安全約定：token 只放在請求標頭；任何例外訊息、日誌都不含 token 或 GitHub 的原始回應內容，
    只記錄狀態碼與例外型別。`transport` 僅供測試注入（`httpx.MockTransport`）。

    .NET 對照：像是包了 `HttpClient` 的 typed client；`transport` 相當於可替換的
    `HttpMessageHandler`。
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    def get_authenticated_user(self, token: str) -> GitHubUser:
        """以 token 呼叫 `GET /user`；200 回傳使用者，其他情況轉成領域例外。"""
        response = self._get("/user", token)
        status = response.status_code
        if status == 401:
            raise GitHubTokenInvalidError(
                "GitHub rejected the token (invalid, expired or revoked)."
            )
        if status != 200:
            logger.warning("github_unexpected_status", extra={"github_status": status})
            if status in (403, 429):
                raise GitHubApiError(
                    "GitHub denied the request (forbidden or rate limited).", status=status
                )
            raise GitHubApiError(f"GitHub returned an unexpected status ({status}).", status=status)
        return _parse_user(response)

    def get_repository(self, owner: str, name: str, token: str | None) -> GitHubRepo:
        """以 `GET /repos/{owner}/{name}` 取得標準大小寫的名稱、private 與預設分支。

        `owner`／`name` 須已通過 `parse_github_repo` 驗證（只含安全字元）。`token` 可為 None（匿名，
        僅公開 repo）。404 同時代表「不存在」與「看不到的私有 repo」，訊息依有無 token 區分。
        """
        response = self._get(f"/repos/{owner}/{name}", token)
        status = response.status_code
        if status == 404:
            if token is None:
                raise GitHubRepoNotFoundError(
                    "The repository was not found. If it is private, save a GitHub token "
                    "that can read it and try again."
                )
            raise GitHubRepoNotFoundError(
                "The repository was not found with the saved token. It may not exist, or the "
                "token may not cover it (repository access, or organization approval)."
            )
        if status == 401:
            raise GitHubTokenInvalidError(
                "GitHub rejected the token (invalid, expired or revoked)."
            )
        if status != 200:
            logger.warning("github_unexpected_status", extra={"github_status": status})
            if status in (403, 429):
                raise GitHubApiError(
                    "GitHub denied the request (forbidden or rate limited).", status=status
                )
            raise GitHubApiError(f"GitHub returned an unexpected status ({status}).", status=status)
        return _parse_repository(response, owner, name)

    def _get(self, path: str, token: str | None) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            # 每次呼叫建立短命的 Client；不跟隨重新導向（httpx 預設），避免標頭被帶往其他位置
            with httpx.Client(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                return client.get(path, headers=headers)
        except httpx.TimeoutException as exc:
            logger.warning("github_request_failed", extra={"error_type": type(exc).__name__})
            raise GitHubTimeoutError("Timed out while calling the GitHub API.") from None
        except httpx.HTTPError as exc:
            logger.warning("github_request_failed", extra={"error_type": type(exc).__name__})
            raise GitHubUnreachableError("Could not reach the GitHub API.") from None


def _parse_user(response: httpx.Response) -> GitHubUser:
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    login = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not login or len(login) > _MAX_LOGIN_LENGTH:
        logger.warning("github_unexpected_payload")
        raise GitHubApiError("GitHub returned an unexpected response.")
    return GitHubUser(login=login)


def _parse_repository(response: httpx.Response, owner: str, name: str) -> GitHubRepo:
    """解析並二次驗證 repo 回應：標準名稱會成為目錄名稱，所以不信任回應內容。

    依官方文件與實測（2026-11-09，octocat/Hello-World）使用 `full_name`（標準大小寫的
    `owner/name`）、`private`（bool）、`default_branch`。`full_name` 必須與請求的名稱只差
    大小寫（依官方文件，改名的 repo 會回 301 重新導向；本用戶端不跟隨，會被視為非預期狀態）。
    """
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        logger.warning("github_unexpected_payload")
        raise GitHubApiError("GitHub returned an unexpected response.")
    full_name = payload.get("full_name")
    is_private = payload.get("private")
    branch = payload.get("default_branch")
    canonical_owner, _, canonical_name = (
        full_name.partition("/") if isinstance(full_name, str) else ("", "", "")
    )
    valid = (
        is_valid_owner_and_name(canonical_owner, canonical_name)
        and f"{canonical_owner}/{canonical_name}".lower() == f"{owner}/{name}".lower()
        and isinstance(is_private, bool)
        and isinstance(branch, str)
        and 0 < len(branch) <= _MAX_BRANCH_LENGTH
    )
    if not valid:
        logger.warning("github_unexpected_payload")
        raise GitHubApiError("GitHub returned an unexpected response.")
    return GitHubRepo(
        owner=canonical_owner,
        name=canonical_name,
        is_private=bool(is_private),
        default_branch=str(branch),
    )
