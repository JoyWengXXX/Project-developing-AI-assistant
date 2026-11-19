from collections.abc import Iterator

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from repopilot.config import Settings
from repopilot.main import create_app
from tests.integration.conftest import alembic_config

VERSION_QUERY = "SELECT version_num FROM alembic_version"


@pytest.fixture
def client(test_database_url: str, db_engine: Engine) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=SecretStr(test_database_url)))
    with TestClient(app) as test_client:
        yield test_client


def test_ready_returns_200_when_database_is_reachable(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_migrations_upgrade_empty_database_to_head(
    test_database_url: str, db_engine: Engine
) -> None:
    head = ScriptDirectory.from_config(alembic_config(test_database_url)).get_current_head()

    with db_engine.connect() as connection:
        current = connection.execute(text(VERSION_QUERY)).scalars().all()

    assert current == [head]


def test_pgvector_is_installed_and_can_calculate_distance(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        version = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        distance = connection.scalar(text("SELECT '[1,0]'::vector <-> '[0,1]'::vector"))
    assert version is not None
    assert distance == pytest.approx(2**0.5)


def test_downgrade_preserves_existing_pgvector_extension(
    test_database_url: str, db_engine: Engine, clean_repositories: None
) -> None:
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "0014")
        with db_engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
                )
                == 1
            )
            assert connection.scalar(text("SELECT '[1,0]'::vector <-> '[1,0]'::vector")) == 0
    finally:
        command.upgrade(config, "head")


def test_upgrade_keeps_preexisting_vector_data(
    test_database_url: str, db_engine: Engine, clean_repositories: None
) -> None:
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "0014")
        with db_engine.connect() as connection:
            connection.execute(text("CREATE TEMP TABLE existing_vectors (embedding vector(2))"))
            connection.execute(text("INSERT INTO existing_vectors VALUES ('[1,2]')"))
            connection.commit()
            command.upgrade(config, "head")
            assert (
                connection.scalar(
                    text("SELECT embedding <-> '[1,2]'::vector FROM existing_vectors")
                )
                == 0
            )
    finally:
        command.upgrade(config, "head")


def test_migration_upgrade_is_repeatable_and_downgrade_to_base_works(
    test_database_url: str, db_engine: Engine, clean_repositories: None
) -> None:
    config = alembic_config(test_database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    with db_engine.connect() as connection:
        versions = connection.execute(text(VERSION_QUERY)).scalars().all()
    assert len(versions) == 1


def test_models_and_migrations_have_no_drift(test_database_url: str, db_engine: Engine) -> None:
    """等同 `alembic check`：從空資料庫套用 migration 後，ORM model 沒有尚未寫成 migration 的差異。

    只涵蓋 Alembic 能比對的項目（資料表、欄位、型別、索引、唯一與外鍵約束）；CHECK 約束與
    部分索引的運算式不在其比對範圍內，由約束的直接測試負責。
    """
    command.check(alembic_config(test_database_url))


def test_every_migration_step_upgrades_and_downgrades_one_revision_at_a_time(
    test_database_url: str, db_engine: Engine, clean_repositories: None
) -> None:
    config = alembic_config(test_database_url)
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1, "migration 鏈不可分岔"
    revisions = [revision.revision for revision in reversed(list(script.walk_revisions()))]
    assert len(revisions) >= 5  # 0001 ～ 0005

    def current() -> list[str]:
        with db_engine.connect() as connection:
            return list(connection.execute(text(VERSION_QUERY)).scalars().all())

    try:
        command.downgrade(config, "base")
        assert current() == []
        for revision in revisions:  # 空資料庫逐版升級
            command.upgrade(config, "+1")
            assert current() == [revision]
        for previous in [*reversed(revisions[:-1]), None]:  # 逐版降回 base
            command.downgrade(config, "-1")
            assert current() == ([previous] if previous else [])
    finally:
        command.upgrade(config, "head")  # 不論成敗都還原，避免影響之後的測試

    with db_engine.connect() as connection:
        tables = set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert {"repositories", "snapshots", "snapshot_files", "github_credentials"} <= tables
