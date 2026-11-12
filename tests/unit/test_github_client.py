import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from repopilot.errors import (
    GitHubApiError,
    GitHubRepoNotFoundError,
    GitHubTimeoutError,
    GitHubTokenInvalidError,
    GitHubUnreachableError,
    RepoPilotError,
)
from repopilot.github.client import GITHUB_API_VERSION, GitHubClient
from tests.github_helpers import FAKE_TOKEN, GITHUB_BODY_MARKER, FakeGitHub, capture_logs


def test_get_authenticated_user_returns_login_and_sends_expected_headers() -> None:
    fake = FakeGitHub()

    user = fake.client().get_authenticated_user(FAKE_TOKEN)

    assert user.login == "octocat"
    request = fake.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.github.test/user"
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION == "2026-03-10"


def test_base_url_with_path_prefix_and_trailing_slash_is_respected() -> None:
    fake = FakeGitHub()
    client = GitHubClient(
        base_url="https://ghe.example.test/api/v3/",
        timeout_seconds=1,
        transport=httpx.MockTransport(fake.handle),
    )

    client.get_authenticated_user(FAKE_TOKEN)

    assert str(fake.requests[0].url) == "https://ghe.example.test/api/v3/user"


def test_401_maps_to_token_invalid() -> None:
    fake = FakeGitHub()
    fake.status = 401

    with pytest.raises(GitHubTokenInvalidError):
        fake.client().get_authenticated_user(FAKE_TOKEN)


@pytest.mark.parametrize("status", [403, 429, 404, 500, 502, 503, 301, 204])
def test_other_statuses_map_to_github_api_error(status: int) -> None:
    fake = FakeGitHub()
    fake.status = status

    with pytest.raises(GitHubApiError) as exc_info:
        fake.client().get_authenticated_user(FAKE_TOKEN)

    assert exc_info.value.code == "github_api_error"


@pytest.mark.parametrize("body", [b"<html>not json</html>", b"", b"[]", b'"x"', b'{"id": 1}'])
def test_200_with_unusable_body_maps_to_github_api_error(body: bytes) -> None:
    fake = FakeGitHub()
    fake.raw_body = body

    with pytest.raises(GitHubApiError):
        fake.client().get_authenticated_user(FAKE_TOKEN)


@pytest.mark.parametrize("login", [None, "", 123, "x" * 101])
def test_200_with_invalid_login_maps_to_github_api_error(login: object) -> None:
    fake = FakeGitHub()
    fake.raw_body = httpx.Response(200, json={"login": login}).content

    with pytest.raises(GitHubApiError):
        fake.client().get_authenticated_user(FAKE_TOKEN)


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.PoolTimeout("t")]
)
def test_timeouts_map_to_github_timeout(error: Exception) -> None:
    fake = FakeGitHub()
    fake.error = error

    with pytest.raises(GitHubTimeoutError) as exc_info:
        fake.client().get_authenticated_user(FAKE_TOKEN)

    assert exc_info.value.code == "github_timeout"
    # 逾時是「連不上」的特例，API 層可用同一個父類別處理
    assert isinstance(exc_info.value, GitHubUnreachableError)


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("c"), httpx.ReadError("r"), httpx.RemoteProtocolError("p")]
)
def test_connection_errors_map_to_github_unreachable(error: Exception) -> None:
    fake = FakeGitHub()
    fake.error = error

    with pytest.raises(GitHubUnreachableError) as exc_info:
        fake.client().get_authenticated_user(FAKE_TOKEN)

    assert exc_info.value.code == "github_unreachable"
    assert not isinstance(exc_info.value, GitHubTimeoutError)


