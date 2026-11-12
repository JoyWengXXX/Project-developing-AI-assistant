"""切片 5c-2：以 owner/repo 註冊 GitHub repo、受管 clone、fetch（PostgreSQL ＋ 真實 Git）。

「遠端」是本機 bare repo：以 file:// 或本機 `git http-backend` 伺服器存取，不連真實 GitHub；
GitHub API 以 `httpx.MockTransport`（dependency override）模擬。token 一律是假值。
"""

import base64
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.db.models import Repository
from repopilot.errors import DatabaseUnavailableError
from repopilot.git import remote as remote_module
from repopilot.git import runner
from repopilot.main import create_app
from repopilot.services import github_repositories, managed_repos
from tests.git_helpers import RemoteRepo, git, init_repo
from tests.git_http_server import GitHttpServer
from tests.github_helpers import FAKE_TOKEN, FakeGitHub, capture_logs

URL = "/api/v1/repositories"
GITHUB_URL = f"{URL}/github"
B64_TOKEN = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
REPO_FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n"}


# --- fixtures ---


@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def managed_dir(tmp_path: Path) -> Path:
    # 不預先建立：驗證「使用時才建立」
    return tmp_path / "managed"


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    directory = tmp_path / "remotes"
    directory.mkdir()
    return directory


@pytest.fixture
def settings(
    test_database_url: str, roots: Path, managed_dir: Path, remotes: Path, encryption_key: str
) -> Settings:
    return Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        token_encryption_key=SecretStr(encryption_key),
        managed_repos_dir=managed_dir,
        github_clone_base_url=remotes.as_uri(),
        github_clone_timeout_seconds=60,
    )


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def clean_state(db_engine: Engine) -> None:
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))
        connection.execute(text("TRUNCATE github_credentials"))


@pytest.fixture
def app(settings: Settings, clean_state: None, fake_github: FakeGitHub) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_github_client] = fake_github.client
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def remote(remotes: Path, fake_github: FakeGitHub) -> RemoteRepo:
    """octocat/Hello-World：遠端 bare repo 與 GitHub API 登錄皆已就緒。"""
    fake_github.add_repo("octocat/Hello-World")
    return RemoteRepo(remotes, "octocat", "Hello-World", files=dict(REPO_FILES))


@pytest.fixture
def clone_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = remote_module.clone_repository

    def spy(*args: Any, **kwargs: Any) -> None:
        calls.append(f"{kwargs['owner']}/{kwargs['name']}")
        real(*args, **kwargs)

    monkeypatch.setattr("repopilot.services.github_repositories.clone_repository", spy)
    return calls


@pytest.fixture
def http_server(remotes: Path) -> Iterator[GitHttpServer]:
    server = GitHttpServer(remotes)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _register(client: TestClient, repo: str = "octocat/Hello-World") -> Any:
    return client.post(GITHUB_URL, json={"repo": repo})


def _save_token(client: TestClient) -> None:
    assert client.put("/api/v1/github/token", json={"token": FAKE_TOKEN}).status_code == 200


def _rows(db_engine: Engine) -> list[Repository]:
    with Session(db_engine) as session:
        return list(session.scalars(select(Repository).order_by(Repository.id)))


def _count(db_engine: Engine) -> int:
    with Session(db_engine) as session:
        return session.scalar(select(func.count()).select_from(Repository)) or 0


def _clone_path(managed_dir: Path, owner: str = "octocat", name: str = "Hello-World") -> Path:
    return managed_dir / "github" / owner / name


def _temp_children(managed_dir: Path) -> list[str]:
    root = managed_dir / ".tmp"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _files_containing(root: Path, needles: list[str]) -> list[Path]:
    """遞迴掃描所有檔案的位元組，回傳含有任一 needle 的檔案（用來確認秘密沒有落盤）。"""
    patterns = [n.encode() for n in needles]
    found: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and any(p in path.read_bytes() for p in patterns):
            found.append(path)
    return found


def _import(client: TestClient, repository_id: int, **body: Any) -> Any:
    return client.post(f"{URL}/{repository_id}/imports", json=body)


# --- 註冊：基本行為 ---


