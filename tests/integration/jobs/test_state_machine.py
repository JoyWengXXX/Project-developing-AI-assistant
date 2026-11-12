"""狀態機轉換：失敗分類與重試、延後、取消、完成、手動重試，以及取消／完成的列鎖競爭。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.errors import (
    JobNotCancellableError,
    JobNotFoundError,
    JobNotRetryableError,
)
from repopilot.services import jobs as job_service
from tests.integration.jobs.conftest import (
    at,
    dispatch_rows,
    named_engine,
    new_job,
    row_of,
    run_together,
    running_job,
    scalar,
    wait_until_blocked,
)

# --- fail_job：分類、退避、上限 ---


def test_transient_failure_requeues_with_backoff_and_a_new_dispatch(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    job_service.update_progress(session, job.id, token, phase="importing", processed_files=9)

    failed = job_service.fail_job(
        session,
        job.id,
        token,
        job_settings,
        error_code="github_timeout",
        error_summary="upstream timed out",
        now=at(10),
        rand=lambda: 0.5,
    )

    assert failed.status == "queued"
    assert failed.attempt == 1  # 已消耗的次數不退回
    assert failed.error_code == "github_timeout"
    assert failed.error_summary == "upstream timed out"
    assert failed.next_attempt_at == at(40)  # 10 + 30（第 1 次失敗的退避基數）
    assert failed.lease_token is None and failed.lease_expires_at is None
    assert (failed.phase, failed.processed_files, failed.finished_at) == ("pending", 0, None)
    # 舊嘗試（target 1）的事件被換成下一次嘗試（target 2）的待派送事件
    assert dispatch_rows(db_engine, job.id) == [(2, "pending", 0, 0)]
    assert scalar(db_engine, "SELECT not_before FROM job_dispatches") == at(40)


def test_backoff_grows_with_each_attempt(session: Session, job_settings: Settings) -> None:
    job, token = running_job(session, job_settings)
    first = job_service.fail_job(
        session, job.id, token, job_settings, error_code="git_timeout", now=at(0), rand=lambda: 0.5
    )
    first_retry_at = first.next_attempt_at  # 同一 session 內是同一個物件，先取值再繼續
    second_lease = job_service.acquire_lease(session, job.id, job_settings, now=at(100))
    assert second_lease is not None and second_lease.lease_token is not None
    second = job_service.fail_job(
        session,
        job.id,
        second_lease.lease_token,
        job_settings,
        error_code="git_timeout",
        now=at(100),
        rand=lambda: 0.5,
    )

    assert first_retry_at == at(30)
    assert second.next_attempt_at == at(160)  # 100 + 60


def test_permanent_failure_marks_failed_without_retry(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)

    failed = job_service.fail_job(
        session,
        job.id,
        token,
        job_settings,
        error_code="invalid_revision",
        error_summary="x" * 900,
        now=at(3),
    )

    assert failed.status == "failed"
    assert failed.error_code == "invalid_revision"
    assert len(failed.error_summary or "") == 500
    assert failed.finished_at == at(3)
    assert failed.lease_token is None
    assert dispatch_rows(db_engine, job.id) == []  # 終態不留事件


def test_transient_failure_at_the_attempt_limit_fails_and_keeps_the_last_error_code(
    session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)  # max_attempts = 3
    for attempt in (1, 2):
        job_service.fail_job(
            session, job.id, token, job_settings, error_code="github_unreachable", now=at(0)
        )
        lease = job_service.acquire_lease(session, job.id, job_settings, now=at(100_000))
        assert lease is not None and lease.attempt == attempt + 1
        assert lease.lease_token is not None
        token = lease.lease_token

    final = job_service.fail_job(
        session, job.id, token, job_settings, error_code="github_unreachable", now=at(1)
    )

    assert (final.status, final.attempt, final.error_code) == ("failed", 3, "github_unreachable")


def test_failure_after_a_cancel_request_ends_cancelled_instead_of_retrying(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    job_service.cancel_job(session, job.id)

    result = job_service.fail_job(session, job.id, token, job_settings, error_code="worker_lost")

    assert result.status == "cancelled"
    assert dispatch_rows(db_engine, job.id) == []


# --- defer_job ---


def test_defer_returns_the_job_to_the_queue_without_consuming_an_attempt(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)

    deferred = job_service.defer_job(session, job.id, token, now=at(7), rand=lambda: 0.5)

    assert (deferred.status, deferred.attempt) == ("queued", 0)
    assert deferred.next_attempt_at == at(17)  # 7 + 10（5～15 秒的中間值）
    assert deferred.error_code is None  # 鎖忙碌不是錯誤
    # 重新排程的是「第 1 次嘗試」的同一筆事件，不是新的一筆
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 0)]


# --- complete_job ---


def test_complete_job_publishes_result_snapshot_and_clears_the_lease(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings, kind="github_register", key="register:o/r")
    job_service.fail_job(  # 先留下一個舊錯誤，成功後應被清除
        session, job.id, token, job_settings, error_code="git_timeout", now=at(0)
    )
    lease = job_service.acquire_lease(session, job.id, job_settings, now=at(1_000))
    assert lease is not None and lease.lease_token is not None

    done = job_service.complete_job(
        session, job.id, lease.lease_token, result={"note": "ok"}, now=at(1_010)
    )

    assert done.status == "succeeded" and done.phase == "completed"
    assert done.result == {"note": "ok"}
    assert (done.error_code, done.error_summary) == (None, None)
    assert done.finished_at == at(1_010)
    assert done.lease_token is None and done.lease_expires_at is None
    assert dispatch_rows(db_engine, job.id) == []


def test_before_publish_runs_in_the_same_transaction_and_failure_rolls_everything_back(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    seen: list[str] = []

    def explode(tx_session: Session) -> None:
        # 回呼內的寫入與狀態變更同一交易
        tx_session.execute(
            text("UPDATE jobs SET repo_key = 'written-in-callback' WHERE id = :i"), {"i": job.id}
        )
        seen.append("called")
        raise RuntimeError("snapshot completion failed")

    with pytest.raises(RuntimeError, match="snapshot completion failed"):
        job_service.complete_job(session, job.id, token, before_publish=explode)

    assert seen == ["called"]
    assert scalar(db_engine, "SELECT status FROM jobs WHERE id = :i", i=job.id) == "running"
    assert scalar(db_engine, "SELECT repo_key FROM jobs WHERE id = :i", i=job.id) is None


def test_complete_with_a_cancel_request_cancels_without_publishing(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    job_service.cancel_job(session, job.id)
    called: list[int] = []

    outcome = job_service.complete_job(
        session,
        job.id,
        token,
        result={"must": "not be published"},
        before_publish=lambda _s: called.append(1),
    )

    assert outcome.status == "cancelled"
    assert called == []
    assert scalar(db_engine, "SELECT result FROM jobs WHERE id = :i", i=job.id) is None


# --- cancel_job ---


def test_cancel_of_a_queued_job_is_immediate(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job = new_job(session, job_settings)

    result = job_service.cancel_job(session, job.id, now=at(5))

    assert result.outcome == "cancelled"
    assert (result.job.status, result.job.finished_at) == ("cancelled", at(5))
    assert dispatch_rows(db_engine, job.id) == []
    # 晚到的訊息：取得租約失敗
    assert job_service.acquire_lease(session, job.id, job_settings, now=at(6)) is None


def test_cancel_of_a_running_job_only_sets_the_flag(
    session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)

    result = job_service.cancel_job(session, job.id, now=at(5))

    assert result.outcome == "requested"
    assert result.job.status == "running"
    assert result.job.cancel_requested_at == at(5)
    assert result.job.lease_token == token  # 租約不受影響，由 worker 在檢查點收尾

    again = job_service.cancel_job(session, job.id, now=at(9))  # 冪等：旗標時間不變
    assert (again.outcome, again.job.cancel_requested_at) == ("requested", at(5))


def test_acknowledge_cancel_turns_a_flagged_running_job_into_cancelled(
    session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    with pytest.raises(ValueError, match="not requested"):
        job_service.acknowledge_cancel(session, job.id, token)
    job_service.cancel_job(session, job.id)

    cancelled = job_service.acknowledge_cancel(session, job.id, token, now=at(20))

    assert (cancelled.status, cancelled.finished_at, cancelled.lease_token) == (
        "cancelled",
        at(20),
        None,
    )


def test_cancel_is_idempotent_for_a_cancelled_job_and_rejected_for_finished_ones(
    session: Session, job_settings: Settings
) -> None:
    cancelled = new_job(session, job_settings, key="a")
    job_service.cancel_job(session, cancelled.id)
    assert job_service.cancel_job(session, cancelled.id).outcome == "cancelled"

    succeeded, token = running_job(session, job_settings, key="b")
    job_service.complete_job(session, succeeded.id, token)
    with pytest.raises(JobNotCancellableError):
        job_service.cancel_job(session, succeeded.id)

    failed, token2 = running_job(session, job_settings, key="c")
    job_service.fail_job(session, failed.id, token2, job_settings, error_code="invalid_revision")
    with pytest.raises(JobNotCancellableError):
        job_service.cancel_job(session, failed.id)

    with pytest.raises(JobNotFoundError):
        job_service.cancel_job(session, 424242)


# --- retry_job ---


def test_retry_requeues_the_same_failed_job_with_a_fresh_attempt_budget(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    job_service.fail_job(
        session, job.id, token, job_settings, error_code="invalid_revision", error_summary="bad"
    )

    retried = job_service.retry_job(session, job.id, job_settings, now=at(50))

    assert retried.id == job.id
    assert retried.status == "queued"
    assert (retried.attempt, retried.max_attempts) == (1, 4)  # attempt + JOB_MAX_ATTEMPTS(3)
    assert retried.finished_at is None and retried.next_attempt_at is None
    assert retried.error_code == "invalid_revision"  # 保留上一次的原因直到下一次結束
    assert dispatch_rows(db_engine, job.id) == [(2, "pending", 0, 0)]
    # 可再次被取得租約，attempt 繼續累加
    lease = job_service.acquire_lease(session, job.id, job_settings, now=at(51))
    assert lease is not None and lease.attempt == 2


@pytest.mark.parametrize("state", ["queued", "running", "succeeded", "cancelled"])
def test_retry_is_rejected_unless_the_job_failed(
    session: Session, job_settings: Settings, state: str
) -> None:
    job = new_job(session, job_settings)
    if state == "cancelled":
        job_service.cancel_job(session, job.id)
    elif state != "queued":
        lease = job_service.acquire_lease(session, job.id, job_settings, now=at(0))
        assert lease is not None and lease.lease_token is not None
        if state == "succeeded":
            job_service.complete_job(session, job.id, lease.lease_token)

    with pytest.raises(JobNotRetryableError):
        job_service.retry_job(session, job.id, job_settings)


def test_retry_is_rejected_when_an_identical_job_is_already_in_progress(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings)
    job_service.fail_job(session, job.id, token, job_settings, error_code="invalid_revision")
    replacement = job_service.create_job(
        session, kind="snapshot_import", dedupe_key=job.dedupe_key, settings=job_settings
    )
    assert replacement.created
    before = row_of(db_engine, job.id)

    with pytest.raises(JobNotRetryableError):
        job_service.retry_job(session, job.id, job_settings)

    assert row_of(db_engine, job.id) == before


def test_retry_of_a_missing_job_is_not_found(session: Session, job_settings: Settings) -> None:
    with pytest.raises(JobNotFoundError):
        job_service.retry_job(session, 424242, job_settings)


# --- 取消／完成／取得租約的競爭（列鎖） ---


def test_cancel_waits_for_an_in_flight_publication_and_cannot_overwrite_success(
    session_factory: sessionmaker[Session],
    session: Session,
    job_settings: Settings,
    test_database_url: str,
    db_engine: Engine,
) -> None:
    """發布交易持有列鎖時，取消會等待；之後看到 succeeded → 409，成功不會被改成 cancelled。"""
    job, token = running_job(session, job_settings)
    inside = threading.Event()
    release = threading.Event()
    cancel_engine = named_engine(test_database_url, "t-cancel-waiter")

    def hold_the_publish_transaction(_: Session) -> None:
        inside.set()
        assert release.wait(timeout=60)

    def publish() -> str:
        with session_factory() as publisher_session:
            return job_service.complete_job(
                publisher_session,
                job.id,
                token,
                result={"snapshot": 1},
                before_publish=hold_the_publish_transaction,
            ).status

    def cancel() -> None:
        with Session(cancel_engine) as canceller_session:
            job_service.cancel_job(canceller_session, job.id)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            publishing = pool.submit(publish)
            assert inside.wait(timeout=30)
            cancelling = pool.submit(cancel)
            wait_until_blocked(db_engine, "t-cancel-waiter")  # 取消確實卡在列鎖上
            release.set()
            assert publishing.result(timeout=60) == "succeeded"
            with pytest.raises(JobNotCancellableError):
                cancelling.result(timeout=60)
    finally:
        release.set()
        cancel_engine.dispose()

    assert scalar(db_engine, "SELECT status FROM jobs WHERE id = :i", i=job.id) == "succeeded"
    assert scalar(db_engine, "SELECT cancel_requested_at FROM jobs WHERE id = :i", i=job.id) is None


def test_publication_waits_for_a_concurrent_cancel_request_and_then_does_not_publish(
    session: Session,
    job_settings: Settings,
    test_database_url: str,
    db_engine: Engine,
) -> None:
    """取消請求先取得列鎖時，發布會等待、讀到旗標後改為 cancelled，且不執行發布回呼。"""
    job, token = running_job(session, job_settings)
    publish_engine = named_engine(test_database_url, "t-publisher")
    called: list[int] = []
    outcome: list[Any] = []

    def publish() -> None:
        with Session(publish_engine) as publisher_session:
            outcome.append(
                job_service.complete_job(
                    publisher_session,
                    job.id,
                    token,
                    result={"snapshot": 1},
                    before_publish=lambda _s: called.append(1),
                )
            )

    try:
        with db_engine.connect() as canceller, ThreadPoolExecutor(max_workers=1) as pool:
            # 「取消請求」交易：先鎖住列、尚未提交
            canceller.execute(text("SELECT id FROM jobs WHERE id = :i FOR UPDATE"), {"i": job.id})
            publishing = pool.submit(publish)
            wait_until_blocked(db_engine, "t-publisher")
            canceller.execute(
                text("UPDATE jobs SET cancel_requested_at = now() WHERE id = :i"), {"i": job.id}
            )
            canceller.commit()
            publishing.result(timeout=60)
    finally:
        publish_engine.dispose()

    assert [o.status for o in outcome] == ["cancelled"]
    assert called == []
    assert scalar(db_engine, "SELECT status FROM jobs WHERE id = :i", i=job.id) == "cancelled"
    assert scalar(db_engine, "SELECT result FROM jobs WHERE id = :i", i=job.id) is None


def test_cancel_and_lease_acquisition_race_always_end_in_a_consistent_state(
    session_factory: sessionmaker[Session], session: Session, job_settings: Settings
) -> None:
    for round_number in range(10):
        job = new_job(session, job_settings, key=f"race:{round_number}")

        def attempt(index: int, job_id: int = job.id) -> Any:
            with session_factory() as worker_session:
                if index == 0:
                    return job_service.acquire_lease(worker_session, job_id, job_settings)
                return job_service.cancel_job(worker_session, job_id)

        leased, cancelled = run_together(2, attempt)

        status = scalar(session.get_bind(), "SELECT status FROM jobs WHERE id = :i", i=job.id)
        if leased is not None:  # 租約先到：取消只記錄旗標
            assert (status, cancelled.outcome) == ("running", "requested")
        else:  # 取消先到：租約取得失敗
            assert (status, cancelled.outcome) == ("cancelled", "cancelled")
