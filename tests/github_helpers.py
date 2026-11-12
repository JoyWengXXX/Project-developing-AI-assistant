"""GitHub 相關測試的共用輔助：假 GitHub 端點與日誌擷取。

測試 token 一律使用明顯的假值，不得使用真實格式的 token。
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

from repopilot.github.client import GitHubClient
from repopilot.logging_config import JsonFormatter

FAKE_TOKEN = "github_pat_TESTONLY_0123456789abcdefghijklmnop"
FAKE_CLASSIC_TOKEN = "ghp_TESTONLY0123456789abcdefghijklmnopqr"
# 假 GitHub 在失敗回應放入此字串，用來確認「GitHub 原始回應 body」不會外洩
GITHUB_BODY_MARKER = "UPSTREAM-BODY-MARKER"


@dataclass(frozen=True)
class FakeRepo:
    full_name: str
    private: bool
    default_branch: str


class FakeGitHub:
    """以 `httpx.MockTransport` 模擬 GitHub API（`GET /user`、`GET /repos/{owner}/{name}`）。

    記錄收到的請求，可切換回應或丟出例外。`/repos/...` 依 `repos` 登錄的 repo 回應（標準大小寫由
    登錄值決定，查詢不分大小寫）；`private=True` 的 repo 在沒有帶 `accepted_token` 時回 404，
    與 GitHub 對「看不到的私有 repo」的行為一致。
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.login = "octocat"
        self.error: Exception | None = None
        self.raw_body: bytes | None = None
        self.repos: dict[str, FakeRepo] = {}
        # 非 None 時，帶了其他 token 的 /repos 請求回 401；private repo 須帶此 token 才看得到
        self.accepted_token: str | None = None
        # 非 None 時，所有 /repos 請求回此狀態（取代 repos 查詢）
        self.repo_status: int | None = None

    def add_repo(
        self, full_name: str, *, private: bool = False, default_branch: str = "main"
    ) -> None:
        self.repos[full_name.lower()] = FakeRepo(full_name, private, default_branch)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if request.url.path.startswith("/repos/"):
            return self._handle_repo(request)
        if self.raw_body is not None:
            return httpx.Response(self.status, content=self.raw_body)
        if self.status == 200:
            return httpx.Response(200, json={"login": self.login, "id": 1})
        return httpx.Response(self.status, json={"message": GITHUB_BODY_MARKER})

    def _handle_repo(self, request: httpx.Request) -> httpx.Response:
        if self.repo_status is not None:
            return httpx.Response(self.repo_status, json={"message": GITHUB_BODY_MARKER})
        if self.raw_body is not None:
            return httpx.Response(200, content=self.raw_body)
        authorization = request.headers.get("Authorization")
        if (
            authorization is not None
            and self.accepted_token is not None
            and authorization != f"Bearer {self.accepted_token}"
        ):
            return httpx.Response(401, json={"message": GITHUB_BODY_MARKER})
        key = request.url.path.removeprefix("/repos/").lower()
        repo = self.repos.get(key)
        if repo is None or (repo.private and authorization is None):
            return httpx.Response(404, json={"message": GITHUB_BODY_MARKER})
        owner, _, name = repo.full_name.partition("/")
        return httpx.Response(
            200,
            json={
                "id": 1,
                "name": name,
                "full_name": repo.full_name,
                "private": repo.private,
                "owner": {"login": owner},
                "default_branch": repo.default_branch,
            },
        )

    def client(self) -> GitHubClient:
        return GitHubClient(
            base_url="https://api.github.test",
            timeout_seconds=1,
            transport=httpx.MockTransport(self.handle),
        )


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class CapturedLogs:
    def __init__(self, handler: _ListHandler) -> None:
        self._handler = handler

    def text(self) -> str:
        """所有紀錄的訊息、extra 欄位與例外文字（以應用程式的 JSON 格式輸出）。"""
        formatter = JsonFormatter()
        return "\n".join(formatter.format(r) for r in self._handler.records)

    def __len__(self) -> int:
        return len(self._handler.records)


@contextmanager
def capture_logs() -> Iterator[CapturedLogs]:
    """擷取 root、repopilot、httpx、httpcore 的所有層級日誌。

    `repopilot` logger 的 propagate 為 False，pytest 的 `caplog` 收不到，
    因此直接在各 logger 掛 handler。進入時把層級調到 DEBUG（以最嚴格的條件驗證不外洩），結束時還原。
    須在 `create_app()` 之後進入，因為 `configure_logging` 會重設 `repopilot` 的 handler。
    """
    handler = _ListHandler()
    names = ("", "repopilot", "httpx", "httpcore")
    # httpx／httpcore 會 propagate 到 root，只需在 root 與（不 propagate 的）repopilot 掛 handler
    handler_names = ("", "repopilot")
    previous = {name: logging.getLogger(name).level for name in names}
    for name in names:
        logging.getLogger(name).setLevel(logging.DEBUG)
    for name in handler_names:
        logging.getLogger(name).addHandler(handler)
    try:
        yield CapturedLogs(handler)
    finally:
        for name in handler_names:
            logging.getLogger(name).removeHandler(handler)
        for name in names:
            logging.getLogger(name).setLevel(previous[name])
