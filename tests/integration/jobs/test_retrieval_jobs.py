"""檢索工作提交經過真正 API 與 PostgreSQL。"""

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.embedding.local_runtime import EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.main import create_app
from repopilot.services import job_runner, jobs
from tests.git_helpers import init_repo
from tests.integration.jobs.conftest import WorkerEnv, register_repo
from tests.integration.snapshot_helpers import add_snapshot


@pytest.fixture
def configured_client(
    worker_env: WorkerEnv, clean_repositories: None, clean_jobs: None
) -> TestClient:
    settings = worker_env.settings.model_copy(
        update={"embedding_model_sha256": "a" * 64, "embedding_runtime_sha256": "b" * 64}
    )
    return TestClient(create_app(settings))


@pytest.fixture
def repository_id(configured_client: TestClient, worker_env: WorkerEnv) -> int:
    repo = init_repo(worker_env.roots / "retrieval", files={"sample.txt": "sample\n"})
    return register_repo(configured_client, repo)


def test_submit_requires_configured_model(
    mapped_client: TestClient, worker_env: WorkerEnv, db_engine: Engine
) -> None:
    repo: Path = init_repo(worker_env.roots / "unconfigured", files={"sample.txt": "sample\n"})
    snapshot_id = add_snapshot(db_engine, register_repo(mapped_client, repo))
    response = mapped_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embedding_not_configured"


def test_submit_deduplicates_an_active_job(
    configured_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=1)
    initial = configured_client.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index")
    assert initial.status_code == 200 and initial.json()["status"] == "none"
    first = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
    again = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
    assert first.status_code == again.status_code == 202
    assert first.json()["job"]["kind"] == "snapshot_retrieval"
    assert first.json()["job"]["id"] == again.json()["job"]["id"]
    assert first.headers["location"] == f"/api/v1/jobs/{first.json()['job']['id']}"
    pending = configured_client.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index")
    assert pending.json()["status"] == "none"
    assert pending.json()["job"]["id"] == first.json()["job"]["id"]


def test_submit_rejects_missing_or_incomplete_snapshot(
    configured_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    incomplete = add_snapshot(db_engine, repository_id, status="importing")
    assert configured_client.post("/api/v1/snapshots/999999999/retrieval-jobs").status_code == 404
    response = configured_client.post(f"/api/v1/snapshots/{incomplete}/retrieval-jobs")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_not_completed"


@pytest.mark.parametrize("outcome", ["succeeded", "cancelled", "failed"])
def test_worker_publishes_only_on_success(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=1)
    submitted = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
    job_id = submitted.json()["job"]["id"]

    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            pass

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            if outcome == "cancelled":
                with Session(db_engine) as session:
                    jobs.cancel_job(session, job_id)
            if outcome == "failed":
                raise EmbeddingRuntimeError("The isolated embedding runtime request failed.")
            return [1.0] + [0.0] * 767

    from repopilot.services import retrieval_job_runner

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", FakeEmbedding)
    settings = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/socket"),
        }
    )
    assert job_runner.run_job(job_id, db_engine, settings) == outcome
    with db_engine.connect() as connection:
        status = connection.scalar(
            text("SELECT status FROM retrieval_indexes WHERE snapshot_id=:id"), {"id": snapshot_id}
        )
        job_status = connection.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id})
    assert status == ("completed" if outcome == "succeeded" else "failed")
    assert job_status == outcome
    details = configured_client.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index")
    assert details.status_code == 200
    assert details.json()["status"] == status
    assert details.json()["job"]["status"] == outcome
    if outcome == "succeeded":
        assert details.json()["segment_count"] == 1
        assert details.json()["chunk_count"] == 1
        again = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
        assert again.status_code == 200 and again.json()["reused"] is True
        different_settings = worker_env.settings.model_copy(
            update={"embedding_model_sha256": "c" * 64, "embedding_runtime_sha256": "b" * 64}
        )
        with TestClient(create_app(different_settings)) as other:
            changed = other.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index")
            assert changed.status_code == 200 and changed.json()["status"] == "none"
            assert changed.json()["job"] is None
    elif outcome == "failed":
        retry = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
        assert retry.status_code == 202
        assert retry.json()["job"]["id"] == job_id
        assert retry.json()["job"]["status"] == "queued"


