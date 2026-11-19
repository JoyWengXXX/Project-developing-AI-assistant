"""檢索工作種類的資料庫與狀態機契約。"""

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.services import jobs
from tests.integration.conftest import alembic_config


def test_retrieval_job_can_transition_to_embedding(
    session: Session, job_settings: Settings
) -> None:
    created = jobs.create_job(
        session, kind="snapshot_retrieval", dedupe_key="retrieval:fake", settings=job_settings
    )
    leased = jobs.acquire_lease(session, created.job.id, job_settings)
    assert leased is not None and leased.lease_token is not None
    updated = jobs.update_progress(session, leased.id, leased.lease_token, phase="embedding")
    assert updated.kind == "snapshot_retrieval"
    assert updated.phase == "embedding"


def test_downgrade_refuses_to_delete_retrieval_job_history(
    db_engine: Engine, session: Session, job_settings: Settings, test_database_url: str
) -> None:
    created = jobs.create_job(
        session, kind="snapshot_retrieval", dedupe_key="retrieval:downgrade", settings=job_settings
    )
    job_id = created.job.id
    try:
        with pytest.raises(DBAPIError, match="Cannot downgrade"):
            command.downgrade(alembic_config(test_database_url), "0016")
        with db_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM jobs WHERE id=:id"), {"id": job_id})
                == 1
            )
    finally:
        with db_engine.begin() as connection:
            connection.execute(text("DELETE FROM jobs WHERE id=:id"), {"id": job_id})


def test_migration_round_trip_without_retrieval_jobs(
    clean_jobs: None, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    command.downgrade(config, "0016")
    try:
        command.upgrade(config, "head")
        command.check(config)
    finally:
        command.upgrade(config, "head")
