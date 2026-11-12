import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.main import create_app
from repopilot.services.github_credentials import (
    CLASSIC_WARNING,
    UNUSABLE_WARNING,
    connect_token,
    get_decrypted_token,
    get_status,
)
from tests.github_helpers import (
    FAKE_CLASSIC_TOKEN,
    FAKE_TOKEN,
    GITHUB_BODY_MARKER,
    FakeGitHub,
    capture_logs,
)

PUT_URL = "/api/v1/github/token"
AUTH_URL = "/api/v1/github/auth"


@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def settings(test_database_url: str, roots: Path, encryption_key: str) -> Settings:
    return Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        token_encryption_key=SecretStr(encryption_key),
    )


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def clean_credentials(db_engine: Engine) -> None:
    # 只清除名稱含 test 的測試資料庫（由 db_engine fixture 保證）
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE github_credentials"))


def _app(settings: Settings, fake: FakeGitHub) -> FastAPI:
    app = create_app(settings)
    app.dependency_overrides[get_github_client] = fake.client
    return app


@pytest.fixture
def client(
    settings: Settings, clean_credentials: None, fake_github: FakeGitHub
) -> Iterator[TestClient]:
    with TestClient(_app(settings, fake_github)) as test_client:
        yield test_client


def _rows(db_engine: Engine) -> list[tuple[int, str, str, str]]:
    with db_engine.connect() as connection:
        result = connection.execute(
            text("SELECT id, encrypted_token, login, token_type FROM github_credentials")
        )
        return [(r[0], r[1], r[2], r[3]) for r in result]


def test_put_then_get_shows_login_and_type_without_any_token(
    client: TestClient, db_engine: Engine
) -> None:
    put = client.put(PUT_URL, json={"token": FAKE_TOKEN})
    get = client.get(AUTH_URL)

    expected = {
        "authenticated": True,
        "login": "octocat",
        "token_type": "fine_grained",
        "warning": None,
    }
    assert put.status_code == 200
    assert put.json() == expected
    assert get.status_code == 200
    assert get.json() == expected
    ciphertext = _rows(db_engine)[0][1]
    for response in (put, get):
        assert FAKE_TOKEN not in response.text
        assert ciphertext not in response.text
        assert set(response.json()) == {"authenticated", "login", "token_type", "warning"}


def test_token_is_stored_encrypted_and_decrypts_with_the_configured_key(
    client: TestClient, db_engine: Engine, encryption_key: str
) -> None:
    client.put(PUT_URL, json={"token": f"  {FAKE_TOKEN}  "})

    rows = _rows(db_engine)

    assert len(rows) == 1
    row_id, ciphertext, login, token_type = rows[0]
    assert (row_id, login, token_type) == (1, "octocat", "fine_grained")
    assert ciphertext != FAKE_TOKEN
    assert FAKE_TOKEN not in ciphertext
    # 前後空白已去除；GitHub 收到的也是去除後的值
    assert Fernet(encryption_key.encode()).decrypt(ciphertext.encode()).decode() == FAKE_TOKEN


def test_token_sent_to_github_is_the_submitted_token(
    client: TestClient, fake_github: FakeGitHub
) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})

    assert [r.headers["Authorization"] for r in fake_github.requests] == [f"Bearer {FAKE_TOKEN}"]


def test_classic_token_returns_warning_on_put_and_get(client: TestClient) -> None:
    put = client.put(PUT_URL, json={"token": FAKE_CLASSIC_TOKEN})
    get = client.get(AUTH_URL)

    for response in (put, get):
        assert response.json()["token_type"] == "classic"
        assert response.json()["warning"] == CLASSIC_WARNING
        assert FAKE_CLASSIC_TOKEN not in response.text


def test_unknown_prefix_is_saved_as_unknown_without_warning(client: TestClient) -> None:
    response = client.put(PUT_URL, json={"token": "TESTONLY-unknown-prefix-token"})

    assert response.status_code == 200
    assert response.json()["token_type"] == "unknown"
    assert response.json()["warning"] is None


