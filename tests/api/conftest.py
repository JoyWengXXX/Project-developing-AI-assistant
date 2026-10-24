from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from repopilot.config import Settings
from repopilot.errors import RepoPilotError
from repopilot.main import create_app
from repopilot.request_context import request_id_var


class FakeConflictError(RepoPilotError):
    code = "fake_conflict"


class FakeUnmappedError(RepoPilotError):
    code = "fake_unmapped"


def _add_probe_routes(app: FastAPI) -> None:
    """只供測試的路由，用來觸發各類錯誤並經過真實的 middleware 與 handler。"""

    @app.get("/_test/items/{item_id}")
    def get_item(item_id: int, limit: int = 10) -> dict[str, int]:
        return {"item_id": item_id, "limit": limit}

    @app.get("/_test/request-id")
    def current_request_id() -> dict[str, str | None]:
        return {"request_id": request_id_var.get()}

    @app.get("/_test/boom")
    def boom() -> None:
        raise RuntimeError("internal detail that must not leak")

    @app.get("/_test/conflict")
    def conflict() -> None:
        raise FakeConflictError("Fake conflict.")

    @app.get("/_test/unmapped")
    def unmapped() -> None:
        raise FakeUnmappedError("Unmapped domain error.")


@pytest.fixture
def app() -> FastAPI:
    application = create_app(Settings(log_level="INFO"))
    _add_probe_routes(application)
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