def test_register_clones_into_managed_dir_and_returns_201(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
    fake_github: FakeGitHub,
) -> None:
    response = _register(client)

    assert response.status_code == 201
    body = response.json()
    expected_path = _clone_path(managed_dir).resolve()
    assert body["name"] == "octocat/Hello-World"
    assert body["canonical_local_path"] == str(expected_path)
    assert body["source_type"] == "github"
    assert body["github_owner"] == "octocat"
    assert body["github_name"] == "Hello-World"
    assert body["github_is_private"] is False
    assert body["github_default_branch"] == "main"
    # 完整 clone 且不 checkout：有 Git 物件與歷史，工作目錄沒有檔案
    assert git(expected_path, "rev-parse", "HEAD") == remote.head()
    assert git(expected_path, "rev-parse", "--is-bare-repository") == "false"
    assert not (expected_path / "README.md").exists()
    assert not (expected_path / "src").exists()
    assert git(expected_path, "rev-parse", "--is-shallow-repository") == "false"
    assert _temp_children(managed_dir) == []
    assert [r.canonical_local_path for r in _rows(db_engine)] == [str(expected_path)]
    # 未保存 token：匿名呼叫 GitHub
    assert "Authorization" not in fake_github.requests[0].headers


def test_managed_dir_is_created_only_when_needed(
    client: TestClient, remote: RemoteRepo, managed_dir: Path
) -> None:
    assert not managed_dir.exists()  # 建立 app 與 TestClient 都不會建立

    assert _register(client).status_code == 201

    assert managed_dir.is_dir()


