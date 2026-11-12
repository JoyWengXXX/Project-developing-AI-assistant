"""dispatcher 迴圈（規格 5.3、9.3）：假發布器與真實 `CeleryPublisher`（mock 的 Celery app）。

不連 Redis。派送、復原掃描、退避、優雅結束都用真實 PostgreSQL；時間以注入的 `monotonic` 與
`now` 控制，不靠 sleep。真實 Redis 與容器內的 dispatcher 由 `stack` 測試驗證。
"""

import threading
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.jobs.celery_app import CeleryPublisher
from repopilot.jobs.dispatcher import MAX_BACKOFF_SECONDS, Dispatcher
from repopilot.services import job_dispatch, service_heartbeats
from repopilot.services import jobs as job_service
from tests.integration.jobs.conftest import dispatch_rows, new_job, scalar


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[int] = []
        self.fail = False

    def publish(self, job_id: int) -> None:
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.published.append(job_id)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def publisher() -> RecordingPublisher:
    return RecordingPublisher()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def dispatcher(
    session_factory: sessionmaker[Session],
    publisher: RecordingPublisher,
    job_settings: Settings,
    clock: FakeClock,
) -> Dispatcher:
    return Dispatcher(session_factory, publisher, job_settings, monotonic=clock)


# --- 派送 ---


def test_a_tick_dispatches_pending_events_and_marks_them_sent(
    dispatcher: Dispatcher,
    publisher: RecordingPublisher,
    session: Session,
    job_settings: Settings,
    db_engine: Engine,
) -> None:
    job = new_job(session, job_settings)

    result = dispatcher.tick()

    assert (result.sent, result.failed, result.error) == (1, 0, False)
    assert publisher.published == [job.id]
    assert dispatch_rows(db_engine, job.id) == [(1, "sent", 1, 0)]
    assert dispatcher.next_delay() == job_settings.dispatch_poll_seconds


def test_a_restarted_dispatcher_still_sends_events_left_pending(
    session: Session,
    session_factory: sessionmaker[Session],
    job_settings: Settings,
    db_engine: Engine,
) -> None:
    """dispatcher 停機期間建立的 job：事件保持 pending；新的 dispatcher 實例啟動後照常送出。"""
    job = new_job(session, job_settings)
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 0)]  # 沒有 dispatcher 在跑
    publisher = RecordingPublisher()

    Dispatcher(session_factory, publisher, job_settings).tick()  # 「重啟」

    assert publisher.published == [job.id]
    assert dispatch_rows(db_engine, job.id) == [(1, "sent", 1, 0)]


def test_the_recovery_scan_runs_on_the_first_tick_and_then_on_its_interval(
    dispatcher: Dispatcher,
    clock: FakeClock,
    session: Session,
    job_settings: Settings,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans: list[float] = []
    real = job_dispatch.run_recovery_scan

    def counting(*args: Any, **kwargs: Any) -> Any:
        scans.append(clock.now)
        return real(*args, **kwargs)

    monkeypatch.setattr(job_dispatch, "run_recovery_scan", counting)

    assert dispatcher.tick().scanned is True  # 重啟後立刻掃描
    clock.now += job_settings.recovery_scan_seconds - 0.1
    assert dispatcher.tick().scanned is False
    clock.now += 0.2
    assert dispatcher.tick().scanned is True
    assert len(scans) == 2


def test_an_expired_lease_is_recovered_by_the_dispatcher_scan(
    dispatcher: Dispatcher, session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    job = new_job(session, job_settings)
    leased = job_service.acquire_lease(session, job.id, job_settings)
    assert leased is not None
    with db_engine.begin() as connection:  # worker 失聯：租約已過期
        connection.exec_driver_sql(
            f"UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = {job.id}"  # noqa: S608
        )

    result = dispatcher.tick()

    assert result.scanned is True
    assert scalar(db_engine, "SELECT status FROM jobs WHERE id = :i", i=job.id) == "queued"
    assert scalar(db_engine, "SELECT error_code FROM jobs WHERE id = :i", i=job.id) == "worker_lost"


# --- 失敗與退避 ---


def test_publisher_failure_keeps_events_pending_and_backs_off_exponentially(
    dispatcher: Dispatcher,
    publisher: RecordingPublisher,
    session: Session,
    job_settings: Settings,
    db_engine: Engine,
) -> None:
    job = new_job(session, job_settings)
    publisher.fail = True

    delays = []
    for _ in range(7):
        result = dispatcher.tick()
        assert (result.sent, result.failed, result.error) == (0, 1, False)
        delays.append(dispatcher.next_delay())

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, MAX_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS]
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 7)]  # 不假裝已派送

    publisher.fail = False
    assert dispatcher.tick().sent == 1
    assert dispatcher.next_delay() == job_settings.dispatch_poll_seconds  # 恢復後退避歸零
    assert dispatch_rows(db_engine, job.id)[0][1] == "sent"


