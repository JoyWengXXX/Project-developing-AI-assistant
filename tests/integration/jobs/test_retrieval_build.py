"""檢索索引與工作完成的交易邊界。"""

from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import try_advisory_lock
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import ImportCancelledError, JobLeaseLostError
from repopilot.services import jobs
from repopilot.services.retrieval_build import RetrievalSummary, build_retrieval_index
from repopilot.services.retrieval_vectors import retrieval_lock_key
from tests.integration import test_embedding_storage
from tests.integration.jobs.conftest import running_job

vector_chunk_set = test_embedding_storage.chunk_set


def test_index_and_job_publish_in_same_transaction(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
) -> None:
    snapshot_id, set_id, _ = vector_chunk_set
    job, token = running_job(session, job_settings)
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)

    def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
        done = jobs.complete_job(
            session,
            job.id,
            token,
            result={"index_id": summary.index_id},
            snapshot_id=snapshot_id,
            before_publish=publish,
        )
        return done.status == "succeeded"

    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        summary = build_retrieval_index(
            session,
            chunk_set_id=set_id,
            owner_token=token,
            profile=profile,
            lock=lock,
            count_tokens=lambda value: len(value) + 1,
            embed=lambda value: [1.0] + [0.0] * 767,
            checkpoint=lambda: None,
            finalize=finalize,
        )
        reused = build_retrieval_index(
            session,
            chunk_set_id=set_id,
            owner_token=uuid4(),
            profile=profile,
            lock=lock,
            count_tokens=lambda value: pytest.fail("completed index must not tokenize"),
            embed=lambda value: pytest.fail("completed index must not call the model"),
            checkpoint=lambda: None,
            finalize=lambda summary, publish: pytest.fail("reused index must not be published"),
        )
        assert reused.reused
        assert reused.index_id == summary.index_id
        assert reused.segment_count == summary.segment_count
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE id=:id"), {"id": summary.index_id}
            )
            == "completed"
        )
        assert connection.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job.id}) == (
            "succeeded"
        )


@pytest.mark.parametrize("outcome", ["cancelled", "error"])
def test_cancel_or_publish_error_never_exposes_completed_index(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
    outcome: str,
) -> None:
    snapshot_id, set_id, _ = vector_chunk_set
    job, token = running_job(session, job_settings)
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)

    def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
        if outcome == "cancelled":
            jobs.cancel_job(session, job.id)
            before_publish = publish
        else:

            def before_publish(transaction: Session) -> None:
                publish(transaction)
                raise RuntimeError("injected failure")

        done = jobs.complete_job(
            session,
            job.id,
            token,
            result={"index_id": summary.index_id},
            snapshot_id=snapshot_id,
            before_publish=before_publish,
        )
        return done.status == "succeeded"

    expected_error = ImportCancelledError if outcome == "cancelled" else RuntimeError
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with pytest.raises(expected_error):
            build_retrieval_index(
                session,
                chunk_set_id=set_id,
                owner_token=token,
                profile=profile,
                lock=lock,
                count_tokens=lambda value: len(value) + 1,
                embed=lambda value: [1.0] + [0.0] * 767,
                checkpoint=lambda: None,
                finalize=finalize,
            )
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id=:id"), {"id": set_id}
            )
            == "failed"
        )
        assert connection.scalar(text("SELECT status FROM jobs WHERE id=:id"), {"id": job.id}) == (
            "cancelled" if outcome == "cancelled" else "running"
        )


def test_stale_builder_does_not_overwrite_new_owner(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
) -> None:
    _, set_id, _ = vector_chunk_set
    _, token = running_job(session, job_settings)
    new_owner = uuid4()
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)

    def replace_owner(_: str) -> list[float]:
        with db_engine.begin() as connection:
            connection.execute(
                text("UPDATE retrieval_indexes SET owner_token=:owner WHERE chunk_set_id=:id"),
                {"owner": new_owner, "id": set_id},
            )
        return [1.0] + [0.0] * 767

    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with pytest.raises(JobLeaseLostError):
            build_retrieval_index(
                session,
                chunk_set_id=set_id,
                owner_token=token,
                profile=profile,
                lock=lock,
                count_tokens=lambda value: len(value) + 1,
                embed=replace_owner,
                checkpoint=lambda: None,
                finalize=lambda summary, publish: True,
            )
    with db_engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, owner_token FROM retrieval_indexes WHERE chunk_set_id=:id"),
            {"id": set_id},
        ).one()
        assert row == ("building", new_owner)
        assert connection.scalar(text("SELECT count(*) FROM retrieval_vectors")) == 0


def test_finalizer_must_publish_before_reporting_success(
    db_engine: Engine,
    session: Session,
    job_settings: Settings,
    vector_chunk_set: tuple[int, int, int],
) -> None:
    _, set_id, _ = vector_chunk_set
    _, token = running_job(session, job_settings)
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with pytest.raises(RuntimeError, match="finalizer did not publish"):
            build_retrieval_index(
                session,
                chunk_set_id=set_id,
                owner_token=token,
                profile=profile,
                lock=lock,
                count_tokens=lambda value: len(value) + 1,
                embed=lambda value: [1.0] + [0.0] * 767,
                checkpoint=lambda: None,
                finalize=lambda summary, publish: True,
            )
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM retrieval_indexes WHERE chunk_set_id=:id"), {"id": set_id}
            )
            == "failed"
        )
