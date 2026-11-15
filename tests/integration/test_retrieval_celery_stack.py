"""隔離 Celery、dispatcher 與真實模型的檢索工作驗證。"""

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from repopilot.config import Settings
from repopilot.main import create_app
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files


@pytest.mark.skipif(
    os.environ.get("REPOPILOT_RUN_RETRIEVAL_CELERY_TEST") != "1",
    reason="requires an explicitly configured isolated Celery stack and real model",
)
def test_dispatcher_and_celery_build_retrieval_index_with_real_model() -> None:
    url = os.environ["TEST_DATABASE_URL"]
    if not url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
        pytest.fail("A dedicated test database is required.")
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(url),
        embedding_model_sha256=os.environ["EMBEDDING_MODEL_SHA256"],
        embedding_runtime_sha256=os.environ["EMBEDDING_RUNTIME_SHA256"],
        embedding_socket_path=Path("/fake/socket"),
    )
    engine = create_engine(url)
    repository_id = None
    job_id = None
    terminal = False
    try:
        with engine.begin() as connection:
            repository_id = connection.execute(
                text(
                    "INSERT INTO repositories (name, canonical_local_path) "
                    "VALUES (:name, :path) RETURNING id"
                ),
                {"name": "celery-evaluation", "path": f"/fake-celery-{uuid4().hex}"},
            ).scalar_one()
        snapshot_id = add_snapshot(engine, repository_id)
        add_files(engine, snapshot_id, {"fake.py": "def sort_values(v):\n    return sorted(v)\n"})
        with TestClient(create_app(settings)) as client:
            response = client.post(f"/api/v1/snapshots/{snapshot_id}/retrieval-jobs")
            assert response.status_code == 202, response.text
            job_id = response.json()["job"]["id"]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                status_response = client.get(f"/api/v1/jobs/{job_id}")
                assert status_response.status_code == 200
                job = status_response.json()
                if job["status"] in ("succeeded", "failed", "cancelled"):
                    terminal = True
                    break
                time.sleep(0.2)
            assert terminal, "Timed out waiting for the retrieval job."
            assert job["status"] == "succeeded", job
            index = client.get(f"/api/v1/snapshots/{snapshot_id}/retrieval-index")
            assert index.status_code == 200
            assert index.json()["status"] == "completed"
        with engine.connect() as connection:
            dimensions = (
                connection.execute(
                    text(
                        "SELECT vector_dims(v.embedding) FROM retrieval_indexes i "
                        "JOIN retrieval_vectors v ON v.index_id=i.id "
                        "WHERE i.snapshot_id=:snapshot_id"
                    ),
                    {"snapshot_id": snapshot_id},
                )
                .scalars()
                .all()
            )
        assert dimensions and all(value == 768 for value in dimensions)
    finally:
        if repository_id is not None and (job_id is None or terminal):
            with engine.begin() as connection:
                if job_id is not None:
                    connection.execute(text("DELETE FROM jobs WHERE id=:id"), {"id": job_id})
                connection.execute(
                    text("DELETE FROM snapshots WHERE repository_id=:id"), {"id": repository_id}
                )
                connection.execute(
                    text("DELETE FROM repositories WHERE id=:id"), {"id": repository_id}
                )
        engine.dispose()
