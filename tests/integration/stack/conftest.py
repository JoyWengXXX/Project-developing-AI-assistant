"""`stack` 測試的共用 fixture：真實 PostgreSQL＋Redis＋worker＋dispatcher 容器（規格 9.4、11.1）。

**整個 package 期間**才啟動 `redis-test`、`dispatcher-test`、`worker-test`，結束後停止
（package 範圍的 fixture）：整合測試其餘部分會建立假 job，若這些容器在跑，真正的 worker 會去
執行它們。`db_engine`（session 範圍）在重建 schema 之前，會先停止仍在執行的 worker／dispatcher
（`tests/stack_control.py`）。

堆疊無法啟動（沒有 Docker、映像未建置、容器不健康）時一律 `pytest.fail` 明確失敗，不跳過
（規則 10、D16）。前置：`docker compose up -d --wait db-test` 與
`docker compose --profile jobs build`。

每個測試開始前 `reset()`：三個服務都在執行且未暫停、Redis 清空、資料表清空、worker 與
dispatcher 心跳在線。測試的 repo 與受管目錄在專案內 `.test-repos\\`、`.test-managed\\`（已在
.gitignore），映射與正式環境相同（Windows 路徑 → `/mnt/repos`、`/mnt/managed`）。租約、掃描週期
在 compose 的測試服務中調得很短（租約 12 秒、心跳 3 秒、掃描 1 秒、重送 15 秒），故障注入測試
才能在數十秒內完成；數值皆未實測。
"""

import json
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from repopilot.config import Settings
from repopilot.main import create_app
from repopilot.path_mapping import PathMapping
from tests.git_helpers import RemoteRepo, init_repo
from tests.integration.jobs.conftest import remove_tree
from tests.stack_control import (
    TEST_MANAGED_DIR,
    TEST_REMOTE_DIR,
    TEST_REPOS_DIR,
    TEST_TOKEN_KEY,
    compose,
)

SERVICES = ("redis-test", "dispatcher-test", "worker-test")
TRUNCATE_ALL = (
    "TRUNCATE jobs, job_dispatches, service_heartbeats, repositories, github_credentials "
    "RESTART IDENTITY CASCADE"
)


def wait_until(
    predicate: Callable[[], Any], what: str, *, timeout: float = 60.0, interval: float = 0.2
) -> Any:
    """輪詢到 `predicate()` 回傳真值（回傳該值）；逾時拋 AssertionError。

    以資料庫／容器狀態為依據，不以 sleep 的長短當成功證據。
    """
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


