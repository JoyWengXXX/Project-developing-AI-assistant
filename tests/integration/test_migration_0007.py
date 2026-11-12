"""migration 0007（repository_references 與 snapshots 的 (id, repository_id) 唯一約束）。

升降版測試會對共用的測試資料庫降到 0006 再升回 head；以 try/finally 保證結束時回到 head。
"""

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot


def _tables(db_engine: Engine) -> set[str]:
    with db_engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _constraints(db_engine: Engine, table: str) -> set[str]:
    with db_engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"),
                {"t": table},
            ).scalars()
        )


def _repository(db_engine: Engine, name: str) -> int:
    with db_engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO repositories (name, canonical_local_path) "
                    "VALUES (:n, :p) RETURNING id"
                ),
                {"n": name, "p": f"/{name}"},
            ).scalar_one()
        )


def test_alembic_check_reports_no_drift_for_the_reference_table(
    db_engine: Engine, test_database_url: str
) -> None:
    command.check(alembic_config(test_database_url))


def test_upgrade_creates_the_expected_constraints(db_engine: Engine) -> None:
    assert "repository_references" in _tables(db_engine)
    assert "uq_snapshots_id_repository_id" in _constraints(db_engine, "snapshots")
    assert {
        "pk_repository_references",
        "uq_repository_references_repository_id",
        "fk_repository_references_repository_id_repositories",
        "fk_repository_references_snapshot_id_snapshots",
    } <= _constraints(db_engine, "repository_references")


def test_both_foreign_keys_cascade_on_delete(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        rules = {
            row[0]: row[1]
            for row in connection.execute(
                text(
                    "SELECT conname, confdeltype FROM pg_constraint "
                    "WHERE conrelid = 'repository_references'::regclass AND contype = 'f'"
                )
            )
        }
    assert rules == {
        "fk_repository_references_repository_id_repositories": "c",
        "fk_repository_references_snapshot_id_snapshots": "c",
    }


def test_downgrade_drops_only_the_reference_table_and_keeps_snapshot_data(
    db_engine: Engine, test_database_url: str, clean_repositories: None
) -> None:
    config = alembic_config(test_database_url)
    repository_id = _repository(db_engine, "keep")
    snapshot_id = add_snapshot(db_engine, repository_id, files=3)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"),
            {"r": repository_id, "s": snapshot_id},
        )
    try:
        command.downgrade(config, "0006")
        assert "repository_references" not in _tables(db_engine)
        assert "uq_snapshots_id_repository_id" not in _constraints(db_engine, "snapshots")
        with db_engine.connect() as connection:  # 第一、二階段的資料原封不動
            assert connection.execute(text("SELECT count(*) FROM snapshots")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM snapshot_files")).scalar_one() == 3

        command.upgrade(config, "head")

        assert "repository_references" in _tables(db_engine)
        with db_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM snapshots")).scalar_one() == 1
            # 參考標記隨降版消失（已記錄於 migration 與 D41）
            refs = connection.execute(text("SELECT count(*) FROM repository_references"))
            assert refs.scalar_one() == 0
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as connection:
            connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))


def test_upgrade_with_existing_snapshots_satisfies_the_new_unique_constraint(
    db_engine: Engine, test_database_url: str, clean_repositories: None
) -> None:
    """既有資料（0006 時期建立的快照）升級時不會違反 (id, repository_id) 唯一約束。"""
    config = alembic_config(test_database_url)
    first = _repository(db_engine, "one")
    second = _repository(db_engine, "two")
    try:
        command.downgrade(config, "0006")
        with db_engine.begin() as connection:
            for repository_id in (first, second):
                for number in range(1, 4):
                    connection.execute(
                        text(
                            "INSERT INTO snapshots (repository_id, commit_sha, "
                            "import_profile_hash, status) VALUES (:r, :c, 'h', 'completed')"
                        ),
                        {"r": repository_id, "c": f"{number:040x}"},
                    )

        command.upgrade(config, "head")  # 不得失敗

        with db_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM snapshots")).scalar_one() == 6
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as connection:
            connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))


def test_a_referenced_snapshot_cannot_be_moved_to_another_repository(
    db_engine: Engine, clean_repositories: None
) -> None:
    mine = _repository(db_engine, "mine")
    theirs = _repository(db_engine, "theirs")
    snapshot_id = add_snapshot(db_engine, mine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"),
            {"r": mine, "s": snapshot_id},
        )

    with (
        pytest.raises(IntegrityError, match="fk_repository_references_snapshot_id_snapshots"),
        db_engine.begin() as connection,
    ):
        connection.execute(
            text("UPDATE snapshots SET repository_id = :r WHERE id = :s"),
            {"r": theirs, "s": snapshot_id},
        )