@pytest.mark.parametrize(
    "status", [200, 401, 403, 500], ids=["ok-then-fail-body", "401", "403", "500"]
)
def test_errors_and_logs_never_contain_token_or_upstream_body(status: int) -> None:
    fake = FakeGitHub()
    fake.status = status
    if status == 200:
        fake.raw_body = f"not json {GITHUB_BODY_MARKER} {FAKE_TOKEN}".encode()

    with capture_logs() as logs, pytest.raises(RepoPilotError) as exc_info:
        fake.client().get_authenticated_user(FAKE_TOKEN)

    for text in (str(exc_info.value), exc_info.value.message, repr(exc_info.value), logs.text()):
        assert FAKE_TOKEN not in text
        assert GITHUB_BODY_MARKER not in text


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout("t"), httpx.ConnectError(f"boom {FAKE_TOKEN}")]
)
def test_transport_errors_do_not_leak_token_even_if_library_message_contains_it(
    error: Exception,
) -> None:
    fake = FakeGitHub()
    fake.error = error

    with capture_logs() as logs, pytest.raises(RepoPilotError) as exc_info:
        fake.client().get_authenticated_user(FAKE_TOKEN)

    assert FAKE_TOKEN not in str(exc_info.value)
    assert FAKE_TOKEN not in logs.text()
    # `from None` 切斷例外鏈，避免 traceback 帶出原始例外的訊息
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__


# --- 真實 socket：確認實際送出的標頭，以及 httpx／httpcore 的 DEBUG 日誌不含 token ---


class _UserHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server: _RecordingServer = self.server  # type: ignore[assignment]
        server.received.append((self.path, dict(self.headers.items())))
        body = b'{"login": "local-fake"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _RecordingServer(HTTPServer):
    received: list[tuple[str, dict[str, str]]]


