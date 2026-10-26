from pydantic import SecretStr
from sqlalchemy.pool import QueuePool

from repopilot.config import Settings
from repopilot.db.session import create_db_engine

FAKE_URL = SecretStr("postgresql+psycopg://user:FAKE_PASSWORD_FOR_TEST@127.0.0.1:1/repopilot")


def test_engine_uses_pool_settings() -> None:
    settings = Settings(
        database_url=FAKE_URL, db_pool_size=3, db_max_overflow=2, db_pool_timeout_seconds=7.5
    )

    pool = create_db_engine(settings).pool

    assert isinstance(pool, QueuePool)
    assert pool.size() == 3
    assert pool._max_overflow == 2  # 公開 API 沒有提供讀取溢出上限的方法
    assert pool.timeout() == 7.5


def test_default_pool_settings_match_documented_values() -> None:
    settings = Settings(database_url=FAKE_URL)

    assert (settings.db_pool_size, settings.db_max_overflow, settings.db_pool_timeout_seconds) == (
        5,
        10,
        30.0,
    )
