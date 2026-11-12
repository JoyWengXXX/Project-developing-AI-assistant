"""背景工作（第二階段切片 2）整合測試的共用 fixture 與輔助函式。

全部使用真實 PostgreSQL（`db-test`）；不依賴 Redis、Celery、Docker worker。時間以 `now` 參數注入
（固定的 `T0` 之後加秒數），競爭情境以多執行緒＋多連線＋資料庫列鎖重現，並以 `pg_stat_activity`
確認「某條連線確實卡在鎖上」，不以 sleep 當同步機制。
"""

import os
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.db.models import Job
from repopilot.main import create_app
from repopilot.path_mapping import PathMapping
from repopilot.services import jobs as job_service

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    """T0 之後 `seconds` 秒的固定時間（注入給服務函式的 `now`）。"""
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def clean_jobs(db_engine: Engine) -> None:
    # 只清本切片的三張表（名稱含 test 的資料庫由 db_engine 保證）；不動 repositories／snapshots
    with db_engine.begin() as connection:
        connection.execute(
            text("TRUNCATE jobs, job_dispatches, service_heartbeats RESTART IDENTITY CASCADE")
        )


@pytest.fixture
def job_settings(test_database_url: str) -> Settings:
    # 明確指定所有與狀態機有關的設定，不受 .env 影響
    return Settings(
        database_url=SecretStr(test_database_url),
        job_lease_seconds=60,
        job_heartbeat_seconds=15,
        job_max_attempts=3,
        job_retry_base_seconds=30,
        job_retry_max_seconds=600,
        dispatch_resend_after_seconds=300,
        service_heartbeat_seconds=15,
    )


@pytest.fixture
def session_factory(db_engine: Engine, clean_jobs: None) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as db_session:
        yield db_session


def new_job(
    session: Session,
    settings: Settings,
    *,
    key: str = "import:1:abc:def",
    kind: str = "snapshot_import",
    **kwargs: Any,
) -> Job:
    created = job_service.create_job(
        session, kind=kind, dedupe_key=key, settings=settings, now=at(0), **kwargs
    )
    assert created.created
    return created.job


def running_job(
    session: Session, settings: Settings, *, key: str = "import:1:abc:def", **kwargs: Any
) -> tuple[Job, UUID]:
    """建立並取得租約（T0 起）；回傳 (job, 令牌)。"""
    job = new_job(session, settings, key=key, **kwargs)
    acquired = job_service.acquire_lease(session, job.id, settings, now=at(0))
    assert acquired is not None
    assert acquired.lease_token is not None
    return acquired, acquired.lease_token


def row_of(engine: Engine, job_id: int) -> tuple[Any, ...]:
    """以獨立連線讀取 job 的所有欄位（比對「被拒絕的寫入沒有改動任何資料」）。"""
    with engine.connect() as connection:
        return tuple(
            connection.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).one()
        )


def dispatch_rows(engine: Engine, job_id: int) -> list[tuple[int, str, int, int]]:
    """(attempt_target, status, send_count, failure_count)，依 attempt_target 排序。"""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT attempt_target, status, send_count, failure_count FROM job_dispatches "
                "WHERE job_id = :id ORDER BY attempt_target"
            ),
            {"id": job_id},
        )
        return [(r[0], r[1], r[2], r[3]) for r in rows]


def scalar(bind: Engine | Connection, sql: str, **params: Any) -> Any:
    """以獨立連線執行單一查詢並回傳第一欄（傳入 Engine 時每次開新連線，不受測試中交易影響）。"""
    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return connection.execute(text(sql), params).scalar()
    return bind.execute(text(sql), params).scalar()


def run_together(workers: int, task: Callable[[int], Any]) -> list[Any]:
    """讓多個執行緒同時出發（Barrier），回傳各自的結果；例外會在 `result()` 重新拋出。"""
    barrier = threading.Barrier(workers)

    def attempt(index: int) -> Any:
        barrier.wait(timeout=30)
        return task(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(attempt, i) for i in range(workers)]
        return [future.result(timeout=120) for future in futures]


