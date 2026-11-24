"""設定參考版本時排入檢索 embedding（issue #25，AC-1～AC-7）。

真實 PostgreSQL；GPU 與 embedding 以假物件取代。
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from repopilot.config import Settings
from repopilot.embedding import gateway_client
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.indexing.profile import IndexProfile
from repopilot.main import create_app
from repopilot.services import job_runner, retrieval_job_runner
from tests.git_helpers import init_repo
from tests.integration.jobs.conftest import WorkerEnv, register_repo
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files


@pytest.fixture
def settings_with_embedding(worker_env: WorkerEnv) -> Settings:
    return worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/cpu.sock"),
        }
    )


@pytest.fixture
def configured_client(
    settings_with_embedding: Settings, clean_repositories: None, clean_jobs: None
) -> TestClient:
    return TestClient(create_app(settings_with_embedding))


@pytest.fixture
def repository_id(configured_client: TestClient, worker_env: WorkerEnv) -> int:
    repo = init_repo(worker_env.roots / "reference-embedding", files={"a.txt": "a\n"})
    return register_repo(configured_client, repo)


def _devices(monkeypatch: pytest.MonkeyPatch, *, gpu_available: bool) -> None:
    state = {
        "cpu": {"configured": True, "available": True},
        "gpu": {"configured": gpu_available, "available": gpu_available},
    }
    monkeypatch.setattr(gateway_client, "embedding_devices", lambda settings: state)


def _indexed_snapshot(
    engine: Engine,
    repository_id: int,
    settings: Settings,
    *,
    index: int = 1,
    structure: bool = True,
) -> int:
    """已完成的快照；`structure=True` 時一併寫入「目前設定」的已完成結構索引。"""
    snapshot_id = add_snapshot(engine, repository_id, index=index)
    add_files(engine, snapshot_id, {"a.txt": "alpha\n"})
    if structure:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO snapshot_indexes (snapshot_id, index_profile_hash, status) "
                    "VALUES (:s, :p, 'completed')"
                ),
                {"s": snapshot_id, "p": IndexProfile.from_settings(settings).hash()},
            )
    return snapshot_id


def _put(client: TestClient, repository_id: int, snapshot_id: int) -> Any:
    response = client.put(
        f"/api/v1/repositories/{repository_id}/reference", json={"snapshot_id": snapshot_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _job_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT count(*) FROM jobs")) or 0)


def test_ac1_gpu_is_the_default_device(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings_with_embedding: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _indexed_snapshot(db_engine, repository_id, settings_with_embedding)

    embedding = _put(configured_client, repository_id, snapshot_id)["embedding"]

    assert embedding["status"] == "job"
    assert embedding["job"]["kind"] == "snapshot_retrieval"
    assert embedding["job"]["snapshot_id"] == snapshot_id
    assert embedding["device"] == "gpu"
    assert embedding["job"]["params"]["device"] == "gpu"
    assert embedding["device_fallback"] is False
    assert _job_count(db_engine) == 1


@pytest.mark.parametrize("configured", [True, False])
def test_ac2_unavailable_gpu_falls_back_to_cpu_and_says_so(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings_with_embedding: Settings,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    state = {
        "cpu": {"configured": True, "available": True},
        "gpu": {"configured": configured, "available": False},
    }
    monkeypatch.setattr(gateway_client, "embedding_devices", lambda settings: state)
    snapshot_id = _indexed_snapshot(db_engine, repository_id, settings_with_embedding)

    embedding = _put(configured_client, repository_id, snapshot_id)["embedding"]

    assert embedding["status"] == "job"
    assert embedding["device"] == "cpu"
    assert embedding["job"]["params"]["device"] == "cpu"
    assert embedding["device_fallback"] is True
    assert "cpu" in embedding["message"]
    assert _job_count(db_engine) == 1


def test_ac3_missing_embedding_settings_skip_the_job_but_keep_the_reference(
    worker_env: WorkerEnv, clean_repositories: None, clean_jobs: None, db_engine: Engine
) -> None:
    unconfigured = worker_env.settings.model_copy(
        update={"embedding_model_sha256": None, "embedding_runtime_sha256": None}
    )  # 不受本機 .env 影響
    client = TestClient(create_app(unconfigured))
    repo = init_repo(worker_env.roots / "unconfigured", files={"a.txt": "a\n"})
    repository_id = register_repo(client, repo)
    snapshot_id = _indexed_snapshot(db_engine, repository_id, unconfigured)

    body = _put(client, repository_id, snapshot_id)

    assert body["snapshot_id"] == snapshot_id
    assert body["embedding"]["status"] == "skipped"
    assert body["embedding"]["reason"] == "embedding_not_configured"
    assert body["embedding"]["job"] is None and body["embedding"]["device"] is None
    assert body["embedding"]["message"]
    assert _job_count(db_engine) == 0
    assert [item["snapshot_id"] for item in client.get("/api/v1/references").json()["items"]] == [
        snapshot_id
    ]


def test_structure_index_pending_is_reported_and_no_job_is_queued(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings_with_embedding: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _indexed_snapshot(
        db_engine, repository_id, settings_with_embedding, structure=False
    )

    embedding = _put(configured_client, repository_id, snapshot_id)["embedding"]

    assert embedding["status"] == "skipped"
    assert embedding["reason"] == "structure_index_pending"
    assert _job_count(db_engine) == 0


def test_ac4_an_active_job_is_returned_instead_of_a_new_one(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings_with_embedding: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _indexed_snapshot(db_engine, repository_id, settings_with_embedding)

    first = _put(configured_client, repository_id, snapshot_id)["embedding"]
    # 第二次送出時 GPU 已不可用：仍回傳既有 job（其裝置不變）
    _devices(monkeypatch, gpu_available=False)
    again = _put(configured_client, repository_id, snapshot_id)["embedding"]

    assert again["status"] == "job"
    assert again["job"]["id"] == first["job"]["id"]
    assert again["device"] == "gpu"
    assert again["device_fallback"] is False
    assert _job_count(db_engine) == 1


def test_ac5_a_completed_index_is_marked_completed_without_a_new_job(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings_with_embedding: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)

    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            pass

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            return [1.0] + [0.0] * 767

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", FakeEmbedding)
    snapshot_id = _indexed_snapshot(db_engine, repository_id, settings_with_embedding)
    job_id = _put(configured_client, repository_id, snapshot_id)["embedding"]["job"]["id"]
    # worker 以 GPU socket 執行：測試設定補上 GPU socket
    with_gpu = settings_with_embedding.model_copy(
        update={"embedding_gpu_socket_path": Path("/fake/gpu.sock")}
    )
    assert job_runner.run_job(job_id, db_engine, with_gpu) == "succeeded"

    embedding = _put(configured_client, repository_id, snapshot_id)["embedding"]

    assert embedding["status"] == "completed"
    assert embedding["job"] is None and embedding["device"] is None
    assert _job_count(db_engine) == 1


def test_ac6_switching_the_reference_keeps_the_old_snapshots_job(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings_with_embedding: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    first = _indexed_snapshot(db_engine, repository_id, settings_with_embedding, index=1)
    second = _indexed_snapshot(db_engine, repository_id, settings_with_embedding, index=2)
    old_job = _put(configured_client, repository_id, first)["embedding"]["job"]["id"]

    new_job = _put(configured_client, repository_id, second)["embedding"]["job"]["id"]

    assert new_job != old_job
    with db_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, snapshot_id, status FROM jobs ORDER BY id")
        ).all()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (old_job, first, "queued"),
        (new_job, second, "queued"),
    ]


def test_ac7_clearing_the_reference_queues_no_job(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings_with_embedding: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _devices(monkeypatch, gpu_available=True)
    snapshot_id = _indexed_snapshot(db_engine, repository_id, settings_with_embedding)

    # 沒有參考時 DELETE：不排入
    assert (
        configured_client.delete(f"/api/v1/repositories/{repository_id}/reference").status_code
        == 204
    )
    assert _job_count(db_engine) == 0

    _put(configured_client, repository_id, snapshot_id)
    assert _job_count(db_engine) == 1
    # 有參考時 DELETE：job 數不變
    assert (
        configured_client.delete(f"/api/v1/repositories/{repository_id}/reference").status_code
        == 204
    )
    assert _job_count(db_engine) == 1