@pytest.fixture
def local_server() -> Iterator[tuple[str, _RecordingServer]]:
    server = _RecordingServer(("127.0.0.1", 0), _UserHandler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_http_request_sends_bearer_header_and_debug_logs_do_not_leak_token(
    local_server: tuple[str, _RecordingServer],
) -> None:
    base_url, server = local_server
    client = GitHubClient(base_url=base_url, timeout_seconds=5)

    with capture_logs() as logs:
        user = client.get_authenticated_user(FAKE_TOKEN)

    assert user.login == "local-fake"
    path, headers = server.received[0]
    assert path == "/user"
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
    # 日誌確實有被擷取（httpx／httpcore 在 DEBUG 會輸出），且其中沒有 token 或 Authorization 標頭
    assert len(logs) > 0
    text = logs.text()
    assert FAKE_TOKEN not in text
    assert "Bearer" not in text


def test_connection_refused_maps_to_github_unreachable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = GitHubClient(base_url=f"http://127.0.0.1:{port}", timeout_seconds=2)

    with pytest.raises(GitHubUnreachableError):
        client.get_authenticated_user(FAKE_TOKEN)


# --- GET /repos/{owner}/{name}（切片 5c-2） ---


def _fake_with_repo(full_name: str = "octocat/Hello-World", **kwargs: object) -> FakeGitHub:
    fake = FakeGitHub()
    fake.add_repo(full_name, **kwargs)  # type: ignore[arg-type]
    return fake


def test_get_repository_returns_canonical_names_private_and_default_branch() -> None:
    fake = _fake_with_repo("OctoCat/Hello-World", private=True, default_branch="trunk")

    repo = fake.client().get_repository("octocat", "hello-world", FAKE_TOKEN)

    assert (repo.owner, repo.name) == ("OctoCat", "Hello-World")
    assert repo.is_private is True
    assert repo.default_branch == "trunk"
    request = fake.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.github.test/repos/octocat/hello-world"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_get_repository_without_token_sends_no_authorization_header() -> None:
    fake = _fake_with_repo()

    fake.client().get_repository("octocat", "Hello-World", None)

    assert "Authorization" not in fake.requests[0].headers


def test_get_repository_404_without_token_suggests_providing_one() -> None:
    fake = _fake_with_repo(private=True)

    with pytest.raises(GitHubRepoNotFoundError) as anonymous:
        fake.client().get_repository("octocat", "Hello-World", None)
    with pytest.raises(GitHubRepoNotFoundError) as with_token:
        FakeGitHub().client().get_repository("octocat", "Hello-World", FAKE_TOKEN)

    assert anonymous.value.code == with_token.value.code == "github_repo_not_found_or_private"
    # 兩種情境的提示不同：未保存 token → 請提供 token；已有 token → 可能是 token 範圍不足
    assert anonymous.value.message != with_token.value.message
    assert "save a GitHub token" in anonymous.value.message
    assert "saved token" in with_token.value.message


def test_get_repository_401_maps_to_token_invalid() -> None:
    fake = _fake_with_repo()
    fake.accepted_token = "another-token"

    with pytest.raises(GitHubTokenInvalidError):
        fake.client().get_repository("octocat", "Hello-World", FAKE_TOKEN)


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 301, 204])
def test_get_repository_other_statuses_map_to_github_api_error(status: int) -> None:
    fake = FakeGitHub()
    fake.repo_status = status

    with pytest.raises(GitHubApiError) as exc_info:
        fake.client().get_repository("octocat", "Hello-World", FAKE_TOKEN)

    assert exc_info.value.code == "github_api_error"


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>not json</html>",
        b"[]",
        b"{}",
        # full_name 與請求的 repo 不符（只允許大小寫差異）
        b'{"full_name": "evil/other", "private": false, "default_branch": "main"}',
        # full_name 含不安全的名稱（會成為目錄名稱）
        b'{"full_name": "octocat/..", "private": false, "default_branch": "main"}',
        b'{"full_name": "octocat/Hello-World.git", "private": false, "default_branch": "main"}',
        b'{"full_name": "octocat/Hello-World/x", "private": false, "default_branch": "main"}',
        # 欄位型別錯誤或缺漏
        b'{"full_name": "octocat/Hello-World", "private": "no", "default_branch": "main"}',
        b'{"full_name": "octocat/Hello-World", "private": false}',
        b'{"full_name": "octocat/Hello-World", "private": false, "default_branch": ""}',
        b'{"full_name": "octocat/Hello-World", "private": false, "default_branch": 5}',
        b'{"full_name": 5, "private": false, "default_branch": "main"}',
    ],
)
def test_get_repository_rejects_unexpected_payloads(payload: bytes) -> None:
    fake = FakeGitHub()
    fake.raw_body = payload

    with pytest.raises(GitHubApiError):
        fake.client().get_repository("octocat", "Hello-World", FAKE_TOKEN)


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.PoolTimeout("t")]
)
def test_get_repository_timeouts_map_to_github_timeout(error: Exception) -> None:
    fake = _fake_with_repo()
    fake.error = error

    with pytest.raises(GitHubTimeoutError):
        fake.client().get_repository("octocat", "Hello-World", FAKE_TOKEN)


def test_get_repository_connection_error_maps_to_unreachable() -> None:
    fake = _fake_with_repo()
    fake.error = httpx.ConnectError(f"boom {FAKE_TOKEN}")

    with capture_logs() as logs, pytest.raises(GitHubUnreachableError) as exc_info:
        fake.client().get_repository("octocat", "Hello-World", FAKE_TOKEN)

    assert FAKE_TOKEN not in str(exc_info.value) + logs.text()


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_get_repository_errors_and_logs_never_contain_token_or_upstream_body(status: int) -> None:
    fake = FakeGitHub()
    fake.repo_status = status

    with capture_logs() as logs, pytest.raises(RepoPilotError) as exc_info:
        fake.client().get_repository("octocat", "Hello-World", FAKE_TOKEN)

    for text in (str(exc_info.value), exc_info.value.message, repr(exc_info.value), logs.text()):
        assert FAKE_TOKEN not in text
        assert GITHUB_BODY_MARKER not in text
