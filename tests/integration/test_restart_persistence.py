"""切片 6：重啟服務後資料仍存在（驗收條件）。

以真實的 `uvicorn` 子程序連到 `db-test` 容器內**臨時建立**的資料庫（測試結束即 DROP；
絕不使用開發資料庫），流程：啟動 → 註冊 repo → 匯入 → 記錄所有可觀察的資料 →
停止行程（確認行程與連接埠都已結束）→ 重新啟動 → 逐項比對。伺服器使用空閒埠、獨立的
工作目錄（不讀取專案的 `.env`），只終止本測試啟動的行程。

此檔的測試較慢（每次啟動 uvicorn 數秒），以 `slow` marker 標記；預設仍會執行，
只想跑快速測試時使用 `uv run pytest -m "not slow"`。
"""

import contextlib
import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
from alembic import command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Repository, Snapshot, SnapshotFile
from tests.git_helpers import commit_files, git, init_repo
from tests.integration.conftest import alembic_config

pytestmark = pytest.mark.slow

READY_TIMEOUT_SECONDS = 40
STOP_TIMEOUT_SECONDS = 20


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _port_accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class ServerProcess:
    """以 `python -m uvicorn repopilot.main:create_app --factory` 啟動的後端子程序。

    只操作自己啟動的 PID：Windows 以 `taskkill /T /PID <pid>` 結束該程序樹（uv 的 venv 以啟動器
    程序再啟動真正的直譯器，只殺最外層會留下仍占著連接埠的子程序），POSIX 以自己的 session 結束。
    """

    def __init__(self, database_url: str, roots: Path, workdir: Path, log_path: Path) -> None:
        self._env = self._build_env(database_url, roots)
        self._workdir = workdir
        self._log_path = log_path
        self.port = 0
        self.process: subprocess.Popen[bytes] | None = None

    @staticmethod
    def _build_env(database_url: str, roots: Path) -> dict[str, str]:
        owned = {name.upper() for name in Settings.model_fields}
        env = {key: value for key, value in os.environ.items() if key.upper() not in owned}
        env.update({"DATABASE_URL": database_url, "REPOSITORY_ROOTS": str(roots)})
        env["PYTHONUNBUFFERED"] = "1"
        return env

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        assert self.process is None, "每個 ServerProcess 只啟動一次"
        self.port = _free_port()
        group: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        with self._log_path.open("ab") as log:
            self.process = subprocess.Popen(  # noqa: S603 - 參數陣列、shell=False
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "repopilot.main:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                ],
                cwd=self._workdir,
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **group,
            )
        self._wait_until_ready()

    def _wait_until_ready(self) -> None:
        assert self.process is not None
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        with httpx.Client(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    pytest.fail(
                        f"uvicorn 提前結束（exit {self.process.returncode}）：{self._tail()}"
                    )
                try:
                    if client.get(f"{self.base_url}/ready").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
        self.kill()
        pytest.fail(f"uvicorn 在 {READY_TIMEOUT_SECONDS} 秒內沒有通過 /ready：{self._tail()}")

    def _tail(self) -> str:
        return self._log_path.read_text(encoding="utf-8", errors="replace")[-1500:]

    def stop(self, *, graceful: bool) -> bool:
        """停止行程並確認它與連接埠都已結束；回傳是否為正常關閉（未動用強制結束）。"""
        process = self.process
        assert process is not None
        exited_by_itself = False
        if graceful and process.poll() is None:
            # Windows：CTRL_BREAK 送給自己的行程群組；uvicorn 把它當成關閉訊號（觸發 lifespan 結束）
            process.send_signal(
                signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
            )
            try:
                process.wait(timeout=STOP_TIMEOUT_SECONDS)
                exited_by_itself = True
            except subprocess.TimeoutExpired:
                pass
        self.kill()
        self._wait_for_port_release()
        return exited_by_itself

    def kill(self) -> None:
        """結束本測試啟動的行程樹（不依名稱、只用自己的 PID）。"""
        process = self.process
        if process is None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        with contextlib.suppress(OSError):
            process.kill()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)

    def _wait_for_port_release(self) -> None:
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while _port_accepts_connections(self.port):
            if time.monotonic() > deadline:
                pytest.fail(f"行程已停止但連接埠 {self.port} 仍可連線：後端沒有真的結束")
            time.sleep(0.2)

    def __enter__(self) -> "ServerProcess":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.kill()  # 保險：測試失敗時也不留下行程


