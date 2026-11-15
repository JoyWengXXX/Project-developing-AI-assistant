from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from repopilot import __version__
from repopilot.api.errors import register_exception_handlers
from repopilot.api.middleware import RequestIdMiddleware
from repopilot.api.routes import (
    github,
    github_jobs,
    github_repositories,
    health,
    jobs,
    ready,
    references,
    repositories,
    retrieval,
    search,
    snapshots,
)
from repopilot.config import Settings, get_settings
from repopilot.db.session import create_db_engine, create_session_factory
from repopilot.logging_config import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """應用程式工廠；以 `uvicorn repopilot.main:create_app --factory` 啟動。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # create_engine 不會連線，因此建立應用程式時不執行 I/O
    engine = create_db_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        yield
        engine.dispose()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(ready.router)
    app.include_router(repositories.router)
    app.include_router(references.router)
    app.include_router(github_repositories.router)
    app.include_router(snapshots.router)
    app.include_router(search.router)
    app.include_router(retrieval.router)
    app.include_router(github.router)
    app.include_router(github_jobs.router)
    app.include_router(jobs.router)
    return app
