"""SQL 索引的 PostgreSQL 與 API 驗證。"""

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build


@pytest.fixture
def sql_snapshot(db_engine: Engine, clean_repositories: None, settings: Settings) -> int:
    with db_engine.begin() as connection:
        repository = int(
            connection.execute(
                text(
                    "INSERT INTO repositories (name, canonical_local_path) "
                    "VALUES ('r', '/r') RETURNING id"
                )
            ).scalar_one()
        )
    snapshot = add_snapshot(db_engine, repository)
    add_files(
        db_engine,
        snapshot,
        {
            "schema.postgres.sql": "CREATE TABLE public.pg_items (id INTEGER);\n",
            "schema.tsql.sql": (
                "CREATE PROCEDURE [dbo].[ts_proc] AS SELECT TOP (1) id FROM items;\nGO\n"
            ),
            "schema.mysql.sql": (
                "CREATE TABLE `mysql_items` (`id` INT AUTO_INCREMENT PRIMARY KEY);\n"
            ),
            "schema.sqlite.sql": "CREATE VIEW sqlite_view AS SELECT id FROM items;\n",
            "unknown.sql": "CREATE TABLE unknown_items (id INTEGER);\n",
            "long.postgres.sql": f'CREATE TABLE "{"x" * 2001}" (id INTEGER);\n',
            "main.py": "def main(): pass\n",
        },
    )
    with Session(db_engine, expire_on_commit=False) as session:
        build(db_engine, session, snapshot, settings)
    return snapshot


def test_sql_index_exposes_dialects_and_unknown(client: TestClient, sql_snapshot: int) -> None:
    base = f"/api/v1/snapshots/{sql_snapshot}"
    index = client.get(f"{base}/index")
    assert index.status_code == 200
    assert index.json()["counts"]["files_by_language"] == {"sql": 6, "python": 1}
    problems = client.get(f"{base}/index/files", params={"has_problems": True})
    assert problems.status_code == 200
    assert [(item["path"], item["error_kind"]) for item in problems.json()["items"]] == [
        ("long.postgres.sql", "sql_name_too_long"),
        ("unknown.sql", "dialect_unspecified"),
    ]
    for query, kind, expected in [
        ("pg_items", "table", "public.pg_items"),
        ("ts_proc", "procedure", "dbo.ts_proc"),
        ("mysql_items", "table", "mysql_items"),
        ("sqlite_view", "view", "sqlite_view"),
    ]:
        result = client.get(
            f"{base}/search/symbols", params={"q": query, "language": "sql", "kind": kind}
        )
        assert result.status_code == 200
        assert [item["qualified_name"] for item in result.json()["items"]] == [expected]


def test_sql_index_prevents_downgrade_without_deleting_data(
    db_engine: Engine, sql_snapshot: int, test_database_url: str
) -> None:
    with pytest.raises(IntegrityError):
        command.downgrade(alembic_config(test_database_url), "0012")
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
        assert (
            connection.scalar(text("SELECT count(*) FROM indexed_files WHERE language = 'sql'"))
            == 6
        )
