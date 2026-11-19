"""migration 0006（jobs、job_dispatches、service_heartbeats）：升降版、約束、ON DELETE、D33。

升降版測試會對共用的測試資料庫降到 0005 再升回 head；以 try/finally 保證結束時回到 head。
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.services import jobs as job_service
from tests.git_helpers import init_repo
from tests.integration.conftest import alembic_config

JOB_TABLES = {"jobs", "job_dispatches", "service_heartbeats"}
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _tables(db_engine: Engine) -> set[str]:
    with db_engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _insert_job(connection: Connection, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "kind": "snapshot_import",
        "dedupe_key": f"k-{uuid.uuid4().hex}",
        "status": "queued",
        "max_attempts": 3,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    return int(
        connection.execute(
            text(f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) RETURNING id"),  # noqa: S608
            values,
        ).scalar_one()
    )


@pytest.fixture
def clean(db_engine: Engine) -> None:
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))
        connection.execute(text("TRUNCATE jobs, job_dispatches, service_heartbeats CASCADE"))


# --- 升降版 ---


def test_downgrade_drops_only_the_job_tables_and_keeps_first_stage_data(
    db_engine: Engine, test_database_url: str, clean: None
) -> None:
    config = alembic_config(test_database_url)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO repositories (name, canonical_local_path) VALUES ('keep', '/keep')")
        )
    try:
        # 0007 加入後 head 不再是 0006：明確降到 0005（原本的 "-1"）；只調整目標版本，斷言不變
        command.downgrade(config, "0005")
        assert not JOB_TABLES & _tables(db_engine)
        assert {"repositories", "snapshots", "snapshot_files"} <= _tables(db_engine)
        with db_engine.connect() as connection:
            assert connection.execute(text("SELECT name FROM repositories")).scalar_one() == "keep"

        command.upgrade(config, "head")

        assert _tables(db_engine) >= JOB_TABLES
        with db_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0
            assert connection.execute(text("SELECT name FROM repositories")).scalar_one() == "keep"
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as connection:
            connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))


def test_alembic_check_reports_no_drift_for_the_job_tables(
    db_engine: Engine, test_database_url: str
) -> None:
    command.check(alembic_config(test_database_url))


def test_partial_unique_index_is_only_for_active_jobs(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        definition = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_jobs_dedupe_key_active'")
        ).scalar_one()

    assert "UNIQUE" in definition
    where = definition.split("WHERE", 1)[1]
    assert "queued" in where and "running" in where
    assert "succeeded" not in where


# --- CHECK 與唯一約束 ---


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"status": "paused"}, "ck_jobs_status_valid"),
        ({"kind": "bogus"}, "ck_jobs_kind_valid"),
        ({"phase": "bogus"}, "ck_jobs_phase_valid"),
        # running 必須有令牌與租約期限；其他狀態不得有令牌
        ({"status": "running", "lease_expires_at": _NOW}, "ck_jobs_lease_matches_status"),
        ({"status": "queued", "lease_token": uuid.uuid4()}, "ck_jobs_lease_matches_status"),
        (
            {"status": "running", "lease_token": uuid.uuid4()},
            "ck_jobs_lease_expiry_matches_status",
        ),
        # 終態若且唯若 finished_at 不為 NULL
        ({"status": "succeeded"}, "ck_jobs_finished_matches_status"),
        ({"status": "queued", "finished_at": _NOW}, "ck_jobs_finished_matches_status"),
        ({"attempt": -1}, "ck_jobs_attempt_range"),
        ({"max_attempts": 0}, "ck_jobs_attempt_range"),
        ({"processed_files": -1}, "ck_jobs_progress_nonnegative"),
        ({"bytes_received": -5}, "ck_jobs_progress_nonnegative"),
    ],
)
def test_job_check_constraints_reject_invalid_rows(
    db_engine: Engine, clean: None, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint), db_engine.begin() as connection:
        _insert_job(connection, **overrides)


def test_dedupe_index_rejects_two_active_jobs_but_allows_finished_duplicates(
    db_engine: Engine, clean: None
) -> None:
    with db_engine.begin() as connection:
        _insert_job(connection, dedupe_key="same", status="queued")
        _insert_job(connection, dedupe_key="same", status="succeeded", finished_at=_NOW)
        _insert_job(connection, dedupe_key="same", status="failed", finished_at=_NOW)
        _insert_job(connection, dedupe_key="same", status="cancelled", finished_at=_NOW)

    with pytest.raises(IntegrityError, match="uq_jobs_dedupe_key_active"), db_engine.begin() as c:
        _insert_job(c, dedupe_key="same", status="queued")

    with pytest.raises(IntegrityError, match="uq_jobs_dedupe_key_active"), db_engine.begin() as c:
        _insert_job(
            c,
            dedupe_key="same",
            status="running",
            lease_token=uuid.uuid4(),
            lease_expires_at=_NOW,
        )


def test_a_finished_job_blocks_no_one_and_requeueing_it_conflicts_with_an_active_one(
    db_engine: Engine, clean: None
) -> None:
    with db_engine.begin() as connection:
        failed = _insert_job(connection, dedupe_key="x", status="failed", finished_at=_NOW)
        _insert_job(connection, dedupe_key="x", status="queued")

    with pytest.raises(IntegrityError, match="uq_jobs_dedupe_key_active"), db_engine.begin() as c:
        c.execute(
            text("UPDATE jobs SET status = 'queued', finished_at = NULL WHERE id = :i"),
            {"i": failed},
        )


def _execute_expecting(db_engine: Engine, constraint: str, sql: str, **params: Any) -> None:
    """執行一個語句並斷言它違反指定名稱的約束。"""
    with pytest.raises(IntegrityError, match=constraint), db_engine.begin() as connection:
        connection.execute(text(sql), params)


def test_dispatch_unique_per_attempt_and_check_constraints(db_engine: Engine, clean: None) -> None:
    with db_engine.begin() as connection:
        job_id = _insert_job(connection)
        connection.execute(
            text("INSERT INTO job_dispatches (job_id, attempt_target) VALUES (:j, 1)"),
            {"j": job_id},
        )

    insert = "INSERT INTO job_dispatches (job_id, attempt_target{extra}) VALUES (:j, {target}{v})"
    _execute_expecting(
        db_engine,
        "uq_job_dispatches_job_id_attempt_target",
        insert.format(extra="", target=1, v=""),
        j=job_id,
    )
    _execute_expecting(
        db_engine,
        "ck_job_dispatches_status_valid",
        insert.format(extra=", status", target=2, v=", 'done'"),
        j=job_id,
    )
    _execute_expecting(
        db_engine,
        "ck_job_dispatches_counts_valid",
        insert.format(extra="", target=0, v=""),
        j=job_id,
    )


def test_service_heartbeats_primary_key_and_service_check(db_engine: Engine, clean: None) -> None:
    insert = "INSERT INTO service_heartbeats (service, instance_id) VALUES ('{}', '{}')"
    with db_engine.begin() as connection:
        connection.execute(text(insert.format("worker", "w1")))

    _execute_expecting(db_engine, "pk_service_heartbeats", insert.format("worker", "w1"))
    _execute_expecting(db_engine, "ck_service_heartbeats_service_valid", insert.format("beat", "x"))


# --- ON DELETE 與 D33 ---


def _repository_with_snapshot(connection: Connection) -> tuple[int, int]:
    repository_id = int(
        connection.execute(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('r', '/r') RETURNING id"
            )
        ).scalar_one()
    )
    snapshot_id = int(
        connection.execute(
            text(
                "INSERT INTO snapshots (repository_id, commit_sha, import_profile_hash, status) "
                "VALUES (:r, :c, 'h', 'completed') RETURNING id"
            ),
            {"r": repository_id, "c": "a" * 40},
        ).scalar_one()
    )
    return repository_id, snapshot_id


def test_deleting_a_snapshot_keeps_the_job_and_nulls_its_snapshot_id(
    db_engine: Engine, clean: None
) -> None:
    with db_engine.begin() as connection:
        repository_id, snapshot_id = _repository_with_snapshot(connection)
        job_id = _insert_job(connection, repository_id=repository_id, snapshot_id=snapshot_id)

    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM snapshots WHERE id = :i"), {"i": snapshot_id})

    with db_engine.connect() as connection:
        row = connection.execute(
            text("SELECT repository_id, snapshot_id FROM jobs WHERE id = :i"), {"i": job_id}
        ).one()
    assert (row[0], row[1]) == (repository_id, None)


def test_deleting_a_repository_cascades_to_its_jobs_and_dispatches_only(
    db_engine: Engine, clean: None
) -> None:
    with db_engine.begin() as connection:
        repository_id, _ = _repository_with_snapshot(connection)
        # 快照要先刪（snapshots → repositories 沒有 CASCADE，這是 D33 既有的順序）
        connection.execute(text("DELETE FROM snapshots"))
        linked = _insert_job(connection, repository_id=repository_id)
        unlinked = _insert_job(connection, kind="github_register", repository_id=None)
        for job_id in (linked, unlinked):
            connection.execute(
                text("INSERT INTO job_dispatches (job_id, attempt_target) VALUES (:j, 1)"),
                {"j": job_id},
            )

    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM repositories WHERE id = :i"), {"i": repository_id})

    with db_engine.connect() as connection:
        remaining = connection.execute(text("SELECT id FROM jobs")).scalars().all()
        dispatched = connection.execute(text("SELECT job_id FROM job_dispatches")).scalars().all()
    assert remaining == [unlinked] and dispatched == [unlinked]


def test_removing_a_repository_through_the_api_works_with_existing_jobs(
    client: TestClient, roots: Path, db_engine: Engine, settings: Settings
) -> None:
    """D33 的刪除順序沒有改動：有 job（連到快照者）的 repo 在 job 結束後仍可移除，不留孤兒。"""
    repo_dir = init_repo(roots / "demo", files={"a.txt": "a\n"})
    repository_id = client.post("/api/v1/repositories", json={"path": str(repo_dir)}).json()["id"]
    snapshot_id = client.post(f"/api/v1/repositories/{repository_id}/imports", json={}).json()["id"]

    with Session(db_engine) as session:
        running = job_service.create_job(
            session,
            kind="snapshot_import",
            dedupe_key="import:running",
            settings=settings,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        ).job
        acquired = job_service.acquire_lease(session, running.id, settings)
        assert acquired is not None and acquired.lease_token is not None
        queued = job_service.create_job(
            session,
            kind="snapshot_index",
            dedupe_key="index:queued",
            settings=settings,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
        ).job
        unrelated = job_service.create_job(
            session, kind="github_register", dedupe_key="register:o/r", settings=settings
        ).job
        ids = (running.id, queued.id, unrelated.id)

    # 第二階段切片 5（規格 5.11）：進行中的 job 會擋住移除；這個測試原本期待它們被連帶刪除，
    # 現在改為先確認被擋下（409），再讓它們結束，驗證刪除順序與外鍵（CASCADE）仍然正確
    blocked = client.delete(f"/api/v1/repositories/{repository_id}")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "repository_busy"
    with Session(db_engine) as session:
        job_service.complete_job(session, running.id, acquired.lease_token, snapshot_id=snapshot_id)
        job_service.cancel_job(session, queued.id)

    response = client.delete(f"/api/v1/repositories/{repository_id}")

    assert response.status_code == 200
    assert response.json()["deleted_snapshots"] == 1
    with db_engine.connect() as connection:
        remaining = connection.execute(text("SELECT id FROM jobs ORDER BY id")).scalars().all()
        dispatched = connection.execute(text("SELECT job_id FROM job_dispatches")).scalars().all()
    assert remaining == [ids[2]]  # 只剩沒有 repository 的 github_register
    assert dispatched == [ids[2]]
    assert client.get(f"/api/v1/jobs/{ids[0]}").status_code == 404  # 連帶刪除的 job 回 404
