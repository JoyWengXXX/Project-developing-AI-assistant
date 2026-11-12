from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    """建立 Engine；此時不會連線，連線在第一次使用時才建立。

    `hide_parameters=True`：SQLAlchemy 的例外文字預設附帶 `[parameters: ...]`，匯入寫入失敗時
    其中有檔案內容；日誌（`unhandled_exception`）因此會洩漏內容（D37）。隱藏後例外文字仍有
    例外型別、`[SQL: ...]` 語句與資料庫端訊息，只是看不到綁定的參數值。
    """
    return create_engine(
        settings.database_url.get_secret_value(),
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency：一個請求一個 Session，結束時關閉；交易邊界由 service 控制。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session
