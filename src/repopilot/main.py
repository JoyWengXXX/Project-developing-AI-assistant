from fastapi import FastAPI

from repopilot import __version__
from repopilot.api.errors import register_exception_handlers
from repopilot.api.middleware import RequestIdMiddleware
from repopilot.api.routes import health
from repopilot.config import Settings, get_settings
from repopilot.logging_config import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """應用程式工廠；以 `uvicorn repopilot.main:create_app --factory` 啟動。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version=__version__)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    return app