def test_response_uses_canonical_case_from_github_for_any_spelling(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    response = _register(client, "https://github.com/OCTOCAT/hello-world.git/")

    assert response.status_code == 201
    row = _rows(db_engine)[0]
    assert (row.github_owner, row.github_name) == ("octocat", "Hello-World")
    # 目錄名稱使用 GitHub 的標準大小寫，而不是使用者輸入的大小寫
    assert [p.name for p in (managed_dir / "github").iterdir()] == ["octocat"]
    assert [p.name for p in (managed_dir / "github" / "octocat").iterdir()] == ["Hello-World"]


def test_duplicate_registration_returns_200_without_cloning_again(
    client: TestClient, remote: RemoteRepo, clone_calls: list[str], db_engine: Engine
) -> None:
    first = _register(client)

    second = _register(client)
    different_spelling = _register(client, "https://github.com/OctoCat/HELLO-WORLD")

    assert first.status_code == 201
    assert second.status_code == 200
    assert different_spelling.status_code == 200
    assert second.json() == first.json() == different_spelling.json()
    assert clone_calls == ["octocat/Hello-World"]
    assert _count(db_engine) == 1


def test_private_repo_requires_saved_token_and_records_private_flag(
    client: TestClient, remotes: Path, fake_github: FakeGitHub, managed_dir: Path
) -> None:
    fake_github.add_repo("octocat/Secret", private=True, default_branch="trunk")
    fake_github.accepted_token = FAKE_TOKEN
    RemoteRepo(remotes, "octocat", "Secret", files={"secret.txt": "s\n"})

    anonymous = _register(client, "octocat/Secret")
    managed_after_anonymous = managed_dir.exists()
    _save_token(client)
    with_token = _register(client, "octocat/Secret")

    assert anonymous.status_code == 404
    assert anonymous.json()["error"]["code"] == "github_repo_not_found_or_private"
    assert not managed_after_anonymous  # 失敗時什麼都不建立
    assert with_token.status_code == 201
    assert with_token.json()["github_is_private"] is True
    assert with_token.json()["github_default_branch"] == "trunk"
    assert fake_github.requests[-1].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


def test_local_registration_and_listing_gain_source_fields_without_breaking_contract(
    client: TestClient, roots: Path, remote: RemoteRepo
) -> None:
    local = client.post(URL, json={"path": str(init_repo(roots / "demo"))})
    github = _register(client)

    listing = client.get(URL).json()["items"]

    assert local.status_code == 201
    assert {"id", "name", "canonical_local_path", "created_at"} <= set(local.json())
    assert local.json()["source_type"] == "local"
    assert [
        local.json()[key]
        for key in ("github_owner", "github_name", "github_is_private", "github_default_branch")
    ] == [None, None, None, None]
    assert [item["source_type"] for item in listing] == ["local", "github"]
    assert client.get(f"{URL}/{github.json()['id']}").json() == github.json()


# --- 註冊：輸入、設定與上游錯誤 ---


@pytest.mark.parametrize(
    "raw",
    ["../x", "a/../b", "-x/y", "a/b;rm", "a b/c", "octocat/x\n", "git@github.com:a/b.git",
     "http://github.com/a/b", "https://evil.com/a/b", "https://u:p@github.com/a/b",
     "https://github.com/a/b?x=1", "https://github.com/a/b/tree/main", "a/b.git", "x" * 5000],
)  # fmt: skip
def test_invalid_repo_input_returns_422_and_never_calls_github(
    client: TestClient, fake_github: FakeGitHub, managed_dir: Path, raw: str
) -> None:
    response = _register(client, raw)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_github_repo"
    assert "xxxx" not in response.text
    assert fake_github.requests == []
    assert not managed_dir.exists()


@pytest.mark.parametrize("body", [{}, {"repo": 5}, {"repo": "a/b", "extra": 1}, {"repo": None}])
def test_malformed_body_returns_validation_error(client: TestClient, body: dict[str, Any]) -> None:
    response = client.post(GITHUB_URL, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_registration_is_disabled_without_managed_dir(
    settings: Settings, client: TestClient, fake_github: FakeGitHub
) -> None:
    settings.managed_repos_dir = None

    response = _register(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "github_registration_disabled"
    assert fake_github.requests == []


def test_unknown_repo_returns_404_and_stores_nothing(
    client: TestClient, db_engine: Engine, managed_dir: Path
) -> None:
    response = _register(client, "octocat/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "github_repo_not_found_or_private"
    assert _count(db_engine) == 0
    assert not managed_dir.exists()


def test_invalid_saved_token_returns_422(
    client: TestClient, remote: RemoteRepo, fake_github: FakeGitHub, db_engine: Engine
) -> None:
    _save_token(client)
    fake_github.accepted_token = "some-other-token"

    response = _register(client)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "github_token_invalid"
    assert FAKE_TOKEN not in response.text
    assert _count(db_engine) == 0


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (httpx.ConnectError("down"), 502, "github_unreachable"),
        (httpx.ReadTimeout("slow"), 504, "github_timeout"),
    ],
)
def test_github_api_failures_map_to_gateway_errors(
    client: TestClient,
    remote: RemoteRepo,
    fake_github: FakeGitHub,
    db_engine: Engine,
    error: Exception,
    status: int,
    code: str,
) -> None:
    fake_github.error = error

    response = _register(client)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert _count(db_engine) == 0


# --- clone 失敗、逾時與清理 ---


def test_clone_failure_leaves_no_directory_or_row(
    client: TestClient, fake_github: FakeGitHub, managed_dir: Path, db_engine: Engine
) -> None:
    fake_github.add_repo("octocat/Ghost")  # GitHub API 說存在，但遠端沒有這個 bare repo

    with capture_logs() as logs:
        response = _register(client, "octocat/Ghost")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_clone_failed"
    assert _count(db_engine) == 0
    assert _temp_children(managed_dir) == []
    assert not _clone_path(managed_dir, name="Ghost").exists()
    assert "github_clone_failed" in logs.text()


def test_clone_timeout_returns_504_and_cleans_up(
    client: TestClient,
    settings: Settings,
    remote: RemoteRepo,
    http_server: GitHttpServer,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    settings.github_clone_base_url = http_server.url
    settings.github_clone_timeout_seconds = 1
    http_server.delay_seconds = 60

    started = time.monotonic()
    response = _register(client)
    elapsed = time.monotonic() - started

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "github_clone_timeout"
    # 逾時須真的中斷 clone，而不是等慢伺服器回應（Windows 的 git 啟動器會產生子程序）
    assert elapsed < 30
    assert _count(db_engine) == 0
    assert _temp_children(managed_dir) == []
    assert not _clone_path(managed_dir).exists()


def test_publish_failure_removes_only_our_temp_directory(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = managed_dir / ".tmp" / "someone-elses-dir"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")

    def broken_publish(temp: Path, destination: Path) -> None:
        raise PermissionError("rename blocked")

    monkeypatch.setattr(managed_repos, "publish", broken_publish)

    response = _register(client)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "managed_storage_error"
    assert _temp_children(managed_dir) == ["someone-elses-dir"]
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert _count(db_engine) == 0


def test_existing_unregistered_destination_is_never_overwritten_or_deleted(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    orphan = _clone_path(managed_dir)
    orphan.mkdir(parents=True)
    (orphan / "precious.txt").write_text("keep", encoding="utf-8")

    response = _register(client)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "managed_storage_error"
    assert (orphan / "precious.txt").read_text(encoding="utf-8") == "keep"
    assert _temp_children(managed_dir) == []
    assert _count(db_engine) == 0


def test_database_failure_after_clone_removes_the_new_destination(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_insert(*_: object, **__: object) -> None:
        raise DatabaseUnavailableError("simulated")

    monkeypatch.setattr(github_repositories, "_insert_row", failing_insert)

    response = _register(client)

    assert response.status_code == 503
    assert not _clone_path(managed_dir).exists()
    assert _temp_children(managed_dir) == []
    assert _count(db_engine) == 0


# --- 併發 ---


def test_concurrent_registration_of_same_repo_creates_one_row_and_one_clone(
    client: TestClient,
    remote: RemoteRepo,
    clone_calls: list[str],
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    workers = 6
    barrier = threading.Barrier(workers)

    def attempt(index: int) -> int:
        barrier.wait()
        spelling = ["octocat/Hello-World", "OCTOCAT/hello-world"][index % 2]
        return int(_register(client, spelling).status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(attempt, range(workers)))

    assert sorted(statuses) == [200] * (workers - 1) + [201]
    assert _count(db_engine) == 1
    assert clone_calls == ["octocat/Hello-World"]
    assert _temp_children(managed_dir) == []
    assert git(_clone_path(managed_dir), "rev-parse", "HEAD") == remote.head()


def test_concurrent_registration_without_the_process_lock_is_still_safe(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 模擬多個 worker 程序：行程內的鎖不共用，只剩 rename 與資料庫唯一索引保護
    monkeypatch.setattr(github_repositories, "_lock_for", lambda key: threading.Lock())
    workers = 5
    barrier = threading.Barrier(workers)

    def attempt(_: int) -> tuple[int, str | None]:
        barrier.wait()
        response = _register(client)
        error = response.json().get("error")
        return int(response.status_code), error["code"] if error else None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(attempt, range(workers)))

    statuses = [status for status, _ in results]
    assert statuses.count(201) == 1
    # 輸家只可能是 200（已看到資料列）或「目錄已存在但資料列尚未寫入」的 500，不會有其他結果
    assert set(statuses) <= {200, 201, 500}
    assert {code for status, code in results if status == 500} <= {"managed_storage_error"}
    assert _count(db_engine) == 1
    assert _temp_children(managed_dir) == []
    assert [p.name for p in (managed_dir / "github" / "octocat").iterdir()] == ["Hello-World"]
    assert git(_clone_path(managed_dir), "rev-parse", "HEAD") == remote.head()
    assert _register(client).status_code == 200


def test_losing_the_unique_index_race_removes_the_extra_clone_but_keeps_the_winner(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 資料列已存在（指向別的位置），但「先查」被騙成找不到，逼出 IntegrityError 路徑
    winner_path = managed_dir / "github" / "OCTOCAT" / "hello-world-old"
    winner_path.mkdir(parents=True)
    (winner_path / "marker.txt").write_text("winner", encoding="utf-8")
    with Session(db_engine) as session:
        session.add(
            Repository(
                name="octocat/Hello-World",
                canonical_local_path=str(winner_path),
                source_type="github",
                github_owner="OCTOCAT",
                github_name="hello-world",
            )
        )
        session.commit()
    real_find = github_repositories.find_github_repository
    calls = {"count": 0}

    def blind_find(session: Session, owner: str, name: str) -> Repository | None:
        calls["count"] += 1
        return None if calls["count"] <= 2 else real_find(session, owner, name)

    monkeypatch.setattr(github_repositories, "find_github_repository", blind_find)

    response = _register(client)

    assert response.status_code == 200
    assert response.json()["canonical_local_path"] == str(winner_path)
    assert _count(db_engine) == 1
    assert not _clone_path(managed_dir).exists()  # 多 clone 出來的目錄被清掉
    assert (winner_path / "marker.txt").read_text(encoding="utf-8") == "winner"  # 贏家不受影響
    assert _temp_children(managed_dir) == []


# --- token：不進 argv、不落盤、不外洩 ---


def test_token_is_never_written_to_the_clone_or_logs_or_responses(
    client: TestClient,
    remotes: Path,
    fake_github: FakeGitHub,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    fake_github.add_repo("octocat/Secret", private=True)
    fake_github.accepted_token = FAKE_TOKEN
    RemoteRepo(remotes, "octocat", "Secret", files={"secret.txt": "s\n"})
    _save_token(client)

    with capture_logs() as logs:
        response = _register(client, "octocat/Secret")

    assert response.status_code == 201
    clone = _clone_path(managed_dir, name="Secret")
    needles = [FAKE_TOKEN, B64_TOKEN, "x-access-token", "extraheader"]
    assert _files_containing(clone, needles) == []
    config_list = subprocess.run(  # noqa: S603
        ["git", "-C", str(clone), "config", "--list", "--show-origin"],  # noqa: S607
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.decode()
    assert not any(n in config_list for n in needles)
    assert not any(n in logs.text() + response.text for n in (FAKE_TOKEN, B64_TOKEN))
    with db_engine.connect() as connection:
        stored = connection.execute(text("SELECT encrypted_token FROM github_credentials")).scalar()
    assert stored is not None and FAKE_TOKEN not in stored


def test_token_reaches_the_server_as_basic_extraheader_and_is_not_persisted(
    client: TestClient,
    settings: Settings,
    remotes: Path,
    fake_github: FakeGitHub,
    http_server: GitHttpServer,
    managed_dir: Path,
) -> None:
    # 真實 HTTP 傳輸：伺服器要求 Authorization，才能證明 extraheader 機制有效
    settings.github_clone_base_url = http_server.url
    fake_github.add_repo("octocat/Secret", private=True)
    fake_github.accepted_token = FAKE_TOKEN
    RemoteRepo(remotes, "octocat", "Secret", files={"secret.txt": "s\n"})
    http_server.required_authorization = f"basic {B64_TOKEN}"
    _save_token(client)

    with capture_logs() as logs:
        response = _register(client, "octocat/Secret")

    assert response.status_code == 201
    assert http_server.requests, "伺服器沒有收到任何請求"
    assert {auth for _, auth in http_server.requests} == {f"basic {B64_TOKEN}"}
    clone = _clone_path(managed_dir, name="Secret")
    assert _files_containing(clone, [FAKE_TOKEN, B64_TOKEN, "x-access-token"]) == []
    assert FAKE_TOKEN not in logs.text()
    assert B64_TOKEN not in logs.text()
    # 實際取得內容（非 no-checkout 的空殼）：HEAD 可解析
    assert git(clone, "rev-parse", "--verify", "HEAD^{commit}")


def test_clone_without_token_against_authenticated_server_fails_cleanly(
    client: TestClient,
    settings: Settings,
    remotes: Path,
    fake_github: FakeGitHub,
    http_server: GitHttpServer,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    # GitHub API（假）說是公開的，但 Git 伺服器要求認證：不得卡在帳密提示或彈出憑證視窗
    settings.github_clone_base_url = http_server.url
    fake_github.add_repo("octocat/Locked")
    RemoteRepo(remotes, "octocat", "Locked")
    http_server.required_authorization = f"basic {B64_TOKEN}"

    started = time.monotonic()
    response = _register(client, "octocat/Locked")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_clone_failed"
    assert time.monotonic() - started < 30
    assert _count(db_engine) == 0
    assert _temp_children(managed_dir) == []


def test_stderr_with_token_shaped_text_is_not_returned_to_the_client(
    client: TestClient, remote: RemoteRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 即使 git 的 stderr 含有 token，回應只會有固定訊息
    _save_token(client)
    real = runner._run_killing_tree

    def noisy_failure(
        command: list[str], *, timeout: float, stdin: bytes | None, env: dict[str, str]
    ) -> tuple[int, bytes, bytes]:
        if "clone" in command:
            return 128, b"", f"fatal: bad credentials {FAKE_TOKEN} {B64_TOKEN}".encode()
        return real(command, timeout=timeout, stdin=stdin, env=env)

    monkeypatch.setattr(runner, "_run_killing_tree", noisy_failure)

    with capture_logs() as logs:
        response = _register(client)

    assert response.status_code == 502
    assert FAKE_TOKEN not in response.text
    assert B64_TOKEN not in response.text
    assert FAKE_TOKEN not in logs.text()
    assert B64_TOKEN not in logs.text()


# --- fetch ---


def test_fetch_reports_updated_refs_and_new_commits_become_importable(
    client: TestClient, remote: RemoteRepo, db_engine: Engine
) -> None:
    repository_id = _register(client).json()["id"]
    first_commit = remote.head()
    old_snapshot = _import(client, repository_id).json()
    new_commit = remote.commit({"src/app.py": "x = 2\n", "NEW.md": "new\n"}, message="second")

    fetch = client.post(f"{URL}/{repository_id}/fetch")

    assert fetch.status_code == 200
    body = fetch.json()
    assert body["repository_id"] == repository_id
    assert body["truncated"] is False
    assert body["updated_refs"] == [
        {
            "name": "origin/main",
            "kind": "branch",
            "change": "updated",
            "old_sha": first_commit,
            "new_sha": new_commit,
        }
    ]
    imported = _import(client, repository_id, revision="origin/main")
    assert imported.status_code == 201
    assert imported.json()["commit_sha"] == new_commit
    assert imported.json()["id"] != old_snapshot["id"]
    files = client.get(f"/api/v1/snapshots/{imported.json()['id']}/files?path_prefix=src/").json()
    content = client.get(
        f"/api/v1/snapshots/{imported.json()['id']}/files/{files['items'][0]['id']}"
    ).json()["content"]
    assert content == "x = 2\n"


def test_old_snapshot_is_unchanged_after_fetch_and_head_stays_at_clone_time_commit(
    client: TestClient, remote: RemoteRepo
) -> None:
    repository_id = _register(client).json()["id"]
    first_commit = remote.head()
    before = _import(client, repository_id).json()
    remote.commit({"README.md": "changed\n"}, message="second")

    client.post(f"{URL}/{repository_id}/fetch")
    after_fetch_head = _import(client, repository_id)  # HEAD = 本機預設分支，fetch 不會移動它
    snapshot_now = client.get(f"/api/v1/snapshots/{before['id']}").json()

    assert before["commit_sha"] == first_commit
    assert after_fetch_head.status_code == 200
    assert after_fetch_head.json()["id"] == before["id"]
    assert {key: snapshot_now[key] for key in before} == before
    assert snapshot_now["status"] == "completed"
    assert snapshot_now["file_count"] == before["file_count"] == 2


def test_fetch_reports_new_tags_new_branches_and_pruned_branches(
    client: TestClient, remote: RemoteRepo
) -> None:
    git(remote.work, "branch", "feature")
    remote.push("feature:refs/heads/feature")
    repository_id = _register(client).json()["id"]
    git(remote.work, "branch", "other")
    remote.push("other:refs/heads/other")
    git(remote.work, "tag", "v1.0")
    remote.push("v1.0")
    remote.delete_branch("feature")

    changes = client.post(f"{URL}/{repository_id}/fetch").json()["updated_refs"]

    assert [(c["name"], c["kind"], c["change"]) for c in changes] == [
        ("origin/feature", "branch", "removed"),
        ("origin/other", "branch", "added"),
        ("v1.0", "tag", "added"),
    ]
    assert _import(client, repository_id, revision="v1.0").status_code == 201


def test_fetch_without_changes_returns_empty_list(client: TestClient, remote: RemoteRepo) -> None:
    repository_id = _register(client).json()["id"]

    response = client.post(f"{URL}/{repository_id}/fetch")

    assert response.status_code == 200
    assert response.json() == {
        "repository_id": repository_id,
        "updated_refs": [],
        "truncated": False,
    }


def test_fetch_response_is_capped_and_marked_truncated(
    client: TestClient, remote: RemoteRepo
) -> None:
    repository_id = _register(client).json()["id"]
    remote.add_tags([f"t{i:03d}" for i in range(130)])

    body = client.post(f"{URL}/{repository_id}/fetch").json()

    assert body["truncated"] is True
    assert len(body["updated_refs"]) == 100
    names = [c["name"] for c in body["updated_refs"]]
    assert names == sorted(names)


def test_fetch_sends_the_token_in_the_extraheader_over_http(
    client: TestClient,
    settings: Settings,
    remotes: Path,
    fake_github: FakeGitHub,
    http_server: GitHttpServer,
) -> None:
    settings.github_clone_base_url = http_server.url
    fake_github.add_repo("octocat/Secret", private=True)
    fake_github.accepted_token = FAKE_TOKEN
    secret = RemoteRepo(remotes, "octocat", "Secret", files={"secret.txt": "s\n"})
    http_server.required_authorization = f"basic {B64_TOKEN}"
    _save_token(client)
    repository_id = _register(client, "octocat/Secret").json()["id"]
    new_commit = secret.commit({"secret.txt": "s2\n"})
    http_server.requests.clear()

    response = client.post(f"{URL}/{repository_id}/fetch")

    assert response.status_code == 200
    assert response.json()["updated_refs"][0]["new_sha"] == new_commit
    assert http_server.requests
    assert {auth for _, auth in http_server.requests} == {f"basic {B64_TOKEN}"}


def test_fetch_timeout_returns_504(
    client: TestClient,
    settings: Settings,
    remote: RemoteRepo,
    http_server: GitHttpServer,
) -> None:
    settings.github_clone_base_url = http_server.url
    repository_id = _register(client).json()["id"]
    settings.github_clone_timeout_seconds = 1
    http_server.delay_seconds = 60

    started = time.monotonic()
    response = client.post(f"{URL}/{repository_id}/fetch")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "github_fetch_timeout"
    assert time.monotonic() - started < 30


def test_fetch_failure_returns_502(client: TestClient, remote: RemoteRepo, remotes: Path) -> None:
    repository_id = _register(client).json()["id"]
    (remotes / "octocat" / "Hello-World.git").rename(remotes / "octocat" / "moved.git")

    response = client.post(f"{URL}/{repository_id}/fetch")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_fetch_failed"


def test_fetch_local_repository_returns_409(client: TestClient, roots: Path) -> None:
    local = client.post(URL, json={"path": str(init_repo(roots / "demo"))}).json()

    response = client.post(f"{URL}/{local['id']}/fetch")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "repository_not_github"


def test_fetch_unknown_repository_returns_404_and_bad_id_422(client: TestClient) -> None:
    assert client.post(f"{URL}/999/fetch").status_code == 404
    assert client.post(f"{URL}/999/fetch").json()["error"]["code"] == "repository_not_found"
    assert client.post(f"{URL}/0/fetch").status_code == 422
    assert client.post(f"{URL}/abc/fetch").status_code == 422


# --- 匯入 GitHub 來源的 repo ---


def test_import_from_github_clone_reads_git_objects_with_existing_rules(
    client: TestClient, remotes: Path, fake_github: FakeGitHub, db_engine: Engine
) -> None:
    fake_github.add_repo("octocat/Hello-World")
    RemoteRepo(
        remotes,
        "octocat",
        "Hello-World",
        files={**REPO_FILES, ".env": "SECRET=FAKE_KEY_FOR_TEST\n", "bin.dat": "a\x00b"},
    )
    repository_id = _register(client).json()["id"]

    response = _import(client, repository_id)

    assert response.status_code == 201
    snapshot = response.json()
    assert snapshot["status"] == "completed"
    assert (snapshot["file_count"], snapshot["skipped_count"]) == (2, 2)
    listing = client.get(f"/api/v1/snapshots/{snapshot['id']}/files?limit=200").json()["items"]
    by_path = {item["path"]: item for item in listing}
    assert by_path["README.md"]["status"] == "imported"
    assert by_path[".env"]["skip_reason"] == "excluded"
    assert by_path["bin.dat"]["skip_reason"] == "binary"
    detail = client.get(f"/api/v1/snapshots/{snapshot['id']}/files/{by_path['src/app.py']['id']}")
    assert detail.json()["content"] == "x = 1\n"
    # 同一個 commit 再匯入回 200 與既有快照
    assert _import(client, repository_id).status_code == 200


def _tamper_path(
    db_engine: Engine, repository_id: int, new_path: Path, *, delete_row: int | None = None
) -> None:
    """直接改資料庫模擬被竄改的路徑；`delete_row` 先刪掉原本占用該路徑的資料列（唯一約束）。"""
    with db_engine.begin() as connection:
        if delete_row is not None:
            connection.execute(text("DELETE FROM repositories WHERE id = :i"), {"i": delete_row})
        connection.execute(
            text("UPDATE repositories SET canonical_local_path = :p WHERE id = :i"),
            {"p": str(new_path), "i": repository_id},
        )


def test_import_rejects_github_row_whose_path_moved_outside_the_managed_dir(
    client: TestClient, remote: RemoteRepo, roots: Path, db_engine: Engine
) -> None:
    repository_id = _register(client).json()["id"]
    outside = init_repo(roots / "outside")
    _tamper_path(db_engine, repository_id, outside)

    for response in (_import(client, repository_id), client.post(f"{URL}/{repository_id}/fetch")):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_repository_path"


def test_import_rejects_github_row_pointing_to_another_clone(
    client: TestClient,
    remote: RemoteRepo,
    remotes: Path,
    fake_github: FakeGitHub,
    db_engine: Engine,
) -> None:
    fake_github.add_repo("octocat/Other")
    RemoteRepo(remotes, "octocat", "Other", files={"other.txt": "o\n"})
    first = _register(client).json()
    second = _register(client, "octocat/Other").json()
    _tamper_path(
        db_engine, first["id"], Path(second["canonical_local_path"]), delete_row=second["id"]
    )

    response = _import(client, first["id"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"


def test_import_rejects_local_row_pointing_into_the_managed_dir(
    client: TestClient, remote: RemoteRepo, roots: Path, db_engine: Engine
) -> None:
    github = _register(client).json()
    local = client.post(URL, json={"path": str(init_repo(roots / "demo"))}).json()
    _tamper_path(
        db_engine, local["id"], Path(github["canonical_local_path"]), delete_row=github["id"]
    )

    response = _import(client, local["id"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"


def test_import_rejects_github_repo_when_managed_dir_is_no_longer_configured(
    client: TestClient, settings: Settings, remote: RemoteRepo
) -> None:
    repository_id = _register(client).json()["id"]
    settings.managed_repos_dir = None

    response = _import(client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"


def test_local_registration_cannot_use_the_managed_dir_even_inside_repository_roots(
    settings: Settings, client: TestClient, remote: RemoteRepo, roots: Path
) -> None:
    # 設定者把受管目錄放在 REPOSITORY_ROOTS 之內：本機註冊仍不得指向受管 clone
    managed_inside = roots / "managed"
    settings.managed_repos_dir = managed_inside
    github = _register(client).json()

    response = client.post(URL, json={"path": github["canonical_local_path"]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"


def _link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("無法建立 symlink")
        completed = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("無法建立 symlink 或 junction")


def test_import_rejects_clone_directory_replaced_by_link_to_outside(
    client: TestClient, remote: RemoteRepo, roots: Path, managed_dir: Path
) -> None:
    repository_id = _register(client).json()["id"]
    outside = init_repo(roots / "outside")
    clone = _clone_path(managed_dir)
    managed_repos.remove_created_tree(
        clone, managed_root=managed_dir.resolve(), expected_parent=clone.parent
    )
    _link_directory(clone, outside)

    response = _import(client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"
    assert (outside / "README.md").exists()  # 連結目標沒有被清理動作波及


# --- 資料庫約束 ---


def test_database_enforces_github_fields_and_source_type(
    db_engine: Engine, clean_state: None
) -> None:
    def insert(**values: Any) -> None:
        with Session(db_engine) as session:
            session.add(Repository(name="x", **values))
            session.commit()

    with pytest.raises(IntegrityError) as missing_owner:
        insert(canonical_local_path="/p1", source_type="github", github_name="n")
    with pytest.raises(IntegrityError) as local_with_owner:
        insert(canonical_local_path="/p2", source_type="local", github_owner="o", github_name="n")
    with pytest.raises(IntegrityError) as bad_type:
        insert(canonical_local_path="/p3", source_type="gitlab")

    assert "ck_repositories_github_fields_match_source" in str(missing_owner.value.orig)
    assert "ck_repositories_github_fields_match_source" in str(local_with_owner.value.orig)
    # source_type 的值域錯誤同時違反兩條 CHECK，PostgreSQL 回報哪一條不固定：只斷言是 CHECK 違規
    assert "violates check constraint" in str(bad_type.value.orig)
    assert _count(db_engine) == 0


def test_database_deduplicates_github_rows_case_insensitively_but_not_local_rows(
    db_engine: Engine, clean_state: None
) -> None:
    with Session(db_engine) as session:
        session.add_all(
            [
                Repository(name="l1", canonical_local_path="/l1"),
                Repository(name="l2", canonical_local_path="/l2"),
                Repository(
                    name="g",
                    canonical_local_path="/g1",
                    source_type="github",
                    github_owner="Octo",
                    github_name="Repo",
                ),
            ]
        )
        session.commit()
        session.add(
            Repository(
                name="g2",
                canonical_local_path="/g2",
                source_type="github",
                github_owner="octo",
                github_name="REPO",
            )
        )
        with pytest.raises(IntegrityError) as exc_info:
            session.commit()

    assert "uq_repositories_github_full_name_lower" in str(exc_info.value.orig)
    assert _count(db_engine) == 3
    with Session(db_engine) as session:
        assert {r.source_type for r in session.scalars(select(Repository))} == {"local", "github"}
