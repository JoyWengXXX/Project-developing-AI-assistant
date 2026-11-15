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