def named_engine(url: str, application_name: str) -> Engine:
    """帶 application_name 的 Engine：讓測試能在 `pg_stat_activity` 找到這條連線。"""
    return create_engine(url, connect_args={"application_name": application_name})


def wait_until_blocked(engine: Engine, application_name: str, timeout: float = 30.0) -> None:
    """等到名為 `application_name` 的連線確實卡在鎖上（`wait_event_type = 'Lock'`）。

    輪詢的是「資料庫狀態」而不是時間：這是確定性的同步點。逾時代表被測程式沒有等鎖（保護失效）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        blocked = scalar(
            engine,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = :name AND wait_event_type = 'Lock'",
            name=application_name,
        )
        if blocked:
            return
        time.sleep(0.02)
    raise AssertionError(f"connection {application_name!r} never blocked on a lock")


# --- 第二階段切片 3：匯入 job、worker 流程的共用 fixture ---


@dataclass
class WorkerEnv:
    """一組一致的測試環境：repo 根、受管目錄，以及「主機路徑＝容器路徑」的映射設定。

    測試在 Windows 主機上執行，沒有真正的容器；映射把 `host` 指到 pytest 暫存目錄、`container`
    指到**同一個目錄**（以 `C:/…` 形式），所以路徑映射與容器端重驗的邏輯照樣被執行，只是「容器端」
    就是本機檔案系統。真正的容器、掛載與 Redis 由 `stack` 測試驗證。
    """

    settings: Settings
    roots: Path
    managed: Path


@pytest.fixture
def worker_env(test_database_url: str, roots: Path, tmp_path: Path) -> WorkerEnv:
    managed = tmp_path / "managed"
    managed.mkdir()
    real_roots, real_managed = roots.resolve(), managed.resolve()
    settings = Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[real_roots],
        managed_repos_dir=real_managed,
        worker_path_mappings=[
            PathMapping(host=str(real_roots), container=real_roots.as_posix(), mode="ro"),
            PathMapping(host=str(real_managed), container=real_managed.as_posix(), mode="rw"),
        ],
        job_lease_seconds=60,
        job_heartbeat_seconds=15,
        job_max_attempts=3,
        job_retry_base_seconds=30,
        job_retry_max_seconds=600,
        service_heartbeat_seconds=15,
    )
    return WorkerEnv(settings, real_roots, real_managed)


@pytest.fixture
def mapped_client(
    worker_env: WorkerEnv, clean_repositories: None, clean_jobs: None
) -> Iterator[TestClient]:
    with TestClient(create_app(worker_env.settings)) as test_client:
        yield test_client


def register_repo(client: TestClient, repo: Path) -> int:
    response = client.post("/api/v1/repositories", json={"path": str(repo)})
    assert response.status_code in (200, 201), response.text
    return int(response.json()["id"])


def submit_import_job(client: TestClient, repository_id: int, **body: Any) -> Any:
    return client.post(f"/api/v1/repositories/{repository_id}/import-jobs", json=body)


def snapshot_row(engine: Engine, snapshot_id: int) -> tuple[str, int, str | None]:
    """(status, file_count, error_summary)。"""
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, file_count, error_summary FROM snapshots WHERE id = :id"),
            {"id": snapshot_id},
        ).one()
    return (row[0], row[1], row[2])


def remove_tree(path: Path) -> None:
    """刪除目錄樹；Git 物件在 Windows 上是唯讀檔，先清除唯讀屬性再刪。

    junction／目錄符號連結只移除連結本身（`os.rmdir`），不跟隨、不動目標。
    """
    if path.is_junction() or path.is_symlink():
        os.rmdir(path)
        return

    def clear_readonly(function: Callable[[str], Any], target: str, _: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onexc=clear_readonly)


def job_state(engine: Engine, job_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, phase, attempt, snapshot_id, error_code, error_summary, "
                "processed_files, total_files, next_attempt_at, lease_token, result "
                "FROM jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).one()
    return dict(row._mapping)
