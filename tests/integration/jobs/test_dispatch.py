"""派送（outbox，SKIP LOCKED）與復原掃描。以假發布器測試，不引入 Celery／Redis。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.services import job_dispatch, job_queries
from repopilot.services import jobs as job_service
from tests.integration.jobs.conftest import (
    T0,
    at,
    dispatch_rows,
    named_engine,
    new_job,
    running_job,
    scalar,
)


class FakePublisher:
    """記錄收到的 job id；`fail_for` 內的 job 會拋出例外（模擬 broker 拒絕或連不上）。"""

    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.published: list[int] = []
        self.fail_for = fail_for or set()

    def publish(self, job_id: int) -> None:
        if job_id in self.fail_for:
            raise ConnectionError("broker password=hunter2 unreachable")  # 訊息不得被保存
        self.published.append(job_id)


# --- dispatch_pending ---


def test_dispatch_publishes_pending_events_in_order_and_marks_them_sent(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    jobs = [new_job(session, job_settings, key=f"k{i}") for i in range(3)]
    publisher = FakePublisher()

    result = job_dispatch.dispatch_pending(session, publisher, now=at(1))

    assert (result.sent, result.failed) == (3, 0)
    assert publisher.published == [job.id for job in jobs]
    for job in jobs:
        assert dispatch_rows(db_engine, job.id) == [(1, "sent", 1, 0)]
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches WHERE last_sent_at IS NULL") == 0
    # 已派送的事件不會再被派送
    again = FakePublisher()
    assert job_dispatch.dispatch_pending(session, again, now=at(2)).sent == 0
    assert again.published == []


def test_publisher_failure_keeps_the_event_pending_counts_it_and_stores_only_the_type(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    good = new_job(session, job_settings, key="good")
    bad = new_job(session, job_settings, key="bad")

    first = job_dispatch.dispatch_pending(session, FakePublisher(fail_for={bad.id}), now=at(1))

    assert (first.sent, first.failed) == (1, 1)
    assert dispatch_rows(db_engine, good.id) == [(1, "sent", 1, 0)]
    assert dispatch_rows(db_engine, bad.id) == [(1, "pending", 0, 1)]
    last_error = scalar(
        db_engine, "SELECT last_error FROM job_dispatches WHERE job_id = :i", i=bad.id
    )
    assert last_error == "ConnectionError"  # 只有例外型別；訊息（可能含連線資訊）不保存

    # 失敗再失敗：持續計次；恢復後成功並清除 last_error，失敗次數保留作為紀錄
    job_dispatch.dispatch_pending(session, FakePublisher(fail_for={bad.id}), now=at(2))
    assert dispatch_rows(db_engine, bad.id) == [(1, "pending", 0, 2)]
    recovered = FakePublisher()
    job_dispatch.dispatch_pending(session, recovered, now=at(3))
    assert recovered.published == [bad.id]
    assert dispatch_rows(db_engine, bad.id) == [(1, "sent", 1, 2)]
    assert (
        scalar(db_engine, "SELECT last_error FROM job_dispatches WHERE job_id = :i", i=bad.id)
        is None
    )


def test_dispatch_respects_not_before_and_only_sends_for_queued_jobs(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, token = running_job(session, job_settings, key="retrying")
    job_service.fail_job(  # 退避 30 秒 → 事件 not_before = T0+40
        session, job.id, token, job_settings, error_code="git_timeout", now=at(10), rand=lambda: 0.5
    )
    running, _ = running_job(session, job_settings, key="running")
    # running 的工作即使殘留 pending 事件也不派送
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO job_dispatches (job_id, attempt_target) VALUES (:i, 99)"),
            {"i": running.id},
        )

    early = FakePublisher()
    assert job_dispatch.dispatch_pending(session, early, now=at(39)).sent == 0
    on_time = FakePublisher()
    assert job_dispatch.dispatch_pending(session, on_time, now=at(40)).sent == 1
    assert on_time.published == [job.id]


def test_batch_size_limits_one_round(session: Session, job_settings: Settings) -> None:
    for i in range(5):
        new_job(session, job_settings, key=f"k{i}")
    publisher = FakePublisher()

    assert job_dispatch.dispatch_pending(session, publisher, batch_size=2, now=at(1)).sent == 2
    assert job_dispatch.dispatch_pending(session, publisher, batch_size=2, now=at(1)).sent == 2
    assert job_dispatch.dispatch_pending(session, publisher, batch_size=2, now=at(1)).sent == 1
    assert len(set(publisher.published)) == 5


def test_two_consumers_never_dispatch_the_same_event_skip_locked(
    session_factory: sessionmaker[Session],
    session: Session,
    job_settings: Settings,
    test_database_url: str,
    db_engine: Engine,
) -> None:
    """A 持有第 1、2 筆的列鎖並停在發布中；B 必須略過它們、取走第 3、4 筆（不等待、不重複）。"""
    jobs = [new_job(session, job_settings, key=f"k{i}") for i in range(4)]
    a_inside = threading.Event()
    release_a = threading.Event()
    b_done = threading.Event()
    publisher_a = FakePublisher()
    publisher_b = FakePublisher()

    class BlockingPublisher(FakePublisher):
        def publish(self, job_id: int) -> None:
            a_inside.set()
            assert release_a.wait(timeout=60)
            super().publish(job_id)

    blocking = BlockingPublisher()
    engine_b = named_engine(test_database_url, "t-dispatcher-b")

    def consumer_a() -> None:
        with session_factory() as a_session:
            job_dispatch.dispatch_pending(a_session, blocking, batch_size=2, now=at(1))

    def consumer_b() -> None:
        try:
            with Session(engine_b) as b_session:
                job_dispatch.dispatch_pending(b_session, publisher_b, batch_size=2, now=at(1))
        finally:
            b_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(consumer_a)
            assert a_inside.wait(timeout=30)
            second = pool.submit(consumer_b)
            # 若沒有 SKIP LOCKED，B 會卡在 A 的列鎖上而無法完成
            b_finished = b_done.wait(timeout=30)
            release_a.set()
            first.result(timeout=60)
            second.result(timeout=60)
    finally:
        release_a.set()
        engine_b.dispose()

    assert b_finished, "consumer B blocked on locked rows (SKIP LOCKED is not in effect)"
    assert publisher_a.published == []
    assert sorted(blocking.published) == [jobs[0].id, jobs[1].id]
    assert sorted(publisher_b.published) == [jobs[2].id, jobs[3].id]
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches WHERE status = 'sent'") == 4


def test_dispatch_lock_is_released_on_publisher_base_exception(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job = new_job(session, job_settings)

    class Interrupted(FakePublisher):
        def publish(self, job_id: int) -> None:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        job_dispatch.dispatch_pending(session, Interrupted(), now=at(1))

    # 交易已回復：事件仍是 pending、未被標記
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 0)]


# --- recover_expired_leases ---


def test_expired_lease_is_requeued_with_worker_lost_and_backoff(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job, _ = running_job(session, job_settings)  # 租約到 T0+60

    assert job_dispatch.recover_expired_leases(session, job_settings, now=at(59)) == (0, 0, 0)
    result = job_dispatch.recover_expired_leases(
        session, job_settings, now=at(61), rand=lambda: 0.5
    )

    assert result == (1, 0, 0)
    refreshed = job_queries.get_job(session, job.id)
    assert refreshed.status == "queued"
    assert (refreshed.error_code, refreshed.attempt) == ("worker_lost", 1)
    assert refreshed.next_attempt_at == at(91)  # 61 + 30
    assert refreshed.lease_token is None
    assert dispatch_rows(db_engine, job.id) == [(2, "pending", 0, 0)]


def test_a_heartbeat_before_the_scan_prevents_recovery(
    session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    job_service.heartbeat(session, job.id, token, job_settings, now=at(50))  # 租約延到 T0+110

    assert job_dispatch.recover_expired_leases(session, job_settings, now=at(70)) == (0, 0, 0)
    assert job_dispatch.recover_expired_leases(session, job_settings, now=at(111)) == (1, 0, 0)


def test_expired_lease_at_the_attempt_limit_fails_with_worker_lost(
    session: Session, job_settings: Settings
) -> None:
    job, _ = running_job(session, job_settings)
    now = 0.0
    for expected_attempt in (1, 2):
        now += 10_000
        assert job_dispatch.recover_expired_leases(session, job_settings, now=at(now)) == (1, 0, 0)
        now += 10_000
        lease = job_service.acquire_lease(session, job.id, job_settings, now=at(now))
        assert lease is not None and lease.attempt == expected_attempt + 1
    now += 10_000

    result = job_dispatch.recover_expired_leases(session, job_settings, now=at(now))

    assert result == (0, 1, 0)
    final = job_queries.get_job(session, job.id)
    assert (final.status, final.error_code, final.attempt) == ("failed", "worker_lost", 3)
    assert final.finished_at == at(now)


def test_expired_lease_with_a_cancel_request_becomes_cancelled(
    session: Session, job_settings: Settings
) -> None:
    job, _ = running_job(session, job_settings)
    job_service.cancel_job(session, job.id, now=at(5))

    assert job_dispatch.recover_expired_leases(session, job_settings, now=at(100)) == (0, 0, 1)
    assert job_queries.get_job(session, job.id).status == "cancelled"


def test_recovery_skips_rows_locked_by_another_transaction(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    """擁有者正在發布結果（持有列鎖）的工作這一輪被跳過，不與正常完成競爭。"""
    job, _ = running_job(session, job_settings)

    with db_engine.connect() as owner:
        owner.execute(text("SELECT id FROM jobs WHERE id = :i FOR UPDATE"), {"i": job.id})
        assert job_dispatch.recover_expired_leases(session, job_settings, now=at(500)) == (0, 0, 0)
    assert job_dispatch.recover_expired_leases(session, job_settings, now=at(500)) == (1, 0, 0)


# --- resend_unreceived_dispatches ---


def test_unreceived_dispatch_is_resent_after_the_wait_and_the_wait_doubles(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job = new_job(session, job_settings)
    job_dispatch.dispatch_pending(session, FakePublisher(), now=at(0))  # sent 1 次，last_sent=T0

    assert job_dispatch.resend_unreceived_dispatches(session, job_settings, now=at(299)) == 0
    assert job_dispatch.resend_unreceived_dispatches(session, job_settings, now=at(301)) == 1
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 1, 0)]

    # 第二次送出（T0+400）後要等 600 秒
    job_dispatch.dispatch_pending(session, FakePublisher(), now=at(400))
    assert job_dispatch.resend_unreceived_dispatches(session, job_settings, now=at(400 + 599)) == 0
    assert job_dispatch.resend_unreceived_dispatches(session, job_settings, now=at(400 + 601)) == 1
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 2, 0)]


def test_resend_ignores_events_whose_job_is_no_longer_queued(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job = new_job(session, job_settings)
    job_dispatch.dispatch_pending(session, FakePublisher(), now=at(0))
    job_service.acquire_lease(session, job.id, job_settings, now=at(1))  # worker 已接收

    assert job_dispatch.resend_unreceived_dispatches(session, job_settings, now=at(10_000)) == 0
    assert dispatch_rows(db_engine, job.id) == [(1, "sent", 1, 0)]


def test_a_queued_job_is_never_failed_just_for_waiting_too_long(
    session: Session, job_settings: Settings
) -> None:
    job = new_job(session, job_settings)

    result = job_dispatch.run_recovery_scan(session, job_settings, now=at(30 * 24 * 3600))

    assert job_queries.get_job(session, job.id).status == "queued"
    assert (result.requeued, result.failed, result.cancelled) == (0, 0, 0)


# --- 其他掃描 ---


def test_remove_finished_dispatches_deletes_leftovers_of_terminal_jobs(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    done, token = running_job(session, job_settings, key="done")
    job_service.complete_job(session, done.id, token)
    queued = new_job(session, job_settings, key="queued")
    with db_engine.begin() as connection:  # 製造殘留事件（正常流程在結束時已刪除）
        connection.execute(
            text("INSERT INTO job_dispatches (job_id, attempt_target) VALUES (:i, 5)"),
            {"i": done.id},
        )

    assert job_dispatch.remove_finished_dispatches(session) == 1
    assert dispatch_rows(db_engine, done.id) == []
    assert dispatch_rows(db_engine, queued.id) == [(1, "pending", 0, 0)]


def test_a_queued_job_without_an_event_gets_one_so_it_cannot_be_stuck(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job = new_job(session, job_settings)
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM job_dispatches"))

    assert job_dispatch.heal_missing_dispatches(session, now=at(5)) == 1

    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 0)]
    assert job_dispatch.heal_missing_dispatches(session, now=at(6)) == 0  # 已有事件，不重複補


def test_overdue_pending_events_are_reported_but_not_changed(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job = new_job(session, job_settings)  # not_before = T0

    assert job_dispatch.count_overdue_pending(session, job_settings, now=at(299)) == 0
    assert job_dispatch.count_overdue_pending(session, job_settings, now=at(301)) == 1
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 0)]


def test_run_recovery_scan_combines_the_steps(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    lost, _ = running_job(session, job_settings, key="lost")
    waiting = new_job(session, job_settings, key="waiting")
    job_dispatch.dispatch_pending(session, FakePublisher(), now=at(0))  # waiting 的事件 sent
    before = T0 + timedelta(seconds=1_000)

    result = job_dispatch.run_recovery_scan(session, job_settings, now=before, rand=lambda: 0.5)

    assert result.requeued == 1  # lost
    assert result.resent == 1  # waiting（sent 超過 300 秒仍是 queued）
    assert dispatch_rows(db_engine, lost.id) == [(2, "pending", 0, 0)]
    assert dispatch_rows(db_engine, waiting.id)[0][1] == "pending"
