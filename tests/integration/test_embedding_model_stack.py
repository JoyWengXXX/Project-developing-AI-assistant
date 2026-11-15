"""自建假內容經隔離真實模型與 PostgreSQL 的端到端測試。"""

import os
import unittest
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import try_advisory_lock
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_client import SocketEmbeddingClient
from repopilot.services import job_runner, jobs, retrieval_jobs
from repopilot.services.retrieval_build import RetrievalSummary, build_retrieval_index
from repopilot.services.retrieval_chunks import build_chunk_set
from repopilot.services.retrieval_vectors import retrieval_lock_key


@unittest.skipUnless(
    os.environ.get("REPOPILOT_RUN_REAL_EMBEDDING_TEST") == "1",
    "requires an explicitly configured isolated model and test database",
)
class TestRealEmbeddingStack(unittest.TestCase):
    def test_formal_retrieval_job_with_fake_snapshot(self) -> None:
        url = os.environ["TEST_DATABASE_URL"]
        if not url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
            self.fail("A dedicated test database is required.")
        settings = Settings(
            _env_file=None,
            database_url=SecretStr(url),
            embedding_model_sha256=os.environ["TEST_MODEL_SHA256"],
            embedding_runtime_sha256=os.environ["TEST_RUNTIME_SHA256"],
            embedding_socket_path=Path(os.environ["TEST_EMBEDDING_SOCKET"]),
        )
        engine = create_engine(url)
        repository_id = None
        try:
            with engine.begin() as connection:
                repository_id = connection.scalar(
                    text(
                        "INSERT INTO repositories (name, canonical_local_path) "
                        "VALUES (:name, :path) RETURNING id"
                    ),
                    {"name": "fake-retrieval-job", "path": f"/fake-test-{uuid4().hex}"},
                )
            assert isinstance(repository_id, int)
            with Session(engine, expire_on_commit=False) as session:
                snapshot = Snapshot(
                    repository_id=repository_id,
                    commit_sha="b" * 40,
                    import_profile_hash="fake-profile",
                    status="completed",
                    file_count=1,
                )
                session.add(snapshot)
                session.flush()
                content = "def sort_files(files):\n    return sorted(files)\n"
                session.add(
                    SnapshotFile(
                        snapshot_id=snapshot.id,
                        path="fake.py",
                        content=content,
                        byte_size=len(content.encode("utf-8")),
                        status="imported",
                    )
                )
                session.commit()
                submission = retrieval_jobs.submit_retrieval_job(session, snapshot.id, settings)
                assert submission.job is not None
                job_id = submission.job.id
                snapshot_id = snapshot.id
            self.assertEqual(job_runner.run_job(job_id, engine, settings), "succeeded")
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT i.status, j.status, vector_dims(v.embedding) "
                        "FROM retrieval_indexes i JOIN retrieval_vectors v ON v.index_id=i.id "
                        "JOIN jobs j ON j.id=:job_id WHERE i.snapshot_id=:snapshot_id"
                    ),
                    {"job_id": job_id, "snapshot_id": snapshot_id},
                ).one()
                self.assertEqual(row, ("completed", "succeeded", 768))
        finally:
            if isinstance(repository_id, int):
                with engine.begin() as connection:
                    connection.execute(
                        text("DELETE FROM snapshots WHERE repository_id=:id"), {"id": repository_id}
                    )
                    connection.execute(
                        text("DELETE FROM repositories WHERE id=:id"), {"id": repository_id}
                    )
            engine.dispose()

    def test_fake_snapshot_is_indexed_and_published_with_its_job(self) -> None:
        url = os.environ["TEST_DATABASE_URL"]
        if not url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
            self.fail("A dedicated test database is required.")
        profile = EmbeddingProfile(
            model_sha256=os.environ["TEST_MODEL_SHA256"],
            runtime_bundle_sha256=os.environ["TEST_RUNTIME_SHA256"],
        )
        client = SocketEmbeddingClient(
            socket_path=Path(os.environ["TEST_EMBEDDING_SOCKET"]),
            profile=profile,
            timeout_seconds=60,
        )
        client.verify_identity()
        engine = create_engine(url)
        repository_id = None
        try:
            with engine.begin() as connection:
                repository_id = connection.scalar(
                    text(
                        "INSERT INTO repositories (name, canonical_local_path) "
                        "VALUES (:name, :path) RETURNING id"
                    ),
                    {"name": "fake-embedding-test", "path": f"/fake-test-{uuid4().hex}"},
                )
            assert isinstance(repository_id, int)
            settings = Settings(_env_file=None, database_url=SecretStr(url))
            with Session(engine, expire_on_commit=False) as session:
                snapshot = Snapshot(
                    repository_id=repository_id,
                    commit_sha="a" * 40,
                    import_profile_hash="fake-profile",
                    status="completed",
                    file_count=1,
                )
                session.add(snapshot)
                session.flush()
                content = "def sort_files(files):\n    return sorted(files)\n"
                session.add(
                    SnapshotFile(
                        snapshot_id=snapshot.id,
                        path="fake.py",
                        content=content,
                        byte_size=len(content.encode("utf-8")),
                        status="imported",
                    )
                )
                session.commit()
                snapshot_id = snapshot.id
                chunk_set = build_chunk_set(session, snapshot_id, max_bytes=1024)
                session.commit()
                created = jobs.create_job(
                    session,
                    kind="snapshot_index",
                    dedupe_key=f"fake-embedding-{uuid4().hex}",
                    settings=settings,
                    repository_id=repository_id,
                    snapshot_id=snapshot_id,
                )
                job = jobs.acquire_lease(session, created.job.id, settings)
                assert job is not None and job.lease_token is not None
                token = job.lease_token
                job_id = job.id

                def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
                    result = jobs.complete_job(
                        session,
                        job_id,
                        token,
                        result={"index_id": summary.index_id},
                        before_publish=publish,
                    )
                    return result.status == "succeeded"

                with try_advisory_lock(
                    engine, retrieval_lock_key(chunk_set.chunk_set_id, profile)
                ) as lock:
                    assert lock is not None
                    summary = build_retrieval_index(
                        session,
                        chunk_set_id=chunk_set.chunk_set_id,
                        owner_token=token,
                        profile=profile,
                        lock=lock,
                        count_tokens=client.count_tokens,
                        embed=client.embed,
                        checkpoint=lambda: None,
                        finalize=finalize,
                    )
                row = session.execute(
                    text(
                        "SELECT i.status, j.status, vector_dims(v.embedding) "
                        "FROM retrieval_indexes i JOIN retrieval_vectors v ON v.index_id=i.id "
                        "JOIN jobs j ON j.id=:job_id WHERE i.id=:index_id"
                    ),
                    {"job_id": job_id, "index_id": summary.index_id},
                ).one()
                self.assertEqual(row, ("completed", "succeeded", 768))
        finally:
            if isinstance(repository_id, int):
                with engine.begin() as connection:
                    connection.execute(
                        text("DELETE FROM snapshots WHERE repository_id=:id"), {"id": repository_id}
                    )
                    connection.execute(
                        text("DELETE FROM repositories WHERE id=:id"), {"id": repository_id}
                    )
            engine.dispose()
