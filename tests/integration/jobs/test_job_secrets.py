"""job 的日誌與持久化資料不含令牌或秘密（規則 6）：整個生命週期走一遍，逐項檢查。"""

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.services import job_dispatch
from repopilot.services import jobs as job_service
from tests.github_helpers import capture_logs
from tests.integration.jobs.conftest import at, new_job


class _Publisher:
    def publish(self, job_id: int) -> None:
        return None


def test_logs_and_dispatch_rows_never_contain_lease_tokens(
    session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    tokens: list[str] = []
    with capture_logs() as logs:
        job = new_job(session, job_settings, params={"requested_revision": "HEAD"})
        job_dispatch.dispatch_pending(session, _Publisher(), now=at(1))
        first = job_service.acquire_lease(session, job.id, job_settings, now=at(2))
        assert first is not None and first.lease_token is not None
        tokens.append(str(first.lease_token))
        job_service.heartbeat(session, job.id, first.lease_token, job_settings, now=at(3))
        job_service.update_progress(session, job.id, first.lease_token, phase="importing")
        job_service.fail_job(
            session, job.id, first.lease_token, job_settings, error_code="git_timeout", now=at(4)
        )
        second = job_service.acquire_lease(session, job.id, job_settings, now=at(10_000))
        assert second is not None and second.lease_token is not None
        tokens.append(str(second.lease_token))
        job_service.cancel_job(session, job.id)
        job_service.acknowledge_cancel(session, job.id, second.lease_token)
        log_text = logs.text()

    assert "job_created" in log_text and "job_lease_acquired" in log_text  # 日誌確實有內容
    for token in tokens:
        assert token not in log_text
    with db_engine.connect() as connection:
        dumped = " ".join(
            str(value)
            for table in ("job_dispatches",)
            for row in connection.execute(text(f"SELECT * FROM {table}"))  # noqa: S608
            for value in row
        )
    for token in tokens:
        assert token not in dumped  # 派送事件只有 job id，沒有令牌