def test_job_is_split_into_slices_without_consuming_retries_and_resumes_each_time(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=3)
    job_id = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs").json()[
        "job"
    ]["id"]
    embedded: list[str] = []

    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            pass

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            embedded.append(value)
            return [1.0] + [0.0] * 767

    from repopilot.services import retrieval_job_runner

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", FakeEmbedding)
    settings = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/socket"),
            "retrieval_time_slice_seconds": 0.001,  # 每個片段做完就超過，一段只做一個片段
        }
    )

    def row() -> tuple[str, int, str | None]:
        with db_engine.connect() as connection:
            result = connection.execute(
                text("SELECT status, attempt, error_code FROM jobs WHERE id=:id"), {"id": job_id}
            ).one()
        return result[0], result[1], result[2]

    def allow_next_slice() -> None:
        with db_engine.begin() as connection:  # 略過 5～15 秒的讓出延遲
            connection.execute(
                text("UPDATE jobs SET next_attempt_at = NULL WHERE id=:id"), {"id": job_id}
            )

    assert job_runner.run_job(job_id, db_engine, settings) == "deferred"
    assert row() == ("queued", 0, None)  # 沒有消耗重試額度，也沒有錯誤
    index = configured_client.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index").json()
    assert index["status"] == "building"
    progress = configured_client.get(f"/api/v1/jobs/{job_id}").json()["progress"]
    assert (progress["processed_files"], progress["total_files"]) == (1, 3)

    allow_next_slice()
    assert job_runner.run_job(job_id, db_engine, settings) == "deferred"
    assert row() == ("queued", 0, None)
    progress = configured_client.get(f"/api/v1/jobs/{job_id}").json()["progress"]
    assert (progress["processed_files"], progress["total_files"]) == (2, 3)

    allow_next_slice()
    assert job_runner.run_job(job_id, db_engine, settings) == "succeeded"
    assert row()[0] == "succeeded"
    assert len(embedded) == 3  # 每個片段只算一次：每一段都續跑，沒有重算
    details = configured_client.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index").json()
    assert (details["status"], details["chunk_count"], details["segment_count"]) == (
        "completed",
        3,
        3,
    )


def test_cancel_between_slices_then_resubmit_continues_from_saved_chunks(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=3)
    job_id = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs").json()[
        "job"
    ]["id"]
    embedded: list[str] = []

    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            pass

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            embedded.append(value)
            return [1.0] + [0.0] * 767

    from repopilot.services import retrieval_job_runner

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", FakeEmbedding)
    settings = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/socket"),
            "retrieval_time_slice_seconds": 0.001,
        }
    )
    assert job_runner.run_job(job_id, db_engine, settings) == "deferred"
    with Session(db_engine) as session:
        jobs.cancel_job(session, job_id)
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}) == (
            "cancelled"
        )

    resubmitted = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
    assert resubmitted.status_code == 202 and resubmitted.json()["job"]["id"] == job_id
    unlimited = settings.model_copy(update={"retrieval_time_slice_seconds": 3600.0})
    assert job_runner.run_job(job_id, db_engine, unlimited) == "succeeded"
    assert len(embedded) == 3  # 取消前完成的片段沒有重算


def _available_devices(
    monkeypatch: pytest.MonkeyPatch, *, gpu: bool, configured: bool = True
) -> None:
    from repopilot.embedding import gateway_client

    state = {
        "cpu": {"configured": True, "available": True},
        "gpu": {"configured": configured, "available": gpu},
    }
    monkeypatch.setattr(gateway_client, "embedding_devices", lambda settings: state)