def test_a_database_error_in_a_tick_is_contained_and_backs_off(
    publisher: RecordingPublisher, job_settings: Settings
) -> None:
    calls = {"n": 0}

    def broken_factory() -> Session:
        calls["n"] += 1
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    dispatcher = Dispatcher(broken_factory, publisher, job_settings)  # type: ignore[arg-type]

    first = dispatcher.tick()  # 不拋出
    second = dispatcher.tick()

    assert (first.error, second.error) == (True, True)
    assert dispatcher.next_delay() == 2.0
    assert calls["n"] == 2


def test_the_real_celery_publisher_leaves_the_event_pending_when_the_broker_refuses(
    session_factory: sessionmaker[Session],
    session: Session,
    job_settings: Settings,
    db_engine: Engine,
) -> None:
    """`send_task` 拋例外（Redis 連不上）→ 事件不得被標記為已派送（broker 確認前不標記）。"""
    app = MagicMock()
    app.send_task.side_effect = ConnectionError("redis down")
    job = new_job(session, job_settings)
    dispatcher = Dispatcher(session_factory, CeleryPublisher(app, "q.test"), job_settings)

    result = dispatcher.tick()

    assert (result.sent, result.failed) == (0, 1)
    assert dispatch_rows(db_engine, job.id) == [(1, "pending", 0, 1)]
    assert (
        scalar(db_engine, "SELECT last_error FROM job_dispatches WHERE job_id = :i", i=job.id)
        == "ConnectionError"
    )

    app.send_task.side_effect = None  # broker 恢復
    assert dispatcher.tick().sent == 1
    assert dispatch_rows(db_engine, job.id) == [(1, "sent", 1, 1)]  # 失敗次數保留作為紀錄


def test_the_real_celery_publisher_marks_sent_only_after_send_task_returns(
    session_factory: sessionmaker[Session],
    session: Session,
    job_settings: Settings,
    db_engine: Engine,
) -> None:
    observed: list[list[tuple[int, str, int, int]]] = []
    app = MagicMock()
    job = new_job(session, job_settings)

    def send_task(*_: Any, **__: Any) -> None:
        # 發布呼叫進行中，事件還是 pending（尚未被標記）
        observed.append(dispatch_rows(db_engine, job.id))

    app.send_task.side_effect = send_task

    Dispatcher(session_factory, CeleryPublisher(app, "q.test"), job_settings).tick()

    assert observed == [[(1, "pending", 0, 0)]]
    assert dispatch_rows(db_engine, job.id) == [(1, "sent", 1, 0)]
    _, kwargs = app.send_task.call_args
    assert kwargs["args"] == [job.id]  # 只帶 job_id


# --- 主迴圈與優雅結束 ---


def test_run_loops_until_the_stop_event_is_set_and_finishes_the_current_tick(
    session: Session,
    session_factory: sessionmaker[Session],
    job_settings: Settings,
    db_engine: Engine,
) -> None:
    stop = threading.Event()
    job = new_job(session, job_settings)
    published: list[int] = []

    class StoppingPublisher:
        def publish(self, job_id: int) -> None:
            published.append(job_id)
            stop.set()  # 模擬收到 SIGTERM：這一輪仍要做完

    fast = job_settings.model_copy(update={"dispatch_poll_seconds": 0.01})
    thread = threading.Thread(
        target=Dispatcher(session_factory, StoppingPublisher(), fast).run, args=(stop,)
    )

    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive()  # 優雅結束
    assert published == [job.id]
    assert dispatch_rows(db_engine, job.id) == [(1, "sent", 1, 0)]


def test_the_heartbeat_purge_runs_once_per_hour_not_every_tick(
    dispatcher: Dispatcher, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    purges: list[float] = []

    def counting_purge(session: Session) -> int:
        purges.append(clock.now)
        return 0

    monkeypatch.setattr(service_heartbeats, "purge_stale_heartbeats", counting_purge)

    dispatcher.tick()
    clock.now += 60
    dispatcher.tick()
    clock.now += 3600
    dispatcher.tick()

    assert len(purges) == 2


# --- 服務心跳 ---


def test_the_service_heartbeat_thread_writes_immediately_and_stops_cleanly(
    session_factory: sessionmaker[Session], job_settings: Settings, db_engine: Engine
) -> None:
    thread = service_heartbeats.ServiceHeartbeatThread(
        session_factory, "worker", "instance-under-test", job_settings, info={"queue": "q"}
    )
    thread.start()
    try:
        _wait_for(
            lambda: (
                scalar(
                    db_engine,
                    "SELECT count(*) FROM service_heartbeats WHERE service = 'worker' "
                    "AND instance_id = 'instance-under-test'",
                )
                == 1
            )
        )
    finally:
        thread.stop()

    with session_factory() as session:
        assert service_heartbeats.is_instance_alive(
            session, "worker", "instance-under-test", job_settings
        )
        assert not service_heartbeats.is_instance_alive(
            session, "dispatcher", "instance-under-test", job_settings
        )
        assert not service_heartbeats.is_instance_alive(session, "worker", "nobody", job_settings)


def _wait_for(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")
