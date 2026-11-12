"""service_heartbeats（C8）：寫入、過期判斷、清理，以及 `get_jobs_status`。"""

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.services import service_heartbeats as heartbeats
from tests.integration.jobs.conftest import at, new_job, running_job, scalar


def test_status_is_absent_when_no_service_ever_reported(
    session: Session, job_settings: Settings
) -> None:
    status = heartbeats.get_jobs_status(session, job_settings, now=at(0))

    assert status.dispatcher.status == "absent" and status.worker.status == "absent"
    assert status.dispatcher.last_seen_at is None
    assert (status.queued, status.running) == (0, 0)
    assert status.heartbeat_timeout_seconds == 45  # SERVICE_HEARTBEAT_SECONDS(15) 的 3 倍


def test_a_fresh_heartbeat_is_online_and_goes_stale_after_three_intervals(
    session: Session, job_settings: Settings
) -> None:
    heartbeats.record_heartbeat(session, "worker", "worker-1", now=at(0))

    online = heartbeats.get_jobs_status(session, job_settings, now=at(45))
    stale = heartbeats.get_jobs_status(session, job_settings, now=at(46))

    assert (online.worker.status, online.worker.instances_online) == ("online", 1)
    assert online.worker.last_seen_at == at(0)
    assert (stale.worker.status, stale.worker.instances_online) == ("stale", 0)
    assert stale.worker.last_seen_at == at(0)  # 過期仍回報最後一次看到的時間
    assert stale.dispatcher.status == "absent"  # 兩種服務互不影響


def test_instances_are_counted_separately_and_one_live_instance_is_enough(
    session: Session, job_settings: Settings
) -> None:
    heartbeats.record_heartbeat(session, "worker", "old", now=at(0))
    heartbeats.record_heartbeat(session, "worker", "new", now=at(100))
    heartbeats.record_heartbeat(session, "dispatcher", "d1", now=at(100))

    status = heartbeats.get_jobs_status(session, job_settings, now=at(110))

    assert (status.worker.status, status.worker.instances_online) == ("online", 1)
    assert status.worker.last_seen_at == at(100)
    assert status.dispatcher.status == "online"


def test_recording_again_updates_the_same_row_instead_of_adding_one(
    session: Session, db_engine: Engine
) -> None:
    heartbeats.record_heartbeat(session, "worker", "w1", info={"parser": "1"}, now=at(0))
    heartbeats.record_heartbeat(session, "worker", "w1", info={"parser": "2"}, now=at(30))

    assert scalar(db_engine, "SELECT count(*) FROM service_heartbeats") == 1
    assert scalar(db_engine, "SELECT last_seen_at FROM service_heartbeats") == at(30)
    assert scalar(db_engine, "SELECT info ->> 'parser' FROM service_heartbeats") == "2"


def test_status_counts_queued_and_running_jobs(session: Session, job_settings: Settings) -> None:
    new_job(session, job_settings, key="q1")
    new_job(session, job_settings, key="q2")
    running_job(session, job_settings, key="r1")

    status = heartbeats.get_jobs_status(session, job_settings, now=at(1))

    assert (status.queued, status.running) == (2, 1)


def test_purge_removes_only_heartbeats_older_than_one_day(
    session: Session, db_engine: Engine
) -> None:
    heartbeats.record_heartbeat(session, "worker", "gone", now=at(0))
    heartbeats.record_heartbeat(session, "worker", "recent", now=at(2 * 3600))

    removed = heartbeats.purge_stale_heartbeats(session, now=at(24 * 3600 + 1))

    assert removed == 1
    assert scalar(db_engine, "SELECT instance_id FROM service_heartbeats") == "recent"


def test_invalid_service_instance_and_secret_info_are_rejected(
    session: Session, db_engine: Engine
) -> None:
    with pytest.raises(ValueError, match="Unknown service"):
        heartbeats.record_heartbeat(session, "beat", "x")
    with pytest.raises(ValueError, match="instance_id"):
        heartbeats.record_heartbeat(session, "worker", "")
    with pytest.raises(ValueError, match="instance_id"):
        heartbeats.record_heartbeat(session, "worker", "x" * 101)
    with pytest.raises(ValueError, match="secret-looking"):
        heartbeats.record_heartbeat(session, "worker", "w", info={"api_key": "x"})

    assert scalar(db_engine, "SELECT count(*) FROM service_heartbeats") == 0
