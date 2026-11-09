import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from repopilot.config import Settings
from repopilot.main import create_app

# 預設對應 docker-compose.yml 的 db-test（假密碼，僅限本機）
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://repopilot:repopilot_test@127.0.0.1:5433/repopilot_test"
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    database = make_url(url).database or ""
    # 保護機制：避免誤連開發資料庫
    if "test" not in database:
        pytest.fail(f"TEST_DATABASE_URL 的資料庫名稱必須含 'test'：{database!r}")
    return url


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture(scope="session")
def db_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(test_database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        pytest.fail(
            "無法連線測試資料庫；請先執行 `docker compose up -d --wait db-test`。"
            f"（{type(exc).__name__}）"
        )
    # 重建 schema，確保 migration 從空資料庫開始；只影響名稱含 test 的資料庫
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(test_database_url), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    directory = tmp_path / "repos"
    directory.mkdir()
    return directory


@pytest.fixture
def clean_repositories(db_engine: Engine) -> None:
    # 只清除名稱含 test 的測試資料庫（由 db_engine fixture 保證）；CASCADE 一併清除快照
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))


@pytest.fixture
def settings(test_database_url: str, roots: Path) -> Settings:
    return Settings(database_url=SecretStr(test_database_url), repository_roots=[roots])


@pytest.fixture
def client(settings: Settings, clean_repositories: None) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
