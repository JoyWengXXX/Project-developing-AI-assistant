"""切片 6：真實路由上的未預期例外 → 統一的 500，不洩漏內部資訊，且帶 request ID。

`test_app_pipeline.py` 以測試專用路由驗證了 middleware；這裡對**每個真實端點**注入未預期的例外，
確認整條管線（dependency、handler、middleware）對正式路由同樣成立。不需要資料庫：
service 函式在連線前就被替換，Session 只是延遲建立。
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError, IntegrityError

from repopilot.config import Settings
from repopilot.main import create_app

SECRET = "SECRET-INTERNAL-DETAIL-postgresql://user:hunter2@10.0.0.5/db"

# (HTTP 方法, 路徑, 要注入例外的 service 函式，以 module 路徑表示)
ENDPOINTS = [
    ("GET", "/api/v1/repositories", "repopilot.services.repositories.list_repositories"),
    ("GET", "/api/v1/repositories/1", "repopilot.services.repositories.get_repository"),
    ("POST", "/api/v1/repositories/1/imports", "repopilot.services.snapshots.import_snapshot"),
    ("DELETE", "/api/v1/repositories/1", "repopilot.services.repository_removal.remove_repository"),
    ("GET", "/api/v1/repositories/1/refs", "repopilot.services.repository_history.get_refs"),
    ("GET", "/api/v1/repositories/1/commits", "repopilot.services.repository_history.get_commits"),
    (
        "GET",
        "/api/v1/repositories/1/overview",
        "repopilot.services.repository_overview.get_overview",
    ),
    (
        "GET",
        "/api/v1/repositories/1/snapshots",
        "repopilot.services.snapshot_queries.list_snapshots",
    ),
    ("GET", "/api/v1/snapshots/1", "repopilot.services.snapshot_queries.get_snapshot"),
    ("GET", "/api/v1/snapshots/1/files", "repopilot.services.snapshot_queries.list_files"),
    ("GET", "/api/v1/snapshots/1/files/1", "repopilot.services.snapshot_queries.get_file"),
]

EXCEPTIONS: dict[str, Callable[[], Exception]] = {
    "runtime": lambda: RuntimeError(SECRET),
    "key": lambda: KeyError(SECRET),
    "zero-division": lambda: ZeroDivisionError(SECRET),
    "data-error": lambda: DataError("SELECT :p", {"p": SECRET}, Exception(SECRET)),
    "integrity-error": lambda: IntegrityError("INSERT :p", {"p": SECRET}, Exception(SECRET)),
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(Settings(log_level="INFO"))) as test_client:
        yield test_client


@pytest.mark.parametrize("exception", list(EXCEPTIONS))
@pytest.mark.parametrize(
    ("method", "path", "target"), ENDPOINTS, ids=[e[2].rsplit(".", 1)[1] for e in ENDPOINTS]
)
def test_unexpected_exception_on_a_real_endpoint_returns_unified_500_without_leaking(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    target: str,
    exception: str,
) -> None:
    def explode(*_: Any, **__: Any) -> None:
        raise EXCEPTIONS[exception]()

    monkeypatch.setattr(target, explode)

    response = client.request(method, path, headers={"X-Request-ID": "probe-500"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "probe-500"
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error.",
            "request_id": "probe-500",
        }
    }
    assert SECRET not in response.text
    assert "hunter2" not in str(response.headers)
