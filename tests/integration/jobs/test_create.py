"""建立 job（去重、outbox 同交易、外鍵、秘密鍵名）。PostgreSQL 真實整合測試。"""

from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.db.models import Job, JobDispatch
from repopilot.errors import RepositoryNotFoundError, SnapshotNotFoundError
from repopilot.services import jobs as job_service
from tests.integration.jobs.conftest import at, dispatch_rows, new_job, run_together, scalar

KEY = "import:1:0123abc:feedbeef"


def test_create_job_writes_a_queued_job_and_its_first_dispatch(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    created = job_service.create_job(
        session,
        kind="snapshot_import",
        dedupe_key=KEY,
        settings=job_settings,
        params={"requested_revision": "HEAD", "commit_sha": "0" * 40},
        request_id="req-1",
        import_profile_hash="h" * 64,
    )

    assert created.created is True
    job = created.job
    assert (job.status, job.phase, job.attempt, job.max_attempts) == ("queued", "pending", 0, 3)
    assert job.lease_token is None
    assert job.params == {"requested_revision": "HEAD", "commit_sha": "0" * 40}
    assert job.request_id == "req-1"
    assert job.cancel_requested_at is None and job.finished_at is None
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 0)]


def test_second_create_with_the_same_key_returns_the_existing_active_job(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    first = new_job(session, job_settings, key=KEY)

    second = job_service.create_job(
        session, kind="snapshot_import", dedupe_key=KEY, settings=job_settings
    )

    assert second.created is False
    assert second.job.id == first.id
    assert scalar(db_engine, "SELECT count(*) FROM jobs") == 1
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches") == 1


def test_a_running_job_also_blocks_a_duplicate(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    first = new_job(session, job_settings, key=KEY)
    assert job_service.acquire_lease(session, first.id, job_settings, now=at(0)) is not None

    second = job_service.create_job(
        session, kind="snapshot_import", dedupe_key=KEY, settings=job_settings
    )

    assert second.created is False and second.job.id == first.id
    assert second.job.status == "running"


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_a_finished_job_does_not_block_a_new_one_with_the_same_key(
    session: Session, job_settings: Settings, db_engine: Engine, terminal: str
) -> None:
    first = new_job(session, job_settings, key=KEY)
    if terminal == "cancelled":
        job_service.cancel_job(session, first.id)
    else:
        job = job_service.acquire_lease(session, first.id, job_settings, now=at(0))
        assert job is not None and job.lease_token is not None
        if terminal == "succeeded":
            job_service.complete_job(session, first.id, job.lease_token)
        else:
            job_service.fail_job(
                session, first.id, job.lease_token, job_settings, error_code="invalid_revision"
            )

    second = job_service.create_job(
        session, kind="snapshot_import", dedupe_key=KEY, settings=job_settings
    )

    assert second.created is True
    assert second.job.id != first.id
    assert scalar(db_engine, "SELECT count(*) FROM jobs") == 2


def test_different_keys_create_independent_jobs(session: Session, job_settings: Settings) -> None:
    a = new_job(session, job_settings, key="import:1:a:x")
    b = new_job(session, job_settings, key="import:1:b:x")

    assert a.id != b.id


def test_concurrent_creates_with_the_same_key_produce_exactly_one_active_job(
    session_factory: sessionmaker[Session], job_settings: Settings, db_engine: Engine
) -> None:
    def attempt(_: int) -> tuple[int, bool]:
        with session_factory() as worker_session:
            created = job_service.create_job(
                worker_session, kind="snapshot_import", dedupe_key=KEY, settings=job_settings
            )
            return created.job.id, created.created

    results = run_together(8, attempt)

    assert len({job_id for job_id, _ in results}) == 1
    assert [created for _, created in results].count(True) == 1
    assert scalar(db_engine, "SELECT count(*) FROM jobs WHERE dedupe_key = :k", k=KEY) == 1
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches") == 1


def test_job_and_dispatch_are_written_in_one_transaction(
    session: Session,
    job_settings: Settings,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_dispatch(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(job_service, "schedule_dispatch", failing_dispatch)

    with pytest.raises(RuntimeError, match="outbox write failed"):
        job_service.create_job(
            session, kind="snapshot_import", dedupe_key=KEY, settings=job_settings
        )

    # job 的 INSERT 已執行過，但沒有提交：兩者都不存在
    assert scalar(db_engine, "SELECT count(*) FROM jobs") == 0
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches") == 0


def test_create_for_a_missing_repository_raises_repository_not_found(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    with pytest.raises(RepositoryNotFoundError):
        job_service.create_job(
            session,
            kind="snapshot_import",
            dedupe_key=KEY,
            settings=job_settings,
            repository_id=999_999,
        )

    assert scalar(db_engine, "SELECT count(*) FROM jobs") == 0
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches") == 0


def test_create_for_a_missing_snapshot_raises_snapshot_not_found(
    session: Session, job_settings: Settings
) -> None:
    with pytest.raises(SnapshotNotFoundError):
        job_service.create_job(
            session,
            kind="snapshot_index",
            dedupe_key="index:999999:h",
            settings=job_settings,
            snapshot_id=999_999,
        )


def test_unknown_kind_and_empty_key_are_programming_errors(
    session: Session, job_settings: Settings
) -> None:
    with pytest.raises(ValueError, match="Unknown job kind"):
        job_service.create_job(session, kind="bogus", dedupe_key=KEY, settings=job_settings)
    with pytest.raises(ValueError, match="dedupe_key"):
        job_service.create_job(
            session, kind="snapshot_import", dedupe_key="", settings=job_settings
        )


@pytest.mark.parametrize("key", ["token", "github_token", "Authorization", "password"])
def test_params_with_secret_looking_keys_are_rejected_and_nothing_is_written(
    session: Session, job_settings: Settings, db_engine: Engine, key: str
) -> None:
    with pytest.raises(ValueError, match="secret-looking"):
        job_service.create_job(
            session,
            kind="github_register",
            dedupe_key="register:o/r",
            settings=job_settings,
            params={key: "x"},
        )

    assert scalar(db_engine, "SELECT count(*) FROM jobs") == 0


def test_created_job_row_and_dispatch_hold_no_token_or_secret(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    """持久化的 job 與派送事件只有識別資訊：令牌欄位為空，JSON 欄位不含秘密鍵名。"""
    created = job_service.create_job(
        session,
        kind="github_register",
        dedupe_key="register:octocat/hello",
        settings=job_settings,
        params={"github_repo": "octocat/Hello"},
    )

    with Session(db_engine) as check:
        job = check.scalars(select(Job)).one()
        dispatch = check.scalars(select(JobDispatch)).one()
        dispatch_count = check.scalar(select(func.count()).select_from(JobDispatch))
    assert job.id == created.job.id
    assert job.lease_token is None
    assert job.work_state == {} and job.result is None
    assert (dispatch.status, dispatch.last_error) == ("pending", None)
    assert dispatch_count == 1