def test_put_again_overwrites_and_keeps_a_single_row(
    client: TestClient, fake_github: FakeGitHub, db_engine: Engine, encryption_key: str
) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})
    first_ciphertext = _rows(db_engine)[0][1]
    fake_github.login = "hubot"

    second = client.put(PUT_URL, json={"token": FAKE_CLASSIC_TOKEN})

    rows = _rows(db_engine)
    assert second.status_code == 200
    assert len(rows) == 1
    assert rows[0][1] != first_ciphertext
    assert rows[0][2:] == ("hubot", "classic")
    assert Fernet(encryption_key.encode()).decrypt(rows[0][1].encode()).decode() == (
        FAKE_CLASSIC_TOKEN
    )
    assert client.get(AUTH_URL).json()["login"] == "hubot"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, None),
        (403, None),
        (500, None),
        (200, httpx.ReadTimeout("t")),
        (200, httpx.ConnectError("c")),
    ],
    ids=["401", "403", "500", "timeout", "connect-error"],
)
def test_failed_validation_does_not_change_existing_token(
    client: TestClient,
    fake_github: FakeGitHub,
    db_engine: Engine,
    status: int,
    error: Exception | None,
) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})
    before = _rows(db_engine)
    fake_github.status = status
    fake_github.error = error
    fake_github.login = "intruder"

    response = client.put(PUT_URL, json={"token": FAKE_CLASSIC_TOKEN})

    assert response.status_code >= 400
    assert _rows(db_engine) == before
    fake_github.status = 200
    fake_github.error = None
    assert client.get(AUTH_URL).json()["login"] == "octocat"


def test_failed_first_validation_stores_nothing(
    client: TestClient, fake_github: FakeGitHub, db_engine: Engine
) -> None:
    fake_github.status = 401

    response = client.put(PUT_URL, json={"token": FAKE_TOKEN})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "github_token_invalid"
    assert _rows(db_engine) == []
    assert client.get(AUTH_URL).json() == {
        "authenticated": False,
        "login": None,
        "token_type": None,
        "warning": None,
    }


def test_get_without_saved_token_reports_unauthenticated(client: TestClient) -> None:
    response = client.get(AUTH_URL)

    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "login": None,
        "token_type": None,
        "warning": None,
    }


def test_get_does_not_call_github(client: TestClient, fake_github: FakeGitHub) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})
    calls_after_put = len(fake_github.requests)

    client.get(AUTH_URL)
    client.get(AUTH_URL)

    assert len(fake_github.requests) == calls_after_put


def test_delete_removes_token_and_is_idempotent(client: TestClient, db_engine: Engine) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})

    first = client.delete(AUTH_URL)
    second = client.delete(AUTH_URL)

    assert first.status_code == 204
    assert first.content == b""
    assert second.status_code == 204
    assert _rows(db_engine) == []
    assert client.get(AUTH_URL).json()["authenticated"] is False


def test_delete_without_saved_token_returns_204(client: TestClient) -> None:
    assert client.delete(AUTH_URL).status_code == 204


def test_put_works_again_after_delete(client: TestClient) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})
    client.delete(AUTH_URL)

    response = client.put(PUT_URL, json={"token": FAKE_CLASSIC_TOKEN})

    assert response.status_code == 200
    assert client.get(AUTH_URL).json()["token_type"] == "classic"


def test_put_without_key_returns_503_and_keeps_existing_row(
    settings: Settings,
    clean_credentials: None,
    fake_github: FakeGitHub,
    db_engine: Engine,
    client: TestClient,
) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})
    before = _rows(db_engine)
    keyless = settings.model_copy(update={"token_encryption_key": SecretStr("")})

    with TestClient(_app(keyless, fake_github)) as keyless_client:
        response = keyless_client.put(PUT_URL, json={"token": FAKE_CLASSIC_TOKEN})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "token_encryption_not_configured"
    assert _rows(db_engine) == before


