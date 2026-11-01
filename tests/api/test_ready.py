from fastapi.testclient import TestClient
from pydantic import SecretStr

from repopilot.config import Settings
from repopilot.main import create_app

# 埠 1 無人監聽，連線會立即被拒；此測試不需要資料庫
UNREACHABLE_URL = "postgresql+psycopg://user:FAKE_PASSWORD_FOR_TEST@127.0.0.1:1/repopilot"


def test_ready_returns_503_when_database_is_unreachable() -> None:
    settings = Settings(database_url=SecretStr(UNREACHABLE_URL), db_connect_timeout_seconds=1)

    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "database_unavailable"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert "FAKE_PASSWORD_FOR_TEST" not in response.text


def test_repository_routes_return_503_when_database_is_unreachable() -> None:
    settings = Settings(database_url=SecretStr(UNREACHABLE_URL), db_connect_timeout_seconds=1)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/repositories")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


def test_health_does_not_depend_on_database() -> None:
    settings = Settings(database_url=SecretStr(UNREACHABLE_URL), db_connect_timeout_seconds=1)

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
