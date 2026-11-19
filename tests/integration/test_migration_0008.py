"""migration 0008（結構索引資料表）。

升降版測試會對共用的測試資料庫降到 0007 再升回 head；以 try/finally 保證結束時回到 head。
"""

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot

INDEX_TABLES = {
    "snapshot_indexes",
    "indexed_files",
    "symbols",
    "import_references",
    "symbol_relations",
}


def _tables(db_engine: Engine) -> set[str]:
    with db_engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
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


def _file_ids(db_engine: Engine, snapshot_id: int) -> list[int]:
    with db_engine.connect() as connection:
        return list(
            connection.execute(
                text("SELECT id FROM snapshot_files WHERE snapshot_id = :s ORDER BY id"),
                {"s": snapshot_id},
            ).scalars()
        )


def _index(
    db_engine: Engine, snapshot_id: int, profile: str = "p", status: str = "completed"
) -> int:
    with db_engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO snapshot_indexes (snapshot_id, index_profile_hash, status, "
                    "owner_token) VALUES (:s, :p, :st, "
                    "CASE WHEN CAST(:st AS varchar) = 'building' THEN gen_random_uuid() END) "
                    "RETURNING id"
                ),
                {"s": snapshot_id, "p": profile, "st": status},
            ).scalar_one()
        )


def _indexed_file(db_engine: Engine, index_id: int, file_id: int) -> int:
    with db_engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO indexed_files (snapshot_index_id, snapshot_file_id, language, "
                    "parse_status) VALUES (:i, :f, 'python', 'parsed') RETURNING id"
                ),
                {"i": index_id, "f": file_id},
            ).scalar_one()
        )


def _symbol(
    db_engine: Engine, index_id: int, file_id: int, name: str, parent: int | None = None
) -> int:
    with db_engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO symbols (snapshot_index_id, indexed_file_id, parent_symbol_id, "
                    "language, kind, name, qualified_name, start_line, end_line, start_column, "
                    "end_column) VALUES (:i, :f, :p, 'python', 'class', :n, :n, 1, 1, 0, 1) "
                    "RETURNING id"
                ),
                {"i": index_id, "f": file_id, "p": parent, "n": name},
            ).scalar_one()
        )


def _count(db_engine: Engine, table: str) -> int:
    with db_engine.connect() as connection:
        return int(
            connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
        )


def test_alembic_check_reports_no_drift_for_the_index_tables(
    db_engine: Engine, test_database_url: str
) -> None:
    assert _tables(db_engine) >= INDEX_TABLES
    command.check(alembic_config(test_database_url))


def test_a_symbol_cannot_point_at_a_file_of_another_index(
    db_engine: Engine, clean_repositories: None
) -> None:
    repository_id = _repository(db_engine, "r")
    first = add_snapshot(db_engine, repository_id, files=1, index=1)
    second = add_snapshot(db_engine, repository_id, files=1, index=2)
    index_a = _index(db_engine, first)
    index_b = _index(db_engine, second)
    file_a = _indexed_file(db_engine, index_a, _file_ids(db_engine, first)[0])

    with (
        pytest.raises(IntegrityError, match="fk_symbols_indexed_file_id_indexed_files"),
        db_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO symbols (snapshot_index_id, indexed_file_id, language, kind, name, "
                "qualified_name, start_line, end_line, start_column, end_column) "
                "VALUES (:i, :f, 'python', 'class', 'A', 'A', 1, 1, 0, 1)"
            ),
            {"i": index_b, "f": file_a},
        )


def test_a_parent_symbol_and_a_relation_target_must_belong_to_the_same_index(
    db_engine: Engine, clean_repositories: None
) -> None:
    repository_id = _repository(db_engine, "r")
    first = add_snapshot(db_engine, repository_id, files=1, index=1)
    second = add_snapshot(db_engine, repository_id, files=1, index=2)
    index_a, index_b = _index(db_engine, first), _index(db_engine, second)
    file_a = _indexed_file(db_engine, index_a, _file_ids(db_engine, first)[0])
    file_b = _indexed_file(db_engine, index_b, _file_ids(db_engine, second)[0])
    parent_a = _symbol(db_engine, index_a, file_a, "A")
    child_b = _symbol(db_engine, index_b, file_b, "B")

    with (
        pytest.raises(IntegrityError, match="fk_symbols_parent_symbol_id_symbols"),
        db_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO symbols (snapshot_index_id, indexed_file_id, parent_symbol_id, "
                "language, kind, name, qualified_name, start_line, end_line, start_column, "
                "end_column) VALUES (:i, :f, :p, 'python', 'class', 'C', 'C', 1, 1, 0, 1)"
            ),
            {"i": index_b, "f": file_b, "p": parent_a},
        )
    with (
        pytest.raises(IntegrityError, match="fk_symbol_relations_target_symbol_id_symbols"),
        db_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO symbol_relations (snapshot_index_id, source_symbol_id, "
                "relation_type, target_name, target_symbol_id, resolution, line) "
                "VALUES (:i, :s, 'base_class', 'A', :t, 'resolved_same_file', 1)"
            ),
            {"i": index_b, "s": child_b, "t": parent_a},
        )


