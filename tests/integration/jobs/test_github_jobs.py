"""GitHub 註冊與 fetch 背景工作（切片 5，規格 4.5、5.5、5.8、5.10～5.12，D44）：API 與 worker 流程。

真實 PostgreSQL＋真實 Git；「遠端」是本機 bare repo（`file://`，或本機 `git http-backend`
伺服器以便製造慢速／需要認證的遠端）；GitHub API 以 `MockTransport` 注入
（`run_job(github_client=…)`）。不需要 Redis 與 worker 容器（真實容器由 `stack` 測試驗證）：
worker 流程直接呼叫 `job_runner.run_job`，「容器端」就是本機檔案系統。token 一律是假值；
不連真實 GitHub。
"""

import base64
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.db.locks import acquire_advisory_lock, acquire_advisory_lock_wait, repo_lock_key
from repopilot.main import create_app
from repopilot.path_mapping import PathMapping
from repopilot.services import github_credentials, github_job_runner, job_runner
from repopilot.services import github_repositories as github_service
from repopilot.services import jobs as job_service
from tests.git_helpers import RemoteRepo, git, init_repo
from tests.git_http_server import GitHttpServer
from tests.github_helpers import FAKE_TOKEN, FakeGitHub, capture_logs
from tests.integration.jobs.conftest import WorkerEnv, at, job_state, remove_tree, scalar

REGISTER_URL = "/api/v1/github/registration-jobs"
REPO = "octocat/Hello-World"
REPO_FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n"}
B64_TOKEN = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
SUBMIT_FIELDS = {"job", "snapshot_id", "repository_id", "reused"}


@dataclass
class GitHubEnv:
    settings: Settings  # API 與 worker 共用的設定（worker 另外套用 apply_worker_timeouts）
    remotes: Path
    fake: FakeGitHub
    managed: Path
    roots: Path

    @property
    def worker_settings(self) -> Settings:
        return job_runner.apply_worker_timeouts(self.settings)


@pytest.fixture
def github_env(
    worker_env: WorkerEnv,
    tmp_path: Path,
    db_engine: Engine,
    clean_repositories: None,
    clean_jobs: None,
) -> GitHubEnv:
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE github_credentials"))
    settings = worker_env.settings.model_copy(
        update={
            "token_encryption_key": SecretStr(Fernet.generate_key().decode()),
            "github_clone_base_url": remotes.as_uri(),
            "github_clone_timeout_seconds": 60,
            # 輪詢間隔調短，取消與進度測試不必等
            "git_poll_seconds": 0.1,
            "clone_progress_seconds": 0.1,
        }
    )
    return GitHubEnv(settings, remotes, FakeGitHub(), worker_env.managed, worker_env.roots)


@pytest.fixture
def client(github_env: GitHubEnv) -> Iterator[TestClient]:
    with TestClient(create_app(github_env.settings)) as test_client:
        yield test_client


@pytest.fixture
def remote(github_env: GitHubEnv) -> RemoteRepo:
    github_env.fake.add_repo(REPO)
    return RemoteRepo(github_env.remotes, "octocat", "Hello-World", files=dict(REPO_FILES))


@pytest.fixture
def http_server(github_env: GitHubEnv) -> Iterator[GitHttpServer]:
    server = GitHttpServer(github_env.remotes)
    server.start()
    try:
        yield server
    finally:
        server.stop()


# --- helpers ---


def _submit(client: TestClient, repo: str = REPO) -> Any:
    return client.post(REGISTER_URL, json={"repo": repo})


def _submit_job_id(client: TestClient, repo: str = REPO) -> int:
    response = _submit(client, repo)
    assert response.status_code == 202, response.text
    return int(response.json()["job"]["id"])


def _run(env: GitHubEnv, job_id: int, engine: Engine, **kwargs: Any) -> str:
    return str(
        job_runner.run_job(
            job_id,
            engine,
            env.worker_settings,
            github_client=env.fake.client(),
            heartbeat_interval_seconds=0.2,
            **kwargs,
        )
    )


def _save_token(env: GitHubEnv, engine: Engine) -> None:
    with Session(engine) as session:
        github_credentials.connect_token(
            session, token=FAKE_TOKEN, settings=env.settings, client=env.fake.client()
        )


def _count(engine: Engine, table: str) -> int:
    return int(scalar(engine, f"SELECT count(*) FROM {table}"))  # noqa: S608 - 固定表名


def _repositories(engine: Engine) -> list[tuple[int, str, str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, github_owner, github_name, canonical_local_path "
                "FROM repositories ORDER BY id"
            )
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]


def _clone_path(env: GitHubEnv, owner: str = "octocat", name: str = "Hello-World") -> Path:
    return env.managed / "github" / owner / name


def _temp_children(env: GitHubEnv) -> list[str]:
    root = env.managed / ".tmp"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _advisory_locks(engine: Engine) -> int:
    return int(scalar(engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))


