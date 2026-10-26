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


def test_migration_upgrade_is_repeatable_and_downgrade_to_base_works(
    test_database_url: str, db_engine: Engine
) -> None:
    config = alembic_config(test_database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    with db_engine.connect() as connection:
        versions = connection.execute(text(VERSION_QUERY)).scalars().all()
    assert len(versions) == 1