def test_device_defaults_to_cpu_and_gpu_is_stored_in_params(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available_devices(monkeypatch, gpu=True)
    cpu_snapshot = add_snapshot(db_engine, repository_id, files=1)
    gpu_snapshot = add_snapshot(db_engine, repository_id, files=1, index=2)
    default = configured_client.post(f"/api/v1/snapshots/{cpu_snapshot}/retrieval-jobs")
    assert default.json()["job"]["params"]["device"] == "cpu"
    gpu = configured_client.post(
        f"/api/v1/snapshots/{gpu_snapshot}/retrieval-jobs", json={"device": "gpu"}
    )
    assert gpu.status_code == 202 and gpu.json()["job"]["params"]["device"] == "gpu"
    again = configured_client.post(
        f"/api/v1/snapshots/{gpu_snapshot}/retrieval-jobs", json={"device": "cpu"}
    )
    # 已有進行中的工作：沿用它，不因為重複提交而換裝置
    assert again.json()["job"]["id"] == gpu.json()["job"]["id"]
    assert again.json()["job"]["params"]["device"] == "gpu"
    bad = configured_client.post(
        f"/api/v1/snapshots/{cpu_snapshot}/retrieval-jobs", json={"device": "tpu"}
    )
    assert bad.status_code == 422


@pytest.mark.parametrize("configured", [True, False])
def test_gpu_submit_is_rejected_without_creating_a_job_when_unavailable(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    _available_devices(monkeypatch, gpu=False, configured=configured)
    snapshot_id = add_snapshot(db_engine, repository_id, files=1)
    response = configured_client.post(
        f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs", json={"device": "gpu"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embedding_device_unavailable"
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM jobs")) == 0
    # CPU 不受影響
    assert (
        configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs").status_code == 202
    )


def test_worker_uses_the_socket_of_the_job_device_and_never_falls_back(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available_devices(monkeypatch, gpu=True)
    gpu_snapshot = add_snapshot(db_engine, repository_id, files=1)
    cpu_snapshot = add_snapshot(db_engine, repository_id, files=1, index=2)
    gpu_job = configured_client.post(
        f"/api/v1/snapshots/{gpu_snapshot}/retrieval-jobs", json={"device": "gpu"}
    ).json()["job"]["id"]
    cpu_job = configured_client.post(f"/api/v1/snapshots/{cpu_snapshot}/retrieval-jobs").json()[
        "job"
    ]["id"]
    used: list[str] = []

    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            used.append(socket_path.name)

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            return [1.0] + [0.0] * 767

    from repopilot.services import retrieval_job_runner

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", FakeEmbedding)
    base = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/cpu.sock"),
        }
    )
    # worker 沒有設定 GPU socket：失敗，不改用 CPU
    assert job_runner.run_job(gpu_job, db_engine, base) == "failed"
    assert used == []
    with db_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT error_code FROM jobs WHERE id=:id"), {"id": gpu_job})
            == "embedding_device_unavailable"
        )
        assert connection.scalar(text("SELECT count(*) FROM retrieval_indexes")) == 0

    with_gpu = base.model_copy(update={"embedding_gpu_socket_path": Path("/fake/gpu.sock")})
    assert job_runner.run_job(cpu_job, db_engine, with_gpu) == "succeeded"
    assert used == ["cpu.sock"]
    resubmitted = configured_client.post(
        f"/api/v1/snapshots/{gpu_snapshot}/retrieval-jobs", json={"device": "gpu"}
    )
    assert resubmitted.json()["job"]["id"] == gpu_job
    assert job_runner.run_job(gpu_job, db_engine, with_gpu) == "succeeded"
    assert used == ["cpu.sock", "gpu.sock"]
    for job_id, device in ((gpu_job, "gpu"), (cpu_job, "cpu")):
        assert configured_client.get(f"/api/v1/jobs/{job_id}").json()["result"]["device"] == device


def test_gpu_job_that_stops_resumes_on_cpu_with_saved_chunks_and_no_reembedding(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _available_devices(monkeypatch, gpu=True)
    snapshot_id = add_snapshot(db_engine, repository_id, files=3)
    job_id = configured_client.post(
        f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs", json={"device": "gpu"}
    ).json()["job"]["id"]
    embedded: list[tuple[str, str]] = []

    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            self.name = socket_path.name

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            if self.name == "gpu.sock" and sum(n == "gpu.sock" for n, _ in embedded) == 1:
                raise EmbeddingRuntimeError("The isolated embedding runtime request failed.")
            embedded.append((self.name, value))
            return [1.0] + [0.0] * 767

    from repopilot.services import retrieval_job_runner

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", FakeEmbedding)
    settings = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/cpu.sock"),
            "embedding_gpu_socket_path": Path("/fake/gpu.sock"),
        }
    )
    assert job_runner.run_job(job_id, db_engine, settings) == "failed"
    assert [name for name, _ in embedded] == ["gpu.sock"]

    retried = configured_client.post(
        f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs", json={"device": "cpu"}
    )
    assert retried.json()["job"]["id"] == job_id
    assert retried.json()["job"]["params"]["device"] == "cpu"
    assert job_runner.run_job(job_id, db_engine, settings) == "succeeded"
    assert [name for name, _ in embedded] == ["gpu.sock", "cpu.sock", "cpu.sock"]  # 共 3 次，無重算
    details = configured_client.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index").json()
    assert (details["status"], details["chunk_count"]) == ("completed", 3)


def test_embedding_devices_endpoint_reports_the_gateway_state(
    configured_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available_devices(monkeypatch, gpu=False, configured=True)
    response = configured_client.get("/api/v1/retrieval/devices")
    assert response.status_code == 200
    assert response.json() == {
        "cpu": {"configured": True, "available": True},
        "gpu": {"configured": True, "available": False},
    }


def test_worker_rejects_mismatched_embedding_profile(
    configured_client: TestClient, repository_id: int, db_engine: Engine, worker_env: WorkerEnv
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=1)
    submitted = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
    job_id = submitted.json()["job"]["id"]
    settings = worker_env.settings.model_copy(
        update={"embedding_model_sha256": "c" * 64, "embedding_runtime_sha256": "b" * 64}
    )
    assert job_runner.run_job(job_id, db_engine, settings) == "failed"
    with db_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT error_code FROM jobs WHERE id=:id"), {"id": job_id})
            == "profile_mismatch"
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM retrieval_indexes WHERE snapshot_id=:id"),
                {"id": snapshot_id},
            )
            == 0
        )


def test_worker_losing_lease_does_not_publish_or_change_new_owner(
    configured_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=1)
    submitted = configured_client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
    job_id = submitted.json()["job"]["id"]
    new_token = uuid4()

    class ReplacedOwner:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            pass

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            with db_engine.begin() as connection:
                connection.execute(
                    text("UPDATE jobs SET lease_token=:token WHERE id=:id"),
                    {"token": new_token, "id": job_id},
                )
            return [1.0] + [0.0] * 767

    from repopilot.services import retrieval_job_runner

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", ReplacedOwner)
    settings = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/socket"),
        }
    )
    assert job_runner.run_job(job_id, db_engine, settings) == "lease_lost"
    with db_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT lease_token FROM jobs WHERE id=:id"), {"id": job_id})
            == new_token
        )
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE snapshot_id=:id"),
                {"id": snapshot_id},
            )
            == "building"
        )
