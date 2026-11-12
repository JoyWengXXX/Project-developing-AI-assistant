"""租約與令牌柵欄：取得租約的競爭、attempt 規則、舊令牌的寫入一律被拒且不改動資料。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.errors import JobLeaseLostError
from repopilot.services import job_dispatch
from repopilot.services import jobs as job_service
from tests.integration.jobs.conftest import (
    at,
    new_job,
    row_of,
    run_together,
    running_job,
    scalar,
)


def test_acquire_lease_makes_the_job_running_with_a_token_and_attempt_one(
    session: Session, job_settings: Settings
) -> None:
    job = new_job(session, job_settings)

    acquired = job_service.acquire_lease(session, job.id, job_settings, now=at(10))

    assert acquired is not None
    assert (acquired.status, acquired.attempt) == ("running", 1)
    assert isinstance(acquired.lease_token, UUID)
    assert acquired.lease_expires_at == at(70)  # now + JOB_LEASE_SECONDS(60)
    assert acquired.last_heartbeat_at == at(10)
    assert acquired.started_at == at(10)


def test_acquiring_a_job_that_is_already_running_returns_none(
    session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)

    assert job_service.acquire_lease(session, job.id, job_settings, now=at(1)) is None
    # 失敗的取得不會換掉令牌、也不會增加 attempt
    again = job_service.acquire_lease(session, job.id, job_settings, now=at(2))
    assert again is None
    assert job.lease_token == token


def test_acquiring_a_missing_job_returns_none(session: Session, job_settings: Settings) -> None:
    assert job_service.acquire_lease(session, 424242, job_settings) is None


def test_lease_is_not_granted_before_next_attempt_at(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    job_service.fail_job(
        session,
        job.id,
        token,
        job_settings,
        error_code="github_unreachable",
        now=at(5),
        rand=lambda: 0.5,
    )
    # 退避 30 秒 → next_attempt_at = T0+35

    assert job_service.acquire_lease(session, job.id, job_settings, now=at(34)) is None
    reacquired = job_service.acquire_lease(session, job.id, job_settings, now=at(35))

    assert reacquired is not None
    assert reacquired.attempt == 2
    assert reacquired.next_attempt_at is None


def test_each_acquisition_issues_a_new_token_and_increments_attempt(
    session: Session, job_settings: Settings
) -> None:
    job, token1 = running_job(session, job_settings)
    job_service.fail_job(session, job.id, token1, job_settings, error_code="git_timeout", now=at(1))

    second = job_service.acquire_lease(session, job.id, job_settings, now=at(10_000))

    assert second is not None
    assert second.attempt == 2
    assert second.lease_token is not None and second.lease_token != token1
    # started_at 保留第一次取得的時間
    assert second.started_at == at(0)


def test_concurrent_acquisition_has_exactly_one_winner(
    session_factory: sessionmaker[Session], session: Session, job_settings: Settings
) -> None:
    job = new_job(session, job_settings)

    def attempt(_: int) -> UUID | None:
        with session_factory() as worker_session:
            acquired = job_service.acquire_lease(worker_session, job.id, job_settings, now=at(1))
            return acquired.lease_token if acquired is not None else None

    tokens = run_together(8, attempt)

    winners = [token for token in tokens if token is not None]
    assert len(winners) == 1
    with session_factory() as check:
        stored = job_service.acquire_lease(check, job.id, job_settings)  # 仍是 running → None
        assert stored is None
    assert scalar(session.get_bind(), "SELECT attempt FROM jobs WHERE id = :i", i=job.id) == 1
    assert (
        scalar(session.get_bind(), "SELECT lease_token FROM jobs WHERE id = :i", i=job.id)
        == (winners[0])
    )


# --- 令牌柵欄 ---

FencedCall = Callable[[Session, int, UUID, Settings], Any]

FENCED_CALLS: dict[str, FencedCall] = {
    "update_progress": lambda s, j, t, cfg: job_service.update_progress(
        s, j, t, processed_files=5, work_state={"x": 1}
    ),
    "heartbeat": lambda s, j, t, cfg: job_service.heartbeat(s, j, t, cfg, now=at(5)),
    "complete_job": lambda s, j, t, cfg: job_service.complete_job(s, j, t, result={"ok": True}),
    "fail_job_permanent": lambda s, j, t, cfg: job_service.fail_job(
        s, j, t, cfg, error_code="invalid_revision"
    ),
    "fail_job_transient": lambda s, j, t, cfg: job_service.fail_job(
        s, j, t, cfg, error_code="worker_lost"
    ),
    "defer_job": lambda s, j, t, cfg: job_service.defer_job(s, j, t),
    "acknowledge_cancel": lambda s, j, t, cfg: job_service.acknowledge_cancel(s, j, t),
}


@pytest.mark.parametrize("call", list(FENCED_CALLS))
def test_a_stale_token_is_rejected_and_changes_nothing_after_the_lease_was_taken_over(
    session: Session, job_settings: Settings, db_engine: Engine, call: str
) -> None:
    """舊 worker（租約已過期、被復原掃描收回並由新 worker 取得）的任何寫入都被拒絕。"""
    job, old_token = running_job(session, job_settings)
    job_dispatch.recover_expired_leases(session, job_settings, now=at(100), rand=lambda: 0.5)
    new_lease = job_service.acquire_lease(session, job.id, job_settings, now=at(200))
    assert new_lease is not None and new_lease.lease_token != old_token
    # 新擁有者要求取消，使 acknowledge_cancel 的前置條件成立（舊令牌仍必須被拒）
    job_service.cancel_job(session, job.id, now=at(201))
    before = row_of(db_engine, job.id)

    with pytest.raises(JobLeaseLostError):
        FENCED_CALLS[call](session, job.id, old_token, job_settings)

    assert row_of(db_engine, job.id) == before


@pytest.mark.parametrize("call", list(FENCED_CALLS))
def test_a_random_token_is_rejected_for_a_running_job(
    session: Session, job_settings: Settings, db_engine: Engine, call: str
) -> None:
    job, _ = running_job(session, job_settings)
    job_service.cancel_job(session, job.id, now=at(1))
    before = row_of(db_engine, job.id)

    with pytest.raises(JobLeaseLostError):
        FENCED_CALLS[call](session, job.id, uuid4(), job_settings)

    assert row_of(db_engine, job.id) == before


@pytest.mark.parametrize(
    "call", ["update_progress", "heartbeat", "complete_job", "fail_job_permanent"]
)
def test_the_previous_token_is_rejected_once_the_job_has_finished(
    session: Session, job_settings: Settings, db_engine: Engine, call: str
) -> None:
    job, token = running_job(session, job_settings)
    job_service.complete_job(session, job.id, token, result={"done": True})
    before = row_of(db_engine, job.id)

    with pytest.raises(JobLeaseLostError):
        FENCED_CALLS[call](session, job.id, token, job_settings)

    assert row_of(db_engine, job.id) == before


def test_operations_on_a_missing_job_are_rejected(session: Session, job_settings: Settings) -> None:
    with pytest.raises(JobLeaseLostError):
        job_service.heartbeat(session, 99_999, uuid4(), job_settings)
    with pytest.raises(JobLeaseLostError):
        job_service.complete_job(session, 99_999, uuid4())


def test_the_current_token_keeps_working_after_its_lease_expired_until_recovery_acts(
    session: Session, job_settings: Settings
) -> None:
    """過期只代表「可被復原掃描接手」；掃描尚未動作前，原擁有者的心跳仍有效並延長租約。"""
    job, token = running_job(session, job_settings)

    still_ok = job_service.heartbeat(session, job.id, token, job_settings, now=at(500))

    assert still_ok is False
    assert job_service.complete_job(session, job.id, token).status == "succeeded"


def test_heartbeat_extends_the_lease_and_reports_the_cancel_flag(
    session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)

    assert job_service.heartbeat(session, job.id, token, job_settings, now=at(30)) is False
    refreshed = job_service.update_progress(session, job.id, token)
    assert refreshed.lease_expires_at == at(90)
    assert refreshed.last_heartbeat_at == at(30)

    job_service.cancel_job(session, job.id, now=at(31))
    assert job_service.heartbeat(session, job.id, token, job_settings, now=at(40)) is True


def test_progress_is_monotonic_and_phase_moves_forward_only(
    session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)

    first = job_service.update_progress(
        session,
        job.id,
        token,
        phase="importing",
        processed_files=10,
        total_files=100,
        bytes_received=500,
        now=at(1),
    )
    assert (first.phase, first.processed_files, first.total_files) == ("importing", 10, 100)
    assert first.phase_started_at == at(1)

    # 計數不倒退；同階段不更新 phase_started_at
    second = job_service.update_progress(
        session, job.id, token, phase="importing", processed_files=3, bytes_received=100, now=at(2)
    )
    assert (second.processed_files, second.bytes_received) == (10, 500)
    assert second.phase_started_at == at(1)

    advanced = job_service.update_progress(
        session, job.id, token, phase="indexing", indexed_files=4, parse_error_count=1, now=at(3)
    )
    assert (advanced.phase, advanced.indexed_files, advanced.parse_error_count) == (
        "indexing",
        4,
        1,
    )
    assert advanced.phase_started_at == at(3)

    with pytest.raises(ValueError, match="backwards"):
        job_service.update_progress(session, job.id, token, phase="importing")


@pytest.mark.parametrize("phase", ["cloning", "bogus", "completed"])
def test_invalid_phases_are_rejected_without_writing(
    session: Session, job_settings: Settings, db_engine: Engine, phase: str
) -> None:
    job, token = running_job(session, job_settings)  # snapshot_import
    before = row_of(db_engine, job.id)

    with pytest.raises(ValueError, match=r"(not valid|completed phase)"):
        job_service.update_progress(session, job.id, token, phase=phase)

    assert row_of(db_engine, job.id) == before


def test_work_state_is_merged_and_secret_keys_are_rejected(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    job_service.update_progress(session, job.id, token, work_state={"temp_dir": "abc"})
    merged = job_service.update_progress(session, job.id, token, work_state={"current_file_id": 7})
    assert merged.work_state == {"temp_dir": "abc", "current_file_id": 7}
    before = row_of(db_engine, job.id)

    with pytest.raises(ValueError, match="secret-looking"):
        job_service.update_progress(session, job.id, token, work_state={"github_token": "x"})

    assert row_of(db_engine, job.id) == before