def test_changed_key_makes_stored_token_unusable_but_never_500(
    settings: Settings,
    fake_github: FakeGitHub,
    db_engine: Engine,
    client: TestClient,
) -> None:
    """金鑰更換後：GET 回 authenticated=false 並附提示（200），內部取用回 None；重新 PUT 可恢復。"""
    client.put(PUT_URL, json={"token": FAKE_TOKEN})
    new_key = Fernet.generate_key().decode()
    rotated = settings.model_copy(update={"token_encryption_key": SecretStr(new_key)})

    with TestClient(_app(rotated, fake_github)) as rotated_client, capture_logs() as logs:
        get = rotated_client.get(AUTH_URL)
        factory = sessionmaker(bind=db_engine, expire_on_commit=False)
        with factory() as session:
            decrypted = get_decrypted_token(session, rotated)
        put = rotated_client.put(PUT_URL, json={"token": FAKE_CLASSIC_TOKEN})
        after = rotated_client.get(AUTH_URL)

    assert get.status_code == 200
    assert get.json() == {
        "authenticated": False,
        "login": None,
        "token_type": None,
        "warning": UNUSABLE_WARNING,
    }
    assert decrypted is None
    assert "github_token_unusable" in logs.text()
    assert FAKE_TOKEN not in logs.text()
    assert put.status_code == 200
    assert after.json()["authenticated"] is True
    assert after.json()["token_type"] == "classic"


def test_removed_key_makes_stored_token_unusable(
    settings: Settings, fake_github: FakeGitHub, client: TestClient
) -> None:
    client.put(PUT_URL, json={"token": FAKE_TOKEN})
    keyless = settings.model_copy(update={"token_encryption_key": SecretStr("")})

    with TestClient(_app(keyless, fake_github)) as keyless_client:
        response = keyless_client.get(AUTH_URL)

    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert response.json()["warning"] == UNUSABLE_WARNING


def test_get_decrypted_token_returns_plaintext_only_through_the_service(
    settings: Settings, clean_credentials: None, fake_github: FakeGitHub, db_engine: Engine
) -> None:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        assert get_decrypted_token(session, settings) is None
        connect_token(session, token=FAKE_TOKEN, settings=settings, client=fake_github.client())
    with factory() as session:
        assert get_decrypted_token(session, settings) == FAKE_TOKEN
        status = get_status(session, settings)

    assert status.authenticated is True
    assert FAKE_TOKEN not in repr(status)


def test_concurrent_puts_leave_a_single_row(
    settings: Settings, clean_credentials: None, fake_github: FakeGitHub, db_engine: Engine
) -> None:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    workers = 8
    tokens = [f"github_pat_TESTONLY_concurrent_{i}" for i in range(workers)]
    barrier = threading.Barrier(workers)

    def attempt(token: str) -> None:
        barrier.wait()
        with factory() as session:
            connect_token(session, token=token, settings=settings, client=fake_github.client())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(attempt, tokens))  # 任何例外都會在此重新拋出

    rows = _rows(db_engine)
    assert len(rows) == 1
    with factory() as session:
        assert get_decrypted_token(session, settings) in tokens


def test_database_allows_only_one_row(db_engine: Engine, clean_credentials: None) -> None:
    insert = text(
        "INSERT INTO github_credentials (id, encrypted_token, login, token_type) "
        "VALUES (:id, 'x', 'octocat', :token_type)"
    )
    with Session(db_engine) as session:
        session.execute(insert, {"id": 1, "token_type": "unknown"})
        session.commit()
        with pytest.raises(IntegrityError) as duplicate:
            session.execute(insert, {"id": 1, "token_type": "unknown"})
        session.rollback()
        with pytest.raises(IntegrityError) as second_id:
            session.execute(insert, {"id": 2, "token_type": "unknown"})
        session.rollback()
        with pytest.raises(IntegrityError) as bad_type:
            session.execute(text("UPDATE github_credentials SET token_type = 'oauth'"))
        session.rollback()

    assert "pk_github_credentials" in str(duplicate.value.orig)
    assert "ck_github_credentials_single_row" in str(second_id.value.orig)
    assert "ck_github_credentials_type_valid" in str(bad_type.value.orig)


def test_full_flow_logs_never_contain_token_ciphertext_or_upstream_body(
    client: TestClient, fake_github: FakeGitHub, db_engine: Engine
) -> None:
    with capture_logs() as logs:
        client.put(PUT_URL, json={"token": FAKE_TOKEN})
        client.get(AUTH_URL)
        ciphertext = _rows(db_engine)[0][1]
        fake_github.status = 401
        client.put(PUT_URL, json={"token": FAKE_CLASSIC_TOKEN})
        client.delete(AUTH_URL)

    text_ = logs.text()
    assert len(logs) > 0
    for secret in (FAKE_TOKEN, FAKE_CLASSIC_TOKEN, ciphertext, GITHUB_BODY_MARKER):
        assert secret not in text_