@pytest.fixture
def temp_database_url(test_database_url: str) -> Iterator[str]:
    """在 db-test 容器內建立臨時資料庫並升級到 head，結束時 DROP；不碰其他資料庫。"""
    base = make_url(test_database_url)
    name = f"repopilot_restart_test_{uuid.uuid4().hex[:12]}"
    assert "test" in name
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _get(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text[:300]}"
    return response.json()


def _capture(
    client: httpx.Client, repository_ids: list[int], snapshot_ids: list[int]
) -> dict[str, Any]:
    """記錄服務對外可觀察的所有資料：repo、refs、commits、overview、快照詳情、檔案列表與每個檔案內容。"""
    state: dict[str, Any] = {"repositories": _get(client, "/api/v1/repositories")}
    for repository_id in repository_ids:
        prefix = f"/api/v1/repositories/{repository_id}"
        state[f"repo/{repository_id}"] = _get(client, prefix)
        state[f"refs/{repository_id}"] = _get(client, f"{prefix}/refs")
        state[f"commits/{repository_id}"] = _get(client, f"{prefix}/commits?limit=100")
        state[f"overview/{repository_id}"] = _get(client, f"{prefix}/overview")
        state[f"snapshots/{repository_id}"] = _get(client, f"{prefix}/snapshots")
    for snapshot_id in snapshot_ids:
        prefix = f"/api/v1/snapshots/{snapshot_id}"
        state[f"snapshot/{snapshot_id}"] = _get(client, prefix)
        listing = _get(client, f"{prefix}/files?limit=200")
        state[f"files/{snapshot_id}"] = listing
        for item in listing["items"]:
            state[f"file/{snapshot_id}/{item['id']}"] = _get(client, f"{prefix}/files/{item['id']}")
    return state