def test_building_requires_an_owner_token_and_other_states_must_not_have_one(
    db_engine: Engine, clean_repositories: None
) -> None:
    repository_id = _repository(db_engine, "r")
    snapshot_id = add_snapshot(db_engine, repository_id)
    for status, has_owner in (("building", False), ("completed", True)):
        with (
            pytest.raises(IntegrityError, match="ck_snapshot_indexes_owner_matches_status"),
            db_engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "INSERT INTO snapshot_indexes (snapshot_id, index_profile_hash, status, "
                    "owner_token) VALUES (:s, 'h', :st, "
                    "CASE WHEN :owner THEN gen_random_uuid() END)"
                ),
                {"s": snapshot_id, "st": status, "owner": has_owner},
            )


def test_one_index_per_snapshot_and_profile(db_engine: Engine, clean_repositories: None) -> None:
    repository_id = _repository(db_engine, "r")
    snapshot_id = add_snapshot(db_engine, repository_id)
    _index(db_engine, snapshot_id, profile="same")
    _index(db_engine, snapshot_id, profile="other")  # 不同設定可並存
    with (
        pytest.raises(IntegrityError, match="uq_snapshot_indexes_snapshot_id_index_profile_hash"),
        db_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO snapshot_indexes (snapshot_id, index_profile_hash, status) "
                "VALUES (:s, 'same', 'failed')"
            ),
            {"s": snapshot_id},
        )


def test_deleting_snapshot_files_with_sql_cascades_through_the_whole_index(
    db_engine: Engine, clean_repositories: None
) -> None:
    """D33 以 SQL 先刪 snapshot_files 再刪 snapshots：衍生資料必須隨之消失。"""
    repository_id = _repository(db_engine, "r")
    snapshot_id = add_snapshot(db_engine, repository_id, files=2)
    index_id = _index(db_engine, snapshot_id)
    file_id = _indexed_file(db_engine, index_id, _file_ids(db_engine, snapshot_id)[0])
    parent = _symbol(db_engine, index_id, file_id, "A")
    child = _symbol(db_engine, index_id, file_id, "B", parent=parent)
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO import_references (snapshot_index_id, indexed_file_id, "
                "source_symbol_id, language, kind, target, line) "
                "VALUES (:i, :f, :s, 'python', 'import', 'os', 1)"
            ),
            {"i": index_id, "f": file_id, "s": parent},
        )
        connection.execute(
            text(
                "INSERT INTO symbol_relations (snapshot_index_id, source_symbol_id, "
                "relation_type, target_name, target_symbol_id, resolution, line) "
                "VALUES (:i, :s, 'base_class', 'A', :t, 'resolved_same_file', 1)"
            ),
            {"i": index_id, "s": child, "t": parent},
        )

    with db_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM snapshot_files WHERE snapshot_id = :s"), {"s": snapshot_id}
        )

    for table in ("indexed_files", "symbols", "import_references", "symbol_relations"):
        assert _count(db_engine, table) == 0, table
    assert _count(db_engine, "snapshot_indexes") == 1  # 索引本身隨快照刪除
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM snapshots WHERE id = :s"), {"s": snapshot_id})
    assert _count(db_engine, "snapshot_indexes") == 0


def test_downgrade_drops_only_the_index_tables_and_keeps_snapshot_data(
    db_engine: Engine, test_database_url: str, clean_repositories: None
) -> None:
    config = alembic_config(test_database_url)
    repository_id = _repository(db_engine, "keep")
    snapshot_id = add_snapshot(db_engine, repository_id, files=3)
    _index(db_engine, snapshot_id)
    try:
        command.downgrade(config, "0007")
        assert not INDEX_TABLES & _tables(db_engine)
        assert _count(db_engine, "snapshots") == 1  # 第一、二階段的資料原封不動
        assert _count(db_engine, "snapshot_files") == 3

        command.upgrade(config, "head")

        assert _tables(db_engine) >= INDEX_TABLES
        assert _count(db_engine, "snapshots") == 1
        assert _count(db_engine, "snapshot_indexes") == 0  # 索引隨降版消失（可重建）
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as connection:
            connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))
