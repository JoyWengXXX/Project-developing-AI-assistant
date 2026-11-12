"""GitHub token API：不需資料庫的部分（輸入驗證、金鑰設定、GitHub 錯誤對應）。

這些路徑都在寫入資料庫之前就結束，因此不需要 PostgreSQL；成功保存、覆蓋、刪除等見
`tests/integration/test_github_credentials.py`。
"""

from collections.abc import Iterator

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.main import create_app
from tests.github_helpers import FAKE_TOKEN, GITHUB_BODY_MARKER, FakeGitHub, capture_logs

PUT_URL = "/api/v1/github/token"


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


def _make_client(key: str, fake: FakeGitHub) -> Iterator[TestClient]:
    app: FastAPI = create_app(Settings(token_encryption_key=SecretStr(key)))
    app.dependency_overrides[get_github_client] = fake.client
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(fake_github: FakeGitHub) -> Iterator[TestClient]:
    yield from _make_client(Fernet.generate_key().decode(), fake_github)


@pytest.fixture
def client_without_key(fake_github: FakeGitHub) -> Iterator[TestClient]:
    yield from _make_client("", fake_github)


@pytest.fixture
def client_with_invalid_key(fake_github: FakeGitHub) -> Iterator[TestClient]:
    yield from _make_client("this-is-not-a-fernet-key", fake_github)


def test_put_without_encryption_key_returns_503_and_does_not_call_github(
    client_without_key: TestClient, fake_github: FakeGitHub
) -> None:
    response = client_without_key.put(PUT_URL, json={"token": FAKE_TOKEN})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "token_encryption_not_configured"
    assert FAKE_TOKEN not in response.text
    # 無法保存就不該把使用者的 token 送去 GitHub
    assert fake_github.requests == []


def test_put_with_invalid_encryption_key_returns_503_without_echoing_key(
    client_with_invalid_key: TestClient, fake_github: FakeGitHub
) -> None:
    response = client_with_invalid_key.put(PUT_URL, json={"token": FAKE_TOKEN})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "token_encryption_not_configured"
    assert "this-is-not-a-fernet-key" not in response.text
    assert fake_github.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"token": ""},
        {"token": "   "},
        {"token": "SENTINEL with space"},
        {"token": "SENTINEL\nnewline"},
        {"token": "SENTINEL" + "x" * 300},
        {"token": 12345},
        {"token": ["SENTINEL"]},
        {"token": {"SENTINEL": 1}},
        {"token": None},
        {"token": "SENTINEL", "extra": "SENTINEL-EXTRA"},
        {"token": "SENTINEL-全形"},
    ],
    ids=[
        "missing",
        "empty",
        "spaces",
        "inner-space",
        "inner-newline",
        "too-long",
        "number",
        "list",
        "object",
        "null",
        "extra-field",
        "non-ascii",
    ],
)
def test_invalid_body_returns_422_without_echoing_input_or_calling_github(
    client: TestClient, fake_github: FakeGitHub, body: dict[str, object]
) -> None:
    with capture_logs() as logs:
        response = client.put(PUT_URL, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "SENTINEL" not in response.text
    assert "SENTINEL" not in logs.text()
    assert fake_github.requests == []


def test_non_json_body_returns_422_without_echoing_body(client: TestClient) -> None:
    response = client.put(
        PUT_URL, content=b"SENTINEL not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert "SENTINEL" not in response.text


def test_request_body_is_not_logged_even_when_request_fails(
    client: TestClient, fake_github: FakeGitHub
) -> None:
    fake_github.status = 401

    with capture_logs() as logs:
        response = client.put(PUT_URL, json={"token": FAKE_TOKEN})

    assert response.status_code == 422
    assert len(logs) > 0
    assert FAKE_TOKEN not in logs.text()


def test_invalid_token_returns_422_github_token_invalid(
    client: TestClient, fake_github: FakeGitHub
) -> None:
    fake_github.status = 401

    response = client.put(PUT_URL, json={"token": FAKE_TOKEN})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "github_token_invalid"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert FAKE_TOKEN not in response.text
    assert GITHUB_BODY_MARKER not in response.text


@pytest.mark.parametrize(
    ("status", "error", "expected_status", "expected_code"),
    [
        (403, None, 502, "github_api_error"),
        (500, None, 502, "github_api_error"),
        (200, "bad-body", 502, "github_api_error"),
        (200, httpx.ConnectTimeout("t"), 504, "github_timeout"),
        (200, httpx.ReadTimeout("t"), 504, "github_timeout"),
        (200, httpx.ConnectError(f"boom {FAKE_TOKEN}"), 502, "github_unreachable"),
    ],
    ids=["403", "500", "non-json", "connect-timeout", "read-timeout", "connect-error"],
)
def test_github_failures_map_to_status_and_code_without_leaking(
    client: TestClient,
    fake_github: FakeGitHub,
    status: int,
    error: Exception | str | None,
    expected_status: int,
    expected_code: str,
) -> None:
    fake_github.status = status
    if error == "bad-body":
        fake_github.raw_body = f"<html>{GITHUB_BODY_MARKER}</html>".encode()
    elif isinstance(error, Exception):
        fake_github.error = error

    with capture_logs() as logs:
        response = client.put(PUT_URL, json={"token": FAKE_TOKEN})

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    for text in (response.text, logs.text()):
        assert FAKE_TOKEN not in text
        assert GITHUB_BODY_MARKER not in text