def _job_row(engine: Engine, job_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(text("SELECT * FROM jobs WHERE id = :i"), {"i": job_id}).one()
    return dict(row._mapping)


def _register_through_a_job(
    env: GitHubEnv, client: TestClient, engine: Engine, repo: str = REPO
) -> int:
    """提交並執行一個註冊 job，回傳新 repo 的 id。"""
    job_id = _submit_job_id(client, repo)
    assert _run(env, job_id, engine) == "succeeded"
    return int(job_state(engine, job_id)["result"]["repository_id"])


# --- API：registration-jobs ---


def test_registration_job_is_queued_with_a_safe_description_and_an_outbox_event(
    client: TestClient, remote: RemoteRepo, db_engine: Engine, github_env: GitHubEnv
) -> None:
    response = _submit(client, "OCTOCAT/hello-world")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == SUBMIT_FIELDS
    assert (body["reused"], body["snapshot_id"], body["repository_id"]) == (False, None, None)
    job = body["job"]
    assert response.headers["Location"] == f"/api/v1/jobs/{job['id']}"
    assert job["status_url"] == f"/api/v1/jobs/{job['id']}"
    assert (job["kind"], job["status"], job["phase"], job["attempt"]) == (
        "github_register",
        "queued",
        "pending",
        0,
    )
    # 只存驗證過的 owner/name（不是使用者的原始字串）；repository_id 在成功前為 NULL
    assert job["params"] == {"github_repo": "OCTOCAT/hello-world"}
    assert job["repository_id"] is None
    assert job["result"] is None
    row = _job_row(db_engine, job["id"])
    assert row["dedupe_key"] == "register:octocat/hello-world"
    assert row["repo_key"] == "github:octocat/hello-world"
    assert (
        scalar(db_engine, "SELECT status FROM job_dispatches WHERE job_id = :i", i=job["id"])
        == "pending"
    )
    # API 不連 GitHub、不 clone：沒有任何 GitHub 請求、沒有受管目錄內容、沒有資料列
    assert not (github_env.managed / "github").exists()
    assert _count(db_engine, "repositories") == 0


def test_registration_dedupe_is_case_insensitive_and_returns_the_active_job(
    client: TestClient, remote: RemoteRepo, db_engine: Engine
) -> None:
    first = _submit(client, "octocat/Hello-World")
    second = _submit(client, "OCTOCAT/HELLO-WORLD")
    url_form = _submit(client, "https://github.com/octocat/hello-world.git")

    assert (first.status_code, second.status_code, url_form.status_code) == (202, 202, 202)
    ids = {r.json()["job"]["id"] for r in (first, second, url_form)}
    assert len(ids) == 1
    assert _count(db_engine, "jobs") == 1


def test_registration_after_a_finished_job_creates_a_new_job(
    client: TestClient, remote: RemoteRepo, db_engine: Engine, github_env: GitHubEnv
) -> None:
    first = _submit_job_id(client)
    github_env.fake.repo_status = 404  # 第一次失敗：GitHub 看不到
    assert _run(github_env, first, db_engine) == "failed"

    second = _submit(client)

    assert second.status_code == 202
    assert second.json()["job"]["id"] != first
    assert _count(db_engine, "jobs") == 2


def test_already_registered_repository_returns_200_without_a_job(
    client: TestClient, remote: RemoteRepo, db_engine: Engine, github_env: GitHubEnv
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    jobs_before = _count(db_engine, "jobs")

    response = _submit(client, "OCTOCAT/hello-world")

    assert response.status_code == 200
    assert response.json() == {
        "job": None,
        "snapshot_id": None,
        "repository_id": repository_id,
        "reused": True,
    }
    assert "Location" not in response.headers
    assert _count(db_engine, "jobs") == jobs_before


def test_registration_is_disabled_without_a_managed_dir(
    github_env: GitHubEnv, db_engine: Engine
) -> None:
    settings = github_env.settings.model_copy(update={"managed_repos_dir": None})
    with TestClient(create_app(settings)) as client:
        response = _submit(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "github_registration_disabled"
    assert _count(db_engine, "jobs") == 0


@pytest.mark.parametrize("mapping", ["none", "read_only"])
def test_registration_needs_a_writable_worker_mapping_for_the_managed_dir(
    github_env: GitHubEnv, db_engine: Engine, mapping: str
) -> None:
    mappings: list[PathMapping] = []
    if mapping == "read_only":
        mappings = [PathMapping(host=str(github_env.managed), container="/mnt/managed", mode="ro")]
    settings = github_env.settings.model_copy(update={"worker_path_mappings": mappings})
    with TestClient(create_app(settings)) as client:
        response = _submit(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "worker_path_not_mapped"
    assert _count(db_engine, "jobs") == 0


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "no-slash",
        "a/b/c",
        "http://github.com/o/n",
        "ssh://git@github.com/o/n",
        "o/n;rm",
        "-x/y",
    ],
)
def test_registration_rejects_an_invalid_repo_without_echoing_it(
    client: TestClient, db_engine: Engine, repo: str
) -> None:
    response = _submit(client, repo)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_github_repo"
    assert repo not in response.text or repo == ""
    assert _count(db_engine, "jobs") == 0


def test_registration_body_must_have_exactly_the_repo_field(client: TestClient) -> None:
    response = client.post(REGISTER_URL, json={"repo": REPO, "token": FAKE_TOKEN})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert FAKE_TOKEN not in response.text


# --- API：fetch-jobs ---


def _fetch_url(repository_id: int | str) -> str:
    return f"/api/v1/repositories/{repository_id}/fetch-jobs"


def test_fetch_job_is_queued_and_deduplicated_while_active(
    client: TestClient, remote: RemoteRepo, db_engine: Engine, github_env: GitHubEnv
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)

    first = client.post(_fetch_url(repository_id))
    second = client.post(_fetch_url(repository_id))

    assert (first.status_code, second.status_code) == (202, 202)
    job = first.json()["job"]
    assert second.json()["job"]["id"] == job["id"]
    assert first.json()["repository_id"] == repository_id
    assert first.headers["Location"] == f"/api/v1/jobs/{job['id']}"
    assert (job["kind"], job["status"], job["repository_id"]) == (
        "github_fetch",
        "queued",
        repository_id,
    )
    row = _job_row(db_engine, job["id"])
    assert row["dedupe_key"] == "fetch:github:octocat/hello-world"
    assert scalar(db_engine, "SELECT count(*) FROM jobs WHERE kind = 'github_fetch'") == 1


def test_a_finished_fetch_does_not_block_the_next_one(
    client: TestClient, remote: RemoteRepo, db_engine: Engine, github_env: GitHubEnv
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    first = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])
    assert _run(github_env, first, db_engine) == "succeeded"

    second = client.post(_fetch_url(repository_id))

    assert second.status_code == 202
    assert second.json()["job"]["id"] != first


def test_fetch_job_for_a_local_repository_is_409(
    client: TestClient, github_env: GitHubEnv, db_engine: Engine
) -> None:
    local = init_repo(github_env.roots / "demo")
    repository_id = client.post("/api/v1/repositories", json={"path": str(local)}).json()["id"]

    response = client.post(_fetch_url(repository_id))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "repository_not_github"
    assert _count(db_engine, "jobs") == 0


def test_fetch_job_for_an_unknown_repository_is_404(client: TestClient) -> None:
    response = client.post(_fetch_url(999))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "repository_not_found"


@pytest.mark.parametrize("repository_id", [0, -1, 2**31, "abc"])
def test_fetch_job_id_is_range_checked(client: TestClient, repository_id: int | str) -> None:
    response = client.post(_fetch_url(repository_id))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_fetch_job_needs_the_managed_dir_setting(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    db_engine: Engine,
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    settings = github_env.settings.model_copy(update={"managed_repos_dir": None})
    with TestClient(create_app(settings)) as disabled:
        response = disabled.post(_fetch_url(repository_id))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "github_registration_disabled"


# --- OpenAPI ---


def test_openapi_marks_the_synchronous_github_endpoints_deprecated(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert paths["/api/v1/repositories/github"]["post"]["deprecated"] is True
    assert paths["/api/v1/repositories/{repository_id}/fetch"]["post"]["deprecated"] is True
    assert "deprecated" not in paths[REGISTER_URL]["post"]
    assert "deprecated" not in paths["/api/v1/repositories/{repository_id}/fetch-jobs"]["post"]
    assert "202" in paths[REGISTER_URL]["post"]["responses"]
    assert "200" in paths[REGISTER_URL]["post"]["responses"]
    assert "202" in paths["/api/v1/repositories/{repository_id}/fetch-jobs"]["post"]["responses"]


# --- worker：註冊 ---


def test_a_registration_job_clones_registers_and_reports_the_repository_id(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phases: list[str] = []
    real = job_service.update_progress

    def spy(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("phase") is not None:
            phases.append(kwargs["phase"])
        return real(*args, **kwargs)

    monkeypatch.setattr(job_service, "update_progress", spy)
    job_id = _submit_job_id(client, "octocat/hello-world")  # 使用者的大小寫不同於 GitHub 的標準

    assert _run(github_env, job_id, db_engine) == "succeeded"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["phase"]) == ("succeeded", "completed")
    rows = _repositories(db_engine)
    assert len(rows) == 1
    repository_id, owner, name, path = rows[0]
    assert (owner, name) == ("octocat", "Hello-World")  # GitHub 回傳的標準大小寫
    assert state["result"] == {
        "repository_id": repository_id,
        "github_repo": "octocat/Hello-World",
        "already_registered": False,
    }
    assert _job_row(db_engine, job_id)["repository_id"] == repository_id
    assert Path(path) == _clone_path(github_env).resolve()
    assert git(_clone_path(github_env), "rev-parse", "HEAD") == remote.head()
    assert _temp_children(github_env) == []
    assert _advisory_locks(db_engine) == 0
    assert _count(db_engine, "job_dispatches") == 0
    assert phases == ["resolving", "cloning", "registering"]
    # 暫存目錄紀錄在 rename 後清除；回應不外露 work_state
    assert _job_row(db_engine, job_id)["work_state"] == {"temp_dir": None}
    api_job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert api_job["result"]["repository_id"] == repository_id
    assert "temp_dir" not in str(api_job)
    # 已註冊的 repo 可以接著被同步端點與 fetch-jobs 使用
    assert client.get(f"/api/v1/repositories/{repository_id}").status_code == 200


def test_a_registration_job_for_an_already_registered_repo_succeeds_without_cloning(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    job_id = _submit_job_id(client)
    # 在 job 執行前，另一條路徑（同步端點）已註冊了同一個 repo
    github_env.settings.managed_repos_dir = github_env.managed
    sync_app = create_app(github_env.settings)
    sync_app.dependency_overrides[get_github_client] = github_env.fake.client
    with TestClient(sync_app) as sync_client:
        created = sync_client.post("/api/v1/repositories/github", json={"repo": REPO})
    assert created.status_code == 201
    before = git(_clone_path(github_env), "rev-parse", "HEAD")

    assert _run(github_env, job_id, db_engine) == "succeeded"

    state = job_state(db_engine, job_id)
    assert state["result"]["already_registered"] is True
    assert state["result"]["repository_id"] == created.json()["id"]
    assert len(_repositories(db_engine)) == 1
    assert git(_clone_path(github_env), "rev-parse", "HEAD") == before
    assert _temp_children(github_env) == []


def test_a_missing_repository_fails_permanently_and_leaves_nothing(
    client: TestClient, github_env: GitHubEnv, db_engine: Engine
) -> None:
    job_id = _submit_job_id(client, "octocat/Missing")  # GitHub（假）沒有這個 repo

    assert _run(github_env, job_id, db_engine) == "failed"

    state = job_state(db_engine, job_id)
    assert state["error_code"] == "github_repo_not_found_or_private"
    assert state["attempt"] == 1  # 永久性：不重試
    assert _count(db_engine, "repositories") == 0
    assert not (github_env.managed / "github").exists()
    assert _temp_children(github_env) == []


def test_clone_failure_leaves_no_partial_row_or_directory(
    client: TestClient, github_env: GitHubEnv, db_engine: Engine
) -> None:
    github_env.fake.add_repo(REPO)  # GitHub 說存在，但 Git 遠端沒有（404／網路中斷的替身）
    job_id = _submit_job_id(client)

    assert _run(github_env, job_id, db_engine) == "failed"

    state = job_state(db_engine, job_id)
    assert state["error_code"] == "github_clone_failed"
    assert "fatal" not in (state["error_summary"] or "")
    assert (_count(db_engine, "repositories"), _temp_children(github_env)) == (0, [])
    assert not _clone_path(github_env).exists()
    assert _advisory_locks(db_engine) == 0


def test_an_invalid_token_is_a_permanent_failure(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    _save_token(github_env, db_engine)
    github_env.fake.accepted_token = "github_pat_TESTONLY_some_other_token"  # 儲存的已失效
    job_id = _submit_job_id(client)

    assert _run(github_env, job_id, db_engine) == "failed"

    assert job_state(db_engine, job_id)["error_code"] == "github_token_invalid"
    assert (_count(db_engine, "repositories"), _temp_children(github_env)) == (0, [])


def test_a_saved_token_that_cannot_be_decrypted_fails_without_calling_github(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    _save_token(github_env, db_engine)
    job_id = _submit_job_id(client)
    # worker 環境缺少 TOKEN_ENCRYPTION_KEY（或金鑰與保存時不同）
    github_env.settings.token_encryption_key = SecretStr("")
    requests_before = len(github_env.fake.requests)

    assert _run(github_env, job_id, db_engine) == "failed"

    state = job_state(db_engine, job_id)
    assert state["error_code"] == "token_encryption_not_configured"
    assert state["attempt"] == 1  # 永久性：不重試
    assert len(github_env.fake.requests) == requests_before  # 沒有把請求送出去
    assert (_count(db_engine, "repositories"), _temp_children(github_env)) == (0, [])
    assert FAKE_TOKEN not in str(_job_row(db_engine, job_id))


def test_no_saved_token_means_an_anonymous_registration_even_without_a_key(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    github_env.settings.token_encryption_key = SecretStr("")
    job_id = _submit_job_id(client)

    assert _run(github_env, job_id, db_engine) == "succeeded"

    assert github_env.fake.requests[-1].headers.get("Authorization") is None


@pytest.mark.parametrize(
    ("setting", "code"),
    [("repo_status", "github_server_error"), ("error", "github_unreachable")],
)
def test_github_5xx_and_connection_failures_are_transient(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    db_engine: Engine,
    setting: str,
    code: str,
) -> None:
    if setting == "repo_status":
        github_env.fake.repo_status = 503
    else:
        github_env.fake.error = httpx.ConnectError("boom")
    job_id = _submit_job_id(client)

    assert _run(github_env, job_id, db_engine) == "retry_scheduled"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"], state["attempt"]) == ("queued", code, 1)
    assert state["next_attempt_at"] is not None
    assert (_count(db_engine, "repositories"), _temp_children(github_env)) == (0, [])
    assert _advisory_locks(db_engine) == 0


def test_github_403_is_permanent(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    github_env.fake.repo_status = 403
    job_id = _submit_job_id(client)

    assert _run(github_env, job_id, db_engine) == "failed"

    assert job_state(db_engine, job_id)["error_code"] == "github_api_error"


def test_a_busy_repo_lock_defers_the_registration_without_consuming_an_attempt(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    job_id = _submit_job_id(client)
    held = acquire_advisory_lock(db_engine, repo_lock_key("github:octocat/hello-world"))
    assert held is not None  # 例如同步端點正在 clone 同一個 repo
    try:
        assert _run(github_env, job_id, db_engine) == "deferred"
    finally:
        held.release()

    state = job_state(db_engine, job_id)
    assert (state["status"], state["attempt"]) == ("queued", 0)
    assert github_env.fake.requests == []  # 取鎖在查 GitHub 之前
    assert _run_after_defer(github_env, job_id, db_engine) == "succeeded"


def _run_after_defer(env: GitHubEnv, job_id: int, engine: Engine) -> str:
    # 延後時間不等：直接讓它可以被執行
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET next_attempt_at = NULL WHERE id = :i"), {"i": job_id}
        )
    return _run(env, job_id, engine)


def test_the_token_never_appears_in_the_database_logs_or_responses(
    client: TestClient,
    github_env: GitHubEnv,
    remote: RemoteRepo,
    http_server: GitHttpServer,
    db_engine: Engine,
) -> None:
    github_env.settings.github_clone_base_url = http_server.url
    http_server.required_authorization = f"basic {B64_TOKEN}"
    github_env.fake.add_repo("octocat/Secret", private=True)
    github_env.fake.accepted_token = FAKE_TOKEN
    RemoteRepo(github_env.remotes, "octocat", "Secret", files={"s.txt": "s\n"})
    _save_token(github_env, db_engine)
    job_id = _submit_job_id(client, "octocat/Secret")

    with capture_logs() as logs:
        assert _run(github_env, job_id, db_engine) == "succeeded"
        api_text = client.get(f"/api/v1/jobs/{job_id}").text

    # 伺服器真的收到 Authorization（token 有送出去），但其他任何地方都沒有
    assert {auth for _, auth in http_server.requests} == {f"basic {B64_TOKEN}"}
    row_text = str(_job_row(db_engine, job_id))
    dispatches = str(
        scalar(db_engine, "SELECT coalesce(string_agg(j::text, ''), '') FROM job_dispatches j")
    )
    everything = row_text + dispatches + api_text + logs.text()
    for needle in (FAKE_TOKEN, B64_TOKEN, "x-access-token", "extraheader"):
        assert needle not in everything
    clone = _clone_path(github_env, name="Secret")
    assert git(clone, "rev-parse", "--verify", "HEAD^{commit}")
    config_text = (clone / ".git" / "config").read_text(encoding="utf-8")
    assert not any(n in config_text for n in (FAKE_TOKEN, B64_TOKEN, "extraheader"))


# --- worker：暫存目錄清理 ---


def _make_hex_dir(env: GitHubEnv, name: str, *, age_seconds: float = 0) -> Path:
    path = env.managed / ".tmp" / name
    (path / ".git" / "objects").mkdir(parents=True)
    (path / ".git" / "objects" / "pack.idx").write_bytes(b"x" * 10)
    (path / ".git" / "objects" / "pack.idx").chmod(0o444)  # Windows 的唯讀屬性（Git 物件常見）
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def test_a_new_attempt_removes_the_previous_attempts_temp_dir_and_nothing_else(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    job_id = _submit_job_id(client)
    leftover = _make_hex_dir(github_env, "0123456789abcdef")
    bystander = _make_hex_dir(github_env, "fedcba9876543210")  # 年輕、沒有紀錄：不是孤兒
    stranger = github_env.managed / ".tmp" / "someone-elses-dir"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("keep", encoding="utf-8")
    with db_engine.begin() as connection:  # 上一個 attempt 被 SIGKILL 時留下的紀錄
        connection.execute(
            text("UPDATE jobs SET work_state = CAST(:w AS jsonb) WHERE id = :i"),
            {"w": '{"temp_dir": "0123456789abcdef"}', "i": job_id},
        )

    assert _run(github_env, job_id, db_engine) == "succeeded"

    assert not leftover.exists()
    assert bystander.exists()  # 不是這個 job 記錄的，也不夠舊
    assert (stranger / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert _clone_path(github_env).is_dir()


@pytest.mark.parametrize(
    "recorded",
    [
        "../../victim",
        "..",
        ".",
        "/etc",
        "C:\\Windows",
        "0123456789abcdeg",  # 不是 hex
        "0123456789ABCDEF",  # 大寫：不是本系統產生的格式
        "0123456789abcde",  # 長度不對
        "0123456789abcdef/../../victim",
        "",
        "x" * 300,
    ],
)
def test_a_tampered_temp_dir_record_deletes_nothing(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    db_engine: Engine,
    tmp_path: Path,
    recorded: str,
) -> None:
    victim = github_env.managed.parent / "victim"
    victim.mkdir(exist_ok=True)
    (victim / "precious.txt").write_text("keep", encoding="utf-8")
    inside = github_env.managed / "github" / "keep" / "x"
    inside.mkdir(parents=True)
    job_id = _submit_job_id(client)

    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET work_state = CAST(:w AS jsonb) WHERE id = :i"),
            {"w": json.dumps({"temp_dir": recorded}), "i": job_id},
        )

    _run(github_env, job_id, db_engine)  # 成功與否不重要；重點是沒有刪任何東西

    assert (victim / "precious.txt").read_text(encoding="utf-8") == "keep"
    assert inside.is_dir()
    assert github_env.managed.is_dir()


def test_orphan_temp_dirs_are_swept_when_old_and_unreferenced(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    job_id = _submit_job_id(client)
    old_orphan = _make_hex_dir(github_env, "aaaaaaaaaaaaaaaa", age_seconds=10_000)
    young = _make_hex_dir(github_env, "bbbbbbbbbbbbbbbb", age_seconds=10)
    old_but_referenced = _make_hex_dir(github_env, "cccccccccccccccc", age_seconds=10_000)
    old_not_hex = github_env.managed / ".tmp" / "old-but-not-ours"
    old_not_hex.mkdir()
    os.utime(old_not_hex, (time.time() - 10_000, time.time() - 10_000))
    # 另一個進行中的註冊 job 記錄了 cccc…：它的 clone 可能正在進行，不能碰
    with Session(db_engine) as session:
        other = job_service.create_job(
            session,
            kind="github_register",
            dedupe_key="register:other/repo",
            settings=github_env.settings,
            params={"github_repo": "other/repo"},
        ).job
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET work_state = CAST(:w AS jsonb) WHERE id = :i"),
            {"w": '{"temp_dir": "cccccccccccccccc"}', "i": other.id},
        )

    assert _run(github_env, job_id, db_engine) == "succeeded"

    assert not old_orphan.exists()
    assert young.exists()
    assert old_but_referenced.exists()
    assert old_not_hex.exists()


# --- worker：取消與租約遺失 ---


def _start_job_in_thread(
    env: GitHubEnv, job_id: int, engine: Engine
) -> tuple[threading.Thread, list[str]]:
    outcome: list[str] = []
    thread = threading.Thread(target=lambda: outcome.append(_run(env, job_id, engine)), daemon=True)
    thread.start()
    return thread, outcome


def _wait_until_cloning(
    server: GitHttpServer, engine: Engine, job_id: int, env: GitHubEnv, timeout: float = 30
) -> str:
    """等到 clone 子程序真的在跑：伺服器收到請求、暫存目錄已記錄且存在。回傳暫存目錄名稱。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        temp = _job_row(engine, job_id)["work_state"].get("temp_dir")
        if server.requests and temp and (env.managed / ".tmp" / temp).exists():
            return str(temp)
        time.sleep(0.05)
    raise AssertionError("the clone never started")


def test_cancelling_a_running_clone_kills_git_and_cleans_up(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    http_server: GitHttpServer,
    db_engine: Engine,
) -> None:
    github_env.settings.github_clone_base_url = http_server.url
    http_server.delay_seconds = 120  # 很慢的遠端：clone 會一直等
    job_id = _submit_job_id(client)
    thread, outcome = _start_job_in_thread(github_env, job_id, db_engine)
    temp = _wait_until_cloning(http_server, db_engine, job_id, github_env)
    assert _job_row(db_engine, job_id)["phase"] == "cloning"

    started = time.monotonic()
    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    thread.join(timeout=60)
    elapsed = time.monotonic() - started

    assert response.status_code == 202
    assert not thread.is_alive()
    assert outcome == ["cancelled"]
    assert elapsed < 20  # 輪詢間隔 0.1 秒＋終止程序樹；遠端要等 120 秒，所以不是等 clone 結束
    state = job_state(db_engine, job_id)
    assert (state["status"], state["attempt"]) == ("cancelled", 1)
    assert not (github_env.managed / ".tmp" / temp).exists()
    assert (_count(db_engine, "repositories"), _temp_children(github_env)) == (0, [])
    assert not _clone_path(github_env).exists()
    assert _advisory_locks(db_engine) == 0


def test_losing_the_lease_during_a_clone_stops_git_and_writes_no_result(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    http_server: GitHttpServer,
    db_engine: Engine,
) -> None:
    github_env.settings.github_clone_base_url = http_server.url
    http_server.delay_seconds = 120
    job_id = _submit_job_id(client)
    thread, outcome = _start_job_in_thread(github_env, job_id, db_engine)
    temp = _wait_until_cloning(http_server, db_engine, job_id, github_env)

    with db_engine.begin() as connection:  # 別的 worker 接手了：令牌換掉
        connection.execute(
            text("UPDATE jobs SET lease_token = gen_random_uuid() WHERE id = :i"), {"i": job_id}
        )
    thread.join(timeout=60)

    assert outcome == ["lease_lost"]
    state = job_state(db_engine, job_id)
    assert state["status"] == "running"  # 舊 worker 沒有寫任何結果
    assert state["result"] is None
    assert not (github_env.managed / ".tmp" / temp).exists()  # 本次建立的暫存目錄仍被清掉
    assert _count(db_engine, "repositories") == 0
    assert _advisory_locks(db_engine) == 0


def test_a_cancel_that_arrives_right_before_publishing_leaves_no_row_and_no_directory(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _submit_job_id(client)
    real = github_service.clone_and_publish

    def cancel_after_publish(*args: Any, **kwargs: Any) -> Any:
        published = real(*args, **kwargs)
        # rename 已完成、資料列尚未寫入：此時使用者取消（job 列鎖在完成交易決定先後）
        with Session(db_engine) as session:
            job_service.cancel_job(session, job_id)
        assert _clone_path(github_env).is_dir()
        return published

    monkeypatch.setattr(github_service, "clone_and_publish", cancel_after_publish)

    assert _run(github_env, job_id, db_engine) == "cancelled"

    assert job_state(db_engine, job_id)["status"] == "cancelled"
    assert _count(db_engine, "repositories") == 0
    assert not _clone_path(github_env).exists()  # 本次 rename 出來的目錄也被清掉
    assert _temp_children(github_env) == []


def test_a_database_failure_while_completing_removes_the_new_directory_and_retries(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _submit_job_id(client)
    calls = {"n": 0}
    real = github_service.new_repository_row

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("INSERT", {}, Exception("connection lost"))
        return real(*args, **kwargs)

    monkeypatch.setattr(github_service, "new_repository_row", flaky)

    assert _run(github_env, job_id, db_engine) == "retry_scheduled"

    state = job_state(db_engine, job_id)
    assert state["error_code"] == "database_unavailable"
    assert _count(db_engine, "repositories") == 0
    assert not _clone_path(github_env).exists()
    assert _temp_children(github_env) == []
    assert _run_after_defer(github_env, job_id, db_engine) == "succeeded"
    assert len(_repositories(db_engine)) == 1


def test_an_unregistered_destination_is_never_deleted_by_a_registration_job(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    orphan = _clone_path(github_env)
    orphan.mkdir(parents=True)
    (orphan / "precious.txt").write_text("keep", encoding="utf-8")
    job_id = _submit_job_id(client)

    assert _run(github_env, job_id, db_engine) == "failed"

    assert job_state(db_engine, job_id)["error_code"] == "managed_storage_error"
    assert (orphan / "precious.txt").read_text(encoding="utf-8") == "keep"
    assert (_count(db_engine, "repositories"), _temp_children(github_env)) == (0, [])


def test_bytes_received_follows_the_temp_dir_size_and_never_decreases(
    client: TestClient, github_env: GitHubEnv, db_engine: Engine
) -> None:
    """`bytes_received`：poll 以暫存目錄大小回報，只增不減（沿用 update_progress 的單調）。"""
    job_id = _submit_job_id(client)
    settings = github_env.settings.model_copy(update={"clone_progress_seconds": 0.0})
    with Session(db_engine) as session:
        job = job_service.acquire_lease(session, job_id, settings, now=at(0))
        assert job is not None and job.lease_token is not None

        class Beat:
            def checkpoint(self) -> None:
                return None

        directory = github_env.managed / "measured"
        directory.mkdir()
        (directory / "a.bin").write_bytes(b"x" * 1000)
        poll = github_job_runner._make_poll(session, job_id, job.lease_token, Beat(), settings)

        poll(directory)
        first = job_state_bytes(db_engine, job_id)
        (directory / "b.bin").write_bytes(b"y" * 500)
        poll(directory)
        second = job_state_bytes(db_engine, job_id)
        (directory / "a.bin").unlink()  # 目錄變小（例如 git 清理暫存檔）
        poll(directory)
        third = job_state_bytes(db_engine, job_id)

    assert (first, second, third) == (1000, 1500, 1500)


def job_state_bytes(engine: Engine, job_id: int) -> int | None:
    value = scalar(engine, "SELECT bytes_received FROM jobs WHERE id = :i", i=job_id)
    return None if value is None else int(value)


# --- worker：fetch ---


def test_a_fetch_job_updates_refs_and_the_new_commit_can_be_imported(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    new_sha = remote.commit({"src/new.py": "y = 2\n"}, message="new work")
    fetch_id = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])

    assert _run(github_env, fetch_id, db_engine) == "succeeded"

    state = job_state(db_engine, fetch_id)
    assert (state["status"], state["phase"]) == ("succeeded", "completed")
    result = state["result"]
    assert result["repository_id"] == repository_id
    assert result["truncated"] is False
    assert [(c["name"], c["change"], c["new_sha"]) for c in result["updated_refs"]] == [
        ("origin/main", "updated", new_sha)
    ]
    assert result["changed_refs"] == 1
    # 取得的 commit 可以匯入（固定版本匯入的 revision 解析 `origin/main`）
    imported = client.post(
        f"/api/v1/repositories/{repository_id}/imports", json={"revision": "origin/main"}
    )
    assert imported.status_code == 201
    assert imported.json()["commit_sha"] == new_sha
    assert _advisory_locks(db_engine) == 0


def test_a_fetch_job_with_nothing_new_reports_no_changes(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    fetch_id = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])

    assert _run(github_env, fetch_id, db_engine) == "succeeded"

    assert job_state(db_engine, fetch_id)["result"]["updated_refs"] == []


def test_fetch_failure_is_permanent_and_keeps_the_clone(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    head = git(_clone_path(github_env), "rev-parse", "HEAD")
    remove_tree(remote.bare)  # 遠端消失
    fetch_id = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])

    assert _run(github_env, fetch_id, db_engine) == "failed"

    assert job_state(db_engine, fetch_id)["error_code"] == "github_fetch_failed"
    assert git(_clone_path(github_env), "rev-parse", "HEAD") == head
    assert _count(db_engine, "repositories") == 1


def test_a_fetch_timeout_is_transient_with_a_helpful_summary(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    http_server: GitHttpServer,
    db_engine: Engine,
) -> None:
    # clone 的 origin 記著註冊當時的遠端網址（也決定 GIT_ALLOW_PROTOCOL）：先經由伺服器註冊
    github_env.settings.github_clone_base_url = http_server.url
    repository_id = _register_through_a_job(github_env, client, db_engine)
    github_env.settings.github_clone_timeout_seconds = 2
    http_server.delay_seconds = 60
    fetch_id = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])

    assert _run(github_env, fetch_id, db_engine) == "retry_scheduled"

    state = job_state(db_engine, fetch_id)
    assert state["error_code"] == "git_timeout"
    assert "GITHUB_CLONE_TIMEOUT_SECONDS" in state["error_summary"]
    assert _advisory_locks(db_engine) == 0


def test_cancelling_a_running_fetch_stops_git(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    http_server: GitHttpServer,
    db_engine: Engine,
) -> None:
    github_env.settings.github_clone_base_url = http_server.url  # 先經由伺服器註冊（見上）
    repository_id = _register_through_a_job(github_env, client, db_engine)
    http_server.delay_seconds = 120
    fetch_id = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])
    baseline = len(http_server.requests)  # 註冊時已有請求；要等 fetch 自己的請求
    thread, outcome = _start_job_in_thread(github_env, fetch_id, db_engine)
    wait_until(lambda: len(http_server.requests) > baseline, "fetch request")

    client.post(f"/api/v1/jobs/{fetch_id}/cancel")
    thread.join(timeout=60)

    assert outcome == ["cancelled"]
    assert job_state(db_engine, fetch_id)["status"] == "cancelled"
    assert _advisory_locks(db_engine) == 0
    # 取消不損壞 clone：之後的 fetch 可以成功
    http_server.delay_seconds = 0
    again = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])
    assert _run(github_env, again, db_engine) == "succeeded"


def wait_until(predicate: Any, what: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# --- 互斥：移除 repo 與進行中的 job ---


def test_removing_a_repository_is_refused_while_a_job_is_queued_or_running(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    fetch_id = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])  # queued

    queued = client.delete(f"/api/v1/repositories/{repository_id}")

    assert queued.status_code == 409
    assert queued.json()["error"]["code"] == "repository_busy"
    assert _clone_path(github_env).is_dir()
    assert _count(db_engine, "repositories") == 1

    with Session(db_engine) as session:  # running：取得租約但還沒做完
        job = job_service.acquire_lease(session, fetch_id, github_env.settings)
        assert job is not None and job.lease_token is not None
        token = job.lease_token
    running = client.delete(f"/api/v1/repositories/{repository_id}")
    assert running.status_code == 409
    assert running.json()["error"]["code"] == "repository_busy"

    with Session(db_engine) as session:  # 工作結束（失敗）後可以移除
        job_service.fail_job(
            session, fetch_id, token, github_env.settings, error_code="github_fetch_failed"
        )
    removed = client.delete(f"/api/v1/repositories/{repository_id}")
    assert removed.status_code == 200
    assert removed.json()["removed_clone"] is True
    assert not _clone_path(github_env).exists()


def test_a_finished_job_does_not_block_removal_and_is_deleted_with_the_repository(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)

    response = client.delete(f"/api/v1/repositories/{repository_id}")

    assert response.status_code == 200
    assert _count(db_engine, "repositories") == 0
    # 註冊 job 的 repository_id 指向被刪的 repo，隨 ON DELETE CASCADE 一併刪除
    assert _count(db_engine, "jobs") == 0


def test_an_unrelated_active_job_does_not_block_removing_another_repository(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    github_env.fake.add_repo("octocat/Other")
    _submit_job_id(client, "octocat/Other")  # queued、repository_id 為 NULL

    response = client.delete(f"/api/v1/repositories/{repository_id}")

    assert response.status_code == 200


def test_removal_is_refused_while_a_fetch_holds_the_repo_lock_even_without_a_job(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    # 同步 fetch（沒有 job）持有的是同一把獨占鎖
    repository_id = _register_through_a_job(github_env, client, db_engine)
    held = acquire_advisory_lock(db_engine, repo_lock_key("github:octocat/hello-world"))
    assert held is not None
    try:
        response = client.delete(f"/api/v1/repositories/{repository_id}")
    finally:
        held.release()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "repository_busy"
    assert client.delete(f"/api/v1/repositories/{repository_id}").status_code == 200


def test_removal_waits_out_a_clone_in_another_connection_then_succeeds(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    http_server: GitHttpServer,
    db_engine: Engine,
) -> None:
    """fetch job 在 clone 目錄上執行時（持有獨占鎖），移除回 409；fetch 結束後可移除。"""
    github_env.settings.github_clone_base_url = http_server.url  # 先經由伺服器註冊
    repository_id = _register_through_a_job(github_env, client, db_engine)
    http_server.delay_seconds = 120
    fetch_id = int(client.post(_fetch_url(repository_id)).json()["job"]["id"])
    baseline = len(http_server.requests)  # 註冊時已有請求；要等 fetch 自己的請求
    thread, outcome = _start_job_in_thread(github_env, fetch_id, db_engine)
    wait_until(lambda: len(http_server.requests) > baseline, "fetch request")

    during = client.delete(f"/api/v1/repositories/{repository_id}")
    client.post(f"/api/v1/jobs/{fetch_id}/cancel")
    thread.join(timeout=60)
    after = client.delete(f"/api/v1/repositories/{repository_id}")

    assert during.status_code == 409
    assert outcome == ["cancelled"]
    assert after.status_code == 200
    assert not _clone_path(github_env).exists()


# --- 互斥：同步端點與 worker 共用同一把鎖 ---


def test_a_synchronous_registration_waits_for_the_lock_then_sees_the_registered_row(
    client: TestClient,
    remote: RemoteRepo,
    github_env: GitHubEnv,
    db_engine: Engine,
) -> None:
    app = create_app(github_env.settings)
    app.dependency_overrides[get_github_client] = github_env.fake.client
    held = acquire_advisory_lock(db_engine, repo_lock_key("github:octocat/hello-world"))
    assert held is not None  # worker 正在 clone 同一個 repo
    results: list[Any] = []

    with TestClient(app) as sync_client:
        thread = threading.Thread(
            target=lambda: results.append(
                sync_client.post("/api/v1/repositories/github", json={"repo": REPO})
            ),
            daemon=True,
        )
        thread.start()
        wait_until(lambda: github_env.fake.requests != [], "the sync request to reach GitHub")
        time.sleep(1.0)  # 讓它進入等鎖（不是同步依據：下面以「放鎖前沒有結果」確認）
        assert results == []  # 同步端點在等鎖，沒有 clone、沒有回應
        assert _count(db_engine, "repositories") == 0
        held.release()
        thread.join(timeout=60)

    assert results[0].status_code == 201
    assert _count(db_engine, "repositories") == 1


def test_a_synchronous_fetch_gives_up_with_409_when_the_lock_stays_busy(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    repository_id = _register_through_a_job(github_env, client, db_engine)
    settings = github_env.settings.model_copy(update={"github_clone_timeout_seconds": 1})
    held = acquire_advisory_lock(db_engine, repo_lock_key("github:octocat/hello-world"))
    assert held is not None
    try:
        with TestClient(create_app(settings)) as short:
            response = short.post(f"/api/v1/repositories/{repository_id}/fetch")
    finally:
        held.release()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "repository_busy"


def test_a_registration_job_and_a_synchronous_registration_race_to_one_repository(
    client: TestClient, remote: RemoteRepo, github_env: GitHubEnv, db_engine: Engine
) -> None:
    job_id = _submit_job_id(client)
    app = create_app(github_env.settings)
    app.dependency_overrides[get_github_client] = github_env.fake.client
    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def worker() -> None:
        barrier.wait()
        outcomes["job"] = _run(github_env, job_id, db_engine)

    def sync() -> None:
        with TestClient(app) as sync_client:
            barrier.wait()
            outcomes["sync"] = sync_client.post("/api/v1/repositories/github", json={"repo": REPO})

    threads = [threading.Thread(target=worker), threading.Thread(target=sync)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    # 只有一個真正 clone：worker 取不到鎖時被延後（重跑時發現已註冊），或晚到的同步端點看到資料列
    assert len(_repositories(db_engine)) == 1
    assert outcomes["sync"].status_code in (200, 201)
    assert outcomes["job"] in ("succeeded", "deferred")
    if outcomes["job"] == "deferred":
        assert _run_after_defer(github_env, job_id, db_engine) == "succeeded"
        assert job_state(db_engine, job_id)["result"]["already_registered"] is True
    assert _temp_children(github_env) == []
    assert [p.name for p in (github_env.managed / "github" / "octocat").iterdir()] == [
        "Hello-World"
    ]
    assert _advisory_locks(db_engine) == 0


def test_the_lock_wait_helper_reports_a_conflict_while_another_connection_holds_it(
    db_engine: Engine,
) -> None:
    key = repo_lock_key("github:wait/test")
    held = acquire_advisory_lock(db_engine, key)
    assert held is not None
    sleeps: list[float] = []
    clock = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    try:
        none = acquire_advisory_lock_wait(
            db_engine, key, timeout=2.0, sleep=sleeps.append, monotonic=lambda: next(clock)
        )
    finally:
        held.release()
    assert none is None
    assert sleeps  # 有等過，而不是第一次失敗就放棄

    got = acquire_advisory_lock_wait(db_engine, key, timeout=1.0)
    assert got is not None
    got.release()


def test_job_params_cannot_carry_a_secret_looking_key(
    client: TestClient, db_engine: Engine, github_env: GitHubEnv
) -> None:
    # params 由 API 組成（不含使用者的原始字串），資料庫層仍拒絕疑似秘密的鍵名
    with Session(db_engine) as session, pytest.raises(ValueError, match="secret"):
        job_service.create_job(
            session,
            kind="github_register",
            dedupe_key="register:x/y",
            settings=github_env.settings,
            params={"github_repo": "x/y", "token": FAKE_TOKEN},
        )
    assert _count(db_engine, "jobs") == 0