class FakeRemote:
    """主機端操作假 GitHub 的資料目錄（`.test-remote`，容器內是 `/mnt/remote`）。

    bare repo 由主機的 git 建立與推送；`api.json`（GitHub API 的回應）與 `control.json`
    （慢速、切斷、要求認證…）由測試寫入，容器內的假伺服器（tests/fake_remote）每個請求都重新讀取。
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.git_dir = directory / "git"
        self._repos: dict[str, dict[str, Any]] = {}

    def add_repo(
        self,
        owner: str,
        name: str,
        files: dict[str, str] | None = None,
        *,
        private: bool = False,
        bare: bool = True,
    ) -> RemoteRepo | None:
        """登錄 GitHub API 的回應；`bare=False` 時只登錄 API（Git 遠端不存在）。"""
        self._repos[f"{owner}/{name}".lower()] = {
            "full_name": f"{owner}/{name}",
            "private": private,
            "default_branch": "main",
        }
        self._write("api.json", self._repos)
        if not bare:
            return None
        return RemoteRepo(self.git_dir, owner, name, files=files)

    def control(self, **values: Any) -> None:
        """設定假伺服器的行為（整份覆寫）；不帶參數＝恢復正常。"""
        self._write("control.json", values)

    def requests(self) -> list[dict[str, Any]]:
        path = self.directory / "requests.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _write(self, name: str, payload: Any) -> None:
        temp = self.directory / f"{name}.tmp"
        temp.write_text(json.dumps(payload), encoding="utf-8")
        temp.replace(self.directory / name)

    def clear(self) -> None:
        for child in self.directory.iterdir():
            if child.name == "server.ready":
                continue
            if child.is_dir():
                remove_tree(child)
            else:
                child.unlink(missing_ok=True)


class Stack:
    def __init__(self, engine: Engine, database_url: str) -> None:
        self.engine = engine
        repos, managed = TEST_REPOS_DIR.resolve(), TEST_MANAGED_DIR.resolve()
        self.repos_dir, self.managed_dir = repos, managed
        self.remote = FakeRemote(TEST_REMOTE_DIR.resolve())
        self.api_settings = Settings(
            database_url=SecretStr(database_url),
            repository_roots=[repos],
            managed_repos_dir=managed,
            worker_path_mappings=[
                PathMapping(host=str(repos), container="/mnt/repos", mode="ro"),
                PathMapping(host=str(managed), container="/mnt/managed", mode="rw"),
            ],
            # 與 docker-compose.yml 的測試服務一致（API 端的 /jobs/status 與重試額度用到）
            job_lease_seconds=12,
            job_heartbeat_seconds=3,
            job_retry_base_seconds=1,
            job_retry_max_seconds=4,
            service_heartbeat_seconds=3,
            # 與 worker-test 同一把金鑰：測試用它把假 token 加密保存，worker 在容器內解密
            token_encryption_key=SecretStr(TEST_TOKEN_KEY),
        )

    # --- compose 操作 ---

    def start(self, *services: str) -> None:
        result = compose("start", *services, check=False)
        assert result.returncode == 0, result.stderr[-2000:]

    def stop(self, *services: str) -> None:
        compose("stop", *services)

    def kill(self, service: str) -> None:
        compose("kill", service)  # SIGKILL：模擬崩潰，沒有任何清理機會

    def pause(self, service: str) -> None:
        compose("pause", service)

    def unpause(self, service: str) -> None:
        compose("unpause", service, check=False)  # 沒有暫停時會失敗，無妨

    def state(self, service: str) -> str:
        result = compose("ps", "-a", "--format", "{{.State}}", service, check=False)
        return result.stdout.strip()

    def logs(self, service: str) -> str:
        return compose("logs", "--no-log-prefix", service, check=False).stdout

    def flush_redis(self) -> None:
        compose("exec", "-T", "redis-test", "redis-cli", "flushall")

    # --- 狀態 ---

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar()

    def db_now(self) -> Any:
        return self.scalar("SELECT clock_timestamp()")

    def wait_online(self, since: Any | None = None, timeout: float = 90.0) -> None:
        """worker 與 dispatcher 的心跳都比 `since` 新（沒給就用現在），代表兩者確實在寫心跳。"""
        marker = since if since is not None else self.db_now()

        def online() -> bool:
            count = self.scalar(
                "SELECT count(DISTINCT service) FROM service_heartbeats WHERE last_seen_at > :m",
                m=marker,
            )
            return int(count) == 2

        wait_until(online, "worker and dispatcher heartbeats", timeout=timeout)

    def reset(self) -> None:
        """每個測試前的已知狀態（含解除上一個測試可能留下的 docker pause）。"""
        for service in SERVICES:
            self.unpause(service)
        marker = self.db_now()
        for service in SERVICES:
            if self.state(service) != "running":
                self.start(service)
        with self.engine.begin() as connection:
            connection.execute(text(TRUNCATE_ALL))
        self.flush_redis()
        # 上一個測試留下的受管目錄與假遠端內容（資料表已清空，目錄不清會讓註冊撞到「已存在」）
        self.clear_managed()
        self.remote.clear()
        self.remote.control()
        self.wait_online(since=marker)

    def clear_managed(self) -> None:
        for child in self.managed_dir.iterdir():
            if child.is_dir() or child.is_junction() or child.is_symlink():
                remove_tree(child)
            else:
                child.unlink(missing_ok=True)

    # --- fixture repo ---

    def new_repo(self, name: str, files: dict[str, str] | None = None) -> Path:
        path = self.repos_dir / name
        if path.exists():
            remove_tree(path)
        return init_repo(path, files=files)


@pytest.fixture(scope="package")
def stack(db_engine: Engine, test_database_url: str) -> Iterator[Stack]:
    try:
        probe: subprocess.CompletedProcess[str] = compose("ps", check=False, timeout=60)
    except FileNotFoundError:
        pytest.fail("找不到 docker；stack 測試需要 Docker Desktop（不跳過）。")
    if probe.returncode != 0:
        pytest.fail(f"docker compose 無法使用：{probe.stderr[-500:]}")
    TEST_REPOS_DIR.mkdir(exist_ok=True)
    TEST_MANAGED_DIR.mkdir(exist_ok=True)
    TEST_REMOTE_DIR.mkdir(exist_ok=True)
    instance = Stack(db_engine, test_database_url)
    with db_engine.begin() as connection:
        connection.execute(text(TRUNCATE_ALL))
    try:
        started = compose("up", "-d", "--wait", "redis-test", check=False)
        if started.returncode != 0:
            pytest.fail(f"redis-test 無法啟動：{started.stderr[-1500:]}")
        instance.flush_redis()
        up = compose(
            "up", "-d", "--wait", "dispatcher-test", "worker-test", check=False, timeout=300
        )
        if up.returncode != 0:
            pytest.fail(
                "worker-test／dispatcher-test 無法啟動或不健康（是否已執行 "
                f"`docker compose --profile jobs build`？）：{up.stderr[-1500:]}"
            )
        yield instance
    finally:
        instance.unpause("redis-test")
        compose("stop", *SERVICES, check=False, timeout=180)
        for child in (TEST_REPOS_DIR, TEST_MANAGED_DIR, TEST_REMOTE_DIR):
            if child.exists():
                for entry in child.iterdir():
                    if entry.is_dir() or entry.is_junction() or entry.is_symlink():
                        remove_tree(entry)
                    else:
                        entry.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _fresh_stack(stack: Stack) -> None:
    stack.reset()


@pytest.fixture
def client(stack: Stack) -> Iterator[TestClient]:
    with TestClient(create_app(stack.api_settings)) as test_client:
        yield test_client


def register(client: TestClient, repo: Path) -> int:
    response = client.post("/api/v1/repositories", json={"path": str(repo)})
    assert response.status_code in (200, 201), response.text
    return int(response.json()["id"])


def submit(client: TestClient, repository_id: int, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/repositories/{repository_id}/import-jobs", json=body)
    assert response.status_code in (200, 202), response.text
    payload: dict[str, Any] = response.json()
    return payload


def wait_for_status(
    client: TestClient, job_id: int, wanted: set[str], *, timeout: float = 90.0
) -> dict[str, Any]:
    def poll() -> dict[str, Any] | None:
        job: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}").json()
        return job if job["status"] in wanted else None

    result: dict[str, Any] = wait_until(poll, f"job {job_id} to reach {wanted}", timeout=timeout)
    return result
