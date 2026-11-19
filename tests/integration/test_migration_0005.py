"""migration 0005（repositories 的 GitHub 來源欄位）：既有資料列不受影響、可降版再升版。

測試期間會對共用的測試資料庫降到 0004 再升回 head；以 try/finally 保證結束時回到 head。
"""

from alembic import command
from sqlalchemy import Engine, text

from tests.integration.conftest import alembic_config


def _columns(db_engine: Engine) -> set[str]:
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'repositories'"
            )
        )
        return {row[0] for row in rows}


def test_existing_local_rows_survive_upgrade_and_downgrade_round_trip(
    db_engine: Engine, test_database_url: str, clean_repositories: None
) -> None:
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "0004")
        assert "source_type" not in _columns(db_engine)
        with db_engine.begin() as connection:
            connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))
            connection.execute(
                text("INSERT INTO repositories (name, canonical_local_path) VALUES ('old', '/old')")
            )

        command.upgrade(config, "head")

        with db_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT name, canonical_local_path, source_type, github_owner, github_name, "
                    "github_is_private, github_default_branch FROM repositories"
                )
            ).one()
        assert tuple(row) == ("old", "/old", "local", None, None, None, None)
    finally:
        command.upgrade(config, "head")
        with db_engine.begin() as connection:
            connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))


def test_unique_index_is_partial_and_case_insensitive(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_repositories_github_full_name_lower'"
            )
        ).scalar_one()

    assert "UNIQUE" in definition
    assert "lower((github_owner)::text)" in definition
    assert "lower((github_name)::text)" in definition
    assert "source_type" in definition.split("WHERE", 1)[1]
    assert "github" in definition.split("WHERE", 1)[1]