def _sha256(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("graceful", [True, False], ids=["graceful-shutdown", "forced-kill"])
def test_data_survives_a_service_restart(
    test_database_url: str,
    temp_database_url: str,
    tmp_path: Path,
    graceful: bool,
) -> None:
    roots = tmp_path / "repos"
    roots.mkdir()
    workdir = tmp_path / "workdir"  # 子程序的工作目錄：確保不會讀到專案根目錄的 .env
    workdir.mkdir()
    crlf = b"line1\r\nline2\r\n"
    repo = init_repo(
        roots / "demo",
        files={"README.md": "# demo\n", "src/app.py": "x = 1\n", "文件/說明 檔.md": "中文內容\n"},
    )
    first_sha = commit_files(
        repo, {"crlf.txt": crlf, "empty.txt": b"", "blob.bin": b"\x00\x01\x02"}, message="second"
    )
    head_sha = commit_files(repo, {"src/app.py": "x = 2\n"}, message="third")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")  # 髒工作目錄
    other_repo = init_repo(roots / "other")
    old_sha = git(repo, "rev-parse", "HEAD~2")
    assert first_sha != head_sha

    # --- 第一次啟動：註冊、匯入，記錄資料 ---
    first = ServerProcess(temp_database_url, roots, workdir, tmp_path / "server-1.log")
    with first:
        first.start()
        with httpx.Client(base_url=first.base_url, trust_env=False, timeout=60) as client:
            repository = client.post("/api/v1/repositories", json={"path": str(repo)})
            other = client.post("/api/v1/repositories", json={"path": str(other_repo)})
            assert (repository.status_code, other.status_code) == (201, 201)
            repository_id, other_id = repository.json()["id"], other.json()["id"]
            old = client.post(
                f"/api/v1/repositories/{repository_id}/imports", json={"revision": old_sha}
            )
            head = client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
            assert (old.status_code, head.status_code) == (201, 201)
            snapshot_ids = [old.json()["id"], head.json()["id"]]
            before = _capture(client, [repository_id, other_id], snapshot_ids)
        first_pid = first.process.pid if first.process else 0
        first_port = first.port
        shut_down_cleanly = first.stop(graceful=graceful)
        assert first.process is not None and first.process.poll() is not None
    if graceful:
        assert shut_down_cleanly, f"uvicorn 沒有在 {STOP_TIMEOUT_SECONDS} 秒內正常關閉"

    # 確認資料已經在資料庫（不是在行程記憶體）：直接查臨時資料庫
    engine = create_engine(temp_database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Repository)) == 2
            assert session.scalar(select(func.count()).select_from(Snapshot)) == 2
            files_in_db = session.scalar(select(func.count()).select_from(SnapshotFile))
    finally:
        engine.dispose()
    assert files_in_db == sum(
        before[f"snapshot/{sid}"]["file_count"] + before[f"snapshot/{sid}"]["skipped_count"]
        for sid in snapshot_ids
    )

    # --- 第二次啟動：全新的行程、全新的連接埠與連線池 ---
    second = ServerProcess(temp_database_url, roots, workdir, tmp_path / "server-2.log")
    with second:
        second.start()
        assert second.process is not None and second.process.pid != first_pid
        assert second.port != first_port or not _port_accepts_connections(first_port)
        with httpx.Client(base_url=second.base_url, trust_env=False, timeout=60) as client:
            after = _capture(client, [repository_id, other_id], snapshot_ids)
            reimport = client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
            assert reimport.status_code == 200  # 去重在重啟後仍然成立
            assert reimport.json()["id"] == snapshot_ids[1]
            snapshots_after = _get(client, f"/api/v1/repositories/{repository_id}/snapshots")
        second.stop(graceful=False)

    assert after == before  # 重啟前後所有可觀察資料逐項相同
    assert snapshots_after["total"] == 2
    assert [r["id"] for r in after["repositories"]["items"]] == [repository_id, other_id]
    head_snapshot = after[f"snapshot/{snapshot_ids[1]}"]
    assert (head_snapshot["status"], head_snapshot["commit_sha"]) == ("completed", head_sha)
    old_snapshot = after[f"snapshot/{snapshot_ids[0]}"]
    assert old_snapshot["commit_sha"] == old_sha
    files = {item["path"]: item for item in after[f"files/{snapshot_ids[1]}"]["items"]}
    assert set(files) == {
        "README.md",
        "src/app.py",
        "文件/說明 檔.md",
        "crlf.txt",
        "empty.txt",
        "blob.bin",
    }  # 髒工作目錄的 dirty.txt 不在其中
    detail = after[f"file/{snapshot_ids[1]}/{files['crlf.txt']['id']}"]
    assert detail["content"] == "line1\r\nline2\r\n"  # 換行原樣保留
    assert detail["sha256"] == hashlib.sha256(crlf).hexdigest()
    app = after[f"file/{snapshot_ids[1]}/{files['src/app.py']['id']}"]
    assert (app["content"], app["sha256"]) == ("x = 2\n", _sha256("x = 2\n"))
    old_files = {item["path"]: item for item in after[f"files/{snapshot_ids[0]}"]["items"]}
    old_app = after[f"file/{snapshot_ids[0]}/{old_files['src/app.py']['id']}"]
    assert old_app["content"] == "x = 1\n"  # 舊 commit 的內容在重啟後仍是舊版本
    binary = after[f"file/{snapshot_ids[1]}/{files['blob.bin']['id']}"]
    assert (binary["status"], binary["skip_reason"], binary["content"]) == (
        "skipped",
        "binary",
        None,
    )
    assert after[f"overview/{repository_id}"]["analyzed"] is True
    assert after[f"commits/{repository_id}"]["total_returned"] == 3
