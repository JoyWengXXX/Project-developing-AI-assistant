"""結構索引完成後補送參考版本的檢索 embedding（issue #26，AC-1～AC-5）。

真實 PostgreSQL＋暫存 Git repo，直接呼叫 `job_runner.run_job`；GPU 可用性以 monkeypatch 取代。
AC-6（真實 worker／開發堆疊）不在這裡。
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.embedding import gateway_client
from repopilot.main import create_app
from repopilot.services import job_runner, references, snapshot_indexing
from tests.git_helpers import init_repo
from tests.integration.jobs.conftest import (
    WorkerEnv,
    job_state,
    register_repo,
    snapshot_row,
    submit_import_job,
)


@pytest.fixture
def embedding_settings(worker_env: WorkerEnv) -> Settings:
    return job_runner.apply_worker_timeouts(
        worker_env.settings.model_copy(
            update={
                "embedding_model_sha256": "a" * 64,
                "embedding_runtime_sha256": "b" * 64,
                "embedding_socket_path": Path("/fake/cpu.sock"),
            }
        )
    )


@pytest.fixture
def client(embedding_settings: Settings, clean_repositories: None, clean_jobs: None) -> TestClient:
    return TestClient(create_app(embedding_settings))


@pytest.fixture
def repository_id(client: TestClient, worker_env: WorkerEnv) -> int:
    repo = init_repo(
        worker_env.roots / "after-index", files={"README.md": "hello\n", "app.py": "x = 1\n"}
    )
    return register_repo(client, repo)


def _devices(monkeypatch: pytest.MonkeyPatch, *, gpu_available: bool) -> None:
    state = {
        "cpu": {"configured": True, "available": True},
        "gpu": {"configured": gpu_available, "available": gpu_available},
    }
    monkeypatch.setattr(gateway_client, "embedding_devices", lambda settings: state)


def _sync_snapshot(client: TestClient, repository_id: int) -> int:
    """同步匯入：快照 completed，但還沒有結構索引。"""
    response = client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _set_reference(client: TestClient, repository_id: int, snapshot_id: int) -> dict[str, Any]:
    response = client.put(
        f"/api/v1/repositories/{repository_id}/reference", json={"snapshot_id": snapshot_id}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()["embedding"]
    return body


def _index_job(client: TestClient, snapshot_id: int) -> int:
    response = client.post(f"/api/v1/snapshots/{snapshot_id}/index-jobs")
    assert response.status_code == 202, response.text
    return int(response.json()["job"]["id"])


def _retrieval_jobs(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, snapshot_id, status, params FROM jobs "
                "WHERE kind = 'snapshot_retrieval' ORDER BY id"
            )
        )
        return [dict(row._mapping) for row in rows]


def test_ac1_reference_set_while_indexing_gets_a_gpu_job_when_the_index_job_completes(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _sync_snapshot(client, repository_id)
    pending = _set_reference(client, repository_id, snapshot_id)
    assert pending["reason"] == "structure_index_pending"
    assert _retrieval_jobs(db_engine) == []

    job_id = _index_job(client, snapshot_id)
    assert job_runner.run_job(job_id, db_engine, embedding_settings) == "succeeded"

    (retrieval,) = _retrieval_jobs(db_engine)
    assert retrieval["snapshot_id"] == snapshot_id
    assert retrieval["params"]["device"] == "gpu"
    assert retrieval["status"] == "queued"


def test_ac1_an_import_job_also_triggers_the_follow_up_and_falls_back_to_cpu(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=False)
    snapshot_id = _sync_snapshot(client, repository_id)
    _set_reference(client, repository_id, snapshot_id)

    # 快照 completed、索引缺少 → 匯入 job 只做索引階段（規格 4.4）
    job_id = int(submit_import_job(client, repository_id).json()["job"]["id"])
    assert job_runner.run_job(job_id, db_engine, embedding_settings) == "succeeded"

    (retrieval,) = _retrieval_jobs(db_engine)
    assert retrieval["snapshot_id"] == snapshot_id
    assert retrieval["params"]["device"] == "cpu"


def test_ac1_a_fresh_import_job_of_the_reference_snapshot_queues_after_indexing(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """匯入 job 先發布快照、再索引：在索引階段之前設定參考版本（以掛鉤模擬發布後的瞬間）。"""
    _devices(monkeypatch, gpu_available=True)
    job_id = int(submit_import_job(client, repository_id).json()["job"]["id"])
    real = snapshot_indexing.build_index

    def reference_then_index(session: Session, snapshot_id: int, *args: Any, **kwargs: Any) -> Any:
        with db_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"
                ),
                {"r": repository_id, "s": snapshot_id},
            )
        return real(session, snapshot_id, *args, **kwargs)

    monkeypatch.setattr(snapshot_indexing, "build_index", reference_then_index)
    assert job_runner.run_job(job_id, db_engine, embedding_settings) == "succeeded"

    (retrieval,) = _retrieval_jobs(db_engine)
    assert retrieval["snapshot_id"] == job_state(db_engine, job_id)["snapshot_id"]


def test_ac2_a_non_reference_snapshot_queues_nothing(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _sync_snapshot(client, repository_id)

    job_id = _index_job(client, snapshot_id)
    assert job_runner.run_job(job_id, db_engine, embedding_settings) == "succeeded"

    assert _retrieval_jobs(db_engine) == []


def test_ac3_an_existing_retrieval_job_is_not_duplicated(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _sync_snapshot(client, repository_id)
    _set_reference(client, repository_id, snapshot_id)
    job_id = _index_job(client, snapshot_id)
    assert job_runner.run_job(job_id, db_engine, embedding_settings) == "succeeded"
    (first,) = _retrieval_jobs(db_engine)

    # 補送流程再執行一次（進行中的 job 已存在）：不建立新 job，裝置也不變
    _devices(monkeypatch, gpu_available=False)
    with db_engine.connect() as connection:
        session = Session(bind=connection)
        references.enqueue_embedding_after_structure_index(session, snapshot_id, embedding_settings)

    (again,) = _retrieval_jobs(db_engine)
    assert again["id"] == first["id"] and again["params"]["device"] == "gpu"


def test_ac4_a_failed_index_queues_nothing(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _sync_snapshot(client, repository_id)
    _set_reference(client, repository_id, snapshot_id)
    job_id = _index_job(client, snapshot_id)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(snapshot_indexing, "_write_batch", broken)
    assert job_runner.run_job(job_id, db_engine, embedding_settings) == "retry_scheduled"

    assert snapshot_row(db_engine, snapshot_id)[0] == "completed"
    assert _retrieval_jobs(db_engine) == []


def test_ac4_a_cancelled_index_queues_nothing(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _sync_snapshot(client, repository_id)
    _set_reference(client, repository_id, snapshot_id)
    job_id = _index_job(client, snapshot_id)
    real = snapshot_indexing._write_batch

    def cancel_then_write(*args: Any, **kwargs: Any) -> Any:
        written = real(*args, **kwargs)
        client.post(f"/api/v1/jobs/{job_id}/cancel")
        return written

    monkeypatch.setattr(snapshot_indexing, "_write_batch", cancel_then_write)
    assert job_runner.run_job(job_id, db_engine, embedding_settings) == "cancelled"

    assert snapshot_row(db_engine, snapshot_id)[0] == "completed"
    assert _retrieval_jobs(db_engine) == []


def test_ac5_unconfigured_embedding_is_skipped_and_the_job_still_succeeds(
    worker_env: WorkerEnv,
    clean_repositories: None,
    clean_jobs: None,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unconfigured = job_runner.apply_worker_timeouts(
        worker_env.settings.model_copy(
            update={"embedding_model_sha256": None, "embedding_runtime_sha256": None}
        )
    )  # 不受本機 .env 影響
    _devices(monkeypatch, gpu_available=True)
    client = TestClient(create_app(unconfigured))
    repository_id = register_repo(
        client, init_repo(worker_env.roots / "unconfigured-after", files={"a.py": "x = 1\n"})
    )
    snapshot_id = _sync_snapshot(client, repository_id)
    assert _set_reference(client, repository_id, snapshot_id)["reason"] == (
        "embedding_not_configured"
    )

    job_id = _index_job(client, snapshot_id)

    assert job_runner.run_job(job_id, db_engine, unconfigured) == "succeeded"
    assert job_state(db_engine, job_id)["status"] == "succeeded"
    assert snapshot_row(db_engine, snapshot_id)[0] == "completed"
    assert _retrieval_jobs(db_engine) == []


def test_ac5_a_failing_follow_up_is_logged_and_does_not_fail_the_job(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    embedding_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _sync_snapshot(client, repository_id)
    _set_reference(client, repository_id, snapshot_id)
    job_id = _index_job(client, snapshot_id)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(references, "submit_with_gpu_preference", explode)
    with caplog.at_level("WARNING"):
        outcome = job_runner.run_job(job_id, db_engine, embedding_settings)

    assert outcome == "succeeded"
    assert job_state(db_engine, job_id)["phase"] == "completed"
    assert snapshot_row(db_engine, snapshot_id)[0] == "completed"
    assert _retrieval_jobs(db_engine) == []
    assert any(r.message == "reference_embedding_after_index_failed" for r in caplog.records)
