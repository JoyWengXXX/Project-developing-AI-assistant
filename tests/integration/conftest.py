import os
from collections.abc import Iterator
from pathlib import Path

import anthropic
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from repopilot.config import Settings
from repopilot.main import create_app
from repopilot.qa.provider import QAProvider
from repopilot.services import project_ai
from tests.integration.project_env import REFERENCE_FILES
from tests.integration.project_env import Env as ProjectEnv
from tests.integration.snapshot_helpers import add_snapshot
from tests.stack_control import stop_job_services_if_running

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
    # 重建 schema，確保 migration 從空資料庫開始；只影響名稱含 test 的資料庫。
    # 先停止測試堆疊的 worker／dispatcher（若在跑）：它們會處理測試用的假 job，
    # 也會持有舊 schema 的連線
    stop_job_services_if_running()
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


# --- 新專案（第五階段）：假供應商環境；使用的測試檔以 `env` fixture 包裝它 ---


@pytest.fixture
def project_env(
    db_engine: Engine,
    clean_repositories: None,
    test_database_url: str,
    roots: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProjectEnv]:
    """真實 PostgreSQL、一個已完成的參考快照與假供應商；建立真實客戶端會讓測試失敗。"""
    constructed: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        constructed.append(args)
        raise AssertionError("a real Anthropic client must never be constructed in tests")

    monkeypatch.setattr(anthropic, "Anthropic", forbidden)

    def truncate() -> None:
        with db_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE jobs, job_dispatches, qa_settings, repository_qa_consent, "
                    "qa_usage_ledger RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    app_settings = Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    with db_engine.begin() as connection:
        repository_id = connection.scalar(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('reference', '/fake-project-env') RETURNING id"
            )
        )
    assert isinstance(repository_id, int)
    snapshot_id = add_snapshot(db_engine, repository_id)
    # 延後匯入：避免 conftest 載入時匯入測試模組（循環匯入）
    from tests.integration.test_snapshot_indexing import add_files

    add_files(db_engine, snapshot_id, REFERENCE_FILES)
    result = ProjectEnv(TestClient(create_app(app_settings)), db_engine, repository_id, snapshot_id)

    def build_provider(api_key: str) -> QAProvider:
        result.keys_seen.append(api_key)
        return result.provider

    monkeypatch.setattr(project_ai, "build_provider", build_provider)
    yield result
    truncate()
    assert constructed == []
