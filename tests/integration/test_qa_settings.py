"""問答設定（4-1c）：模型與單價、額度、API key 加密保存、逐 repo 資料外送同意與 migration 0020。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from repopilot.config import Settings
from repopilot.main import create_app
from repopilot.qa import usage_ledger
from repopilot.qa.provider import Usage
from repopilot.qa.usage_ledger import ModelPrice, UsageLimits
from repopilot.services import qa_settings as settings_service
from tests.git_helpers import init_repo
from tests.github_helpers import capture_logs
from tests.integration.conftest import alembic_config

SETTINGS_URL = "/api/v1/qa/settings"
KEY_URL = "/api/v1/qa/provider-key"
USAGE_URL = "/api/v1/qa/usage"
CONSENTS_URL = "/api/v1/qa/consents"
REPOSITORIES_URL = "/api/v1/repositories"
HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"
FAKE_API_KEY = "sk-ant-FAKE-0123456789-abcdefghijklmnopqrstuvwxyz-ZYXWVUTSRQ"
TAIPEI = ZoneInfo("Asia/Taipei")
LEDGER_PRICE = ModelPrice(Decimal("1"), Decimal("5"))
NEW_TABLES = {"qa_settings", "repository_qa_consent"}


@pytest.fixture(autouse=True)
def clean_qa_state(db_engine: Engine) -> Iterator[None]:
    # downgrade 會拒絕有資料的設定與同意（其他 migration 測試會經過 0020）：前後都清空
    def truncate() -> None:
        with db_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE qa_settings, repository_qa_consent, qa_usage_ledger RESTART IDENTITY"
                )
            )

    truncate()
    yield
    truncate()


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
def keyless_client(settings: Settings, clean_repositories: None) -> Iterator[TestClient]:
    keyless = settings.model_copy(update={"token_encryption_key": SecretStr("")})
    with TestClient(create_app(keyless)) as test_client:
        yield test_client


def _get(client: TestClient) -> dict[str, Any]:
    response = client.get(SETTINGS_URL)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _put(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.put(SETTINGS_URL, json=body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _model(body: dict[str, Any], model_id: str) -> dict[str, Any]:
    option: dict[str, Any] = next(m for m in body["models"] if m["id"] == model_id)
    return option


def _row_counts(db_engine: Engine) -> tuple[int, int]:
    with db_engine.connect() as connection:
        return (
            int(connection.scalar(text("SELECT count(*) FROM qa_settings")) or 0),
            int(connection.scalar(text("SELECT count(*) FROM repository_qa_consent")) or 0),
        )


def _register(client: TestClient, roots: Path, name: str = "demo") -> int:
    response = client.post(REPOSITORIES_URL, json={"path": str(init_repo(roots / name))})
    assert response.status_code in (200, 201)
    return int(response.json()["id"])


def _consent_url(repository_id: int | str) -> str:
    return f"{REPOSITORIES_URL}/{repository_id}/qa-consent"


# --- AC-3：新環境沒有預設模型，三個模型都列出單價與每題預估，選定前不能提問 ---


def test_qa_settings_fresh_environment_has_no_model_and_cannot_ask(
    client: TestClient, db_engine: Engine
) -> None:
    body = _get(client)

    assert body["provider"] == "anthropic"
    assert body["model"] is None
    assert body["can_ask"] is False
    assert body["provider_key_configured"] is False
    assert body["provider_key_warning"] is None
    assert body["limits"] == {
        "per_request_tokens": None,
        "daily_tokens": None,
        "daily_cost_usd": None,
    }
    assert body["price_table_as_of"] == "2026-11-18"
    assert body["estimate_basis"] == {"input_tokens": 40_000, "output_tokens": 1_500}
    assert body["updated_at"] is None
    assert body["usage"]["remaining_tokens"] is None
    assert [(m["id"], m["tier"]) for m in body["models"]] == [
        (HAIKU, "low"),
        (SONNET, "mid"),
        (OPUS, "high"),
    ]
    expected = {
        HAIKU: ("0.10", "0.50", "0.00475"),
        SONNET: ("2", "10", "0.095"),
        OPUS: ("4", "20", "0.19"),
    }
    for model_id, (price_in, price_out, estimate) in expected.items():
        option = _model(body, model_id)
        assert Decimal(option["price"]["input_usd_per_million"]) == Decimal(price_in)
        assert Decimal(option["price"]["output_usd_per_million"]) == Decimal(price_out)
        assert Decimal(option["estimated_cost_per_question_usd"]) == Decimal(estimate)
    assert "100K" in _model(body, HAIKU)["price_note"]
    assert _model(body, SONNET)["price_note"] is None
    assert _row_counts(db_engine) == (0, 0)  # 讀取不會建立資料列


def test_qa_settings_can_ask_needs_both_a_model_and_a_usable_key(client: TestClient) -> None:
    assert _put(client, model=SONNET)["can_ask"] is False

    key_saved = client.put(KEY_URL, json={"api_key": FAKE_API_KEY})
    assert key_saved.status_code == 200
    assert key_saved.json()["can_ask"] is True
    assert _put(client, model=None)["can_ask"] is False
    assert _put(client, model=OPUS)["can_ask"] is True
    assert _get(client)["can_ask"] is True

    assert client.delete(KEY_URL).status_code == 204
    after_clear = _get(client)
    assert after_clear["provider_key_configured"] is False
    assert after_clear["can_ask"] is False
    assert after_clear["model"] == OPUS


# --- PUT /qa/settings：輸入驗證與部分更新 ---


def test_qa_settings_rejects_unknown_model_and_bad_numbers_without_saving(
    client: TestClient, db_engine: Engine
) -> None:
    sonnet_ok = {"input_usd_per_million": "1", "output_usd_per_million": "2"}
    bad_bodies: list[dict[str, Any]] = [
        {"model": "gpt-6.2-sol"},
        {"model": ""},
        {"model": 5},
        {"per_request_tokens": 0},
        {"per_request_tokens": -1},
        {"per_request_tokens": 2**31},
        {"per_request_tokens": 1.5},
        {"per_request_tokens": "100"},
        {"daily_tokens": 0},
        {"daily_tokens": True},
        {"daily_cost_usd": "0"},
        {"daily_cost_usd": "-0.5"},
        {"daily_cost_usd": "abc"},
        {"daily_cost_usd": "NaN"},
        {"daily_cost_usd": "1.000000001"},
        {"price_table": {}},
        {"price_table": None},
        {"price_table": {"gpt-6.2-sol": sonnet_ok}},
        {"price_table": {SONNET: {**sonnet_ok, "input_usd_per_million": "-1"}}},
        {"price_table": {SONNET: {"input_usd_per_million": "1"}}},
        {"price_table": {SONNET: {**sonnet_ok, "output_usd_per_million": "x"}}},
        {"price_table_as_of": "2026-12-01"},
        {"price_table": {SONNET: sonnet_ok}, "price_table_as_of": "not-a-date"},
        {"provider": "openai"},
    ]

    for bad in bad_bodies:
        response = client.put(SETTINGS_URL, json=bad)
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "validation_error", bad

    assert _row_counts(db_engine) == (0, 0)


def test_qa_settings_update_is_partial_and_null_clears_a_value(client: TestClient) -> None:
    first = _put(
        client, model=HAIKU, per_request_tokens=50_000, daily_tokens=1_000_000, daily_cost_usd="2.5"
    )
    assert first["limits"]["per_request_tokens"] == 50_000
    assert first["limits"]["daily_tokens"] == 1_000_000
    assert Decimal(first["limits"]["daily_cost_usd"]) == Decimal("2.5")
    assert first["updated_at"] is not None

    only_model = _put(client, model=SONNET)
    assert only_model["model"] == SONNET
    assert only_model["limits"] == first["limits"]
    assert _put(client)["model"] == SONNET  # 空 body 不改任何值

    cleared = _put(client, model=None, daily_tokens=None)
    assert cleared["model"] is None
    assert cleared["limits"]["daily_tokens"] is None
    assert cleared["limits"]["per_request_tokens"] == 50_000
    assert _get(client)["limits"] == cleared["limits"]


# --- AC-5：更新單價後，預估與換算使用新單價 ---


def test_qa_settings_updated_prices_drive_estimates_and_conversions(
    client: TestClient, db_engine: Engine
) -> None:
    body = _put(
        client,
        price_table={SONNET: {"input_usd_per_million": "1", "output_usd_per_million": "5.5"}},
        price_table_as_of="2026-12-01",
    )

    assert body["price_table_as_of"] == "2026-12-01"
    sonnet = _model(body, SONNET)
    assert Decimal(sonnet["price"]["input_usd_per_million"]) == Decimal("1")
    assert Decimal(sonnet["price"]["output_usd_per_million"]) == Decimal("5.5")
    # 40,000 × 1 / 1e6 + 1,500 × 5.5 / 1e6
    assert Decimal(sonnet["estimated_cost_per_question_usd"]) == Decimal("0.04825")
    # 沒列出的模型沿用舊單價
    assert Decimal(_model(body, HAIKU)["estimated_cost_per_question_usd"]) == Decimal("0.00475")
    assert Decimal(_model(body, OPUS)["estimated_cost_per_question_usd"]) == Decimal("0.19")
    persisted = _get(client)
    assert persisted["price_table_as_of"] == "2026-12-01"
    assert _model(persisted, SONNET) == sonnet

    with sessionmaker(bind=db_engine)() as session:
        prices = settings_service.get_prices(session)
    assert prices[SONNET] == ModelPrice(Decimal("1"), Decimal("5.5"))
    assert prices[OPUS] == ModelPrice(Decimal("4"), Decimal("20"))


def test_qa_settings_price_update_without_a_date_uses_today_in_the_configured_timezone(
    client: TestClient, settings: Settings
) -> None:
    body = _put(
        client, price_table={OPUS: {"input_usd_per_million": "3", "output_usd_per_million": "15"}}
    )

    today = datetime.now(ZoneInfo(settings.qa_usage_timezone)).date()
    assert body["price_table_as_of"] == today.isoformat()


# --- AC-1、AC-2：API key ---


def test_qa_settings_saving_a_key_without_encryption_key_returns_503_and_saves_nothing(
    keyless_client: TestClient, db_engine: Engine
) -> None:
    response = keyless_client.put(KEY_URL, json={"api_key": FAKE_API_KEY})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "token_encryption_not_configured"
    assert FAKE_API_KEY not in response.text
    assert _row_counts(db_engine) == (0, 0)
    assert keyless_client.get(SETTINGS_URL).json()["provider_key_configured"] is False


def test_qa_settings_saving_a_key_with_an_invalid_encryption_key_returns_503(
    settings: Settings, clean_repositories: None, db_engine: Engine
) -> None:
    invalid = settings.model_copy(update={"token_encryption_key": SecretStr("not-a-fernet-key")})

    with TestClient(create_app(invalid)) as invalid_client:
        response = invalid_client.put(KEY_URL, json={"api_key": FAKE_API_KEY})

    assert response.status_code == 503
    assert "not-a-fernet-key" not in response.text
    assert _row_counts(db_engine) == (0, 0)


def test_qa_settings_key_is_stored_as_ciphertext_and_never_returned_or_logged(
    client: TestClient, db_engine: Engine, encryption_key: str
) -> None:
    fragments = [FAKE_API_KEY, FAKE_API_KEY[:14], FAKE_API_KEY[-14:], FAKE_API_KEY[20:34]]

    # `repopilot` logger 不 propagate，caplog 收不到，所以用 capture_logs（DEBUG 層級）
    with capture_logs() as logs:
        responses = [
            client.put(KEY_URL, json={"api_key": FAKE_API_KEY}),
            client.get(SETTINGS_URL),
            client.put(SETTINGS_URL, json={"model": SONNET}),
            client.get(USAGE_URL),
            # 驗證失敗的回應與日誌也不能回顯 key
            client.put(KEY_URL, json={"api_key": FAKE_API_KEY + " extra words"}),
            client.put(KEY_URL, json={"api_key": FAKE_API_KEY, "extra": 1}),
        ]
        captured = logs.text()

    assert [r.status_code for r in responses] == [200, 200, 200, 200, 422, 422]
    assert responses[0].json()["provider_key_configured"] is True
    assert "api_key" not in responses[0].text
    for response in responses:
        for fragment in fragments:
            assert fragment not in response.text
    assert "qa_provider_key_saved" in captured  # 確實有擷取到相關日誌
    for fragment in fragments:
        assert fragment not in captured

    with db_engine.connect() as connection:
        stored = connection.scalar(text("SELECT encrypted_provider_key FROM qa_settings"))
        dumped = connection.scalar(text("SELECT row_to_json(s)::text FROM qa_settings s"))
    assert stored is not None
    for fragment in fragments:
        assert fragment not in stored
        assert fragment not in dumped
    assert Fernet(encryption_key.encode()).decrypt(stored.encode()).decode() == FAKE_API_KEY


def test_qa_settings_key_is_trimmed_and_blank_or_oversized_keys_are_rejected(
    client: TestClient, db_engine: Engine, encryption_key: str
) -> None:
    for bad in ({"api_key": "   "}, {"api_key": ""}, {"api_key": "x" * 513}, {}):
        assert client.put(KEY_URL, json=bad).status_code == 422
    assert _row_counts(db_engine) == (0, 0)

    assert client.put(KEY_URL, json={"api_key": f"  {FAKE_API_KEY}\n"}).status_code == 200

    with db_engine.connect() as connection:
        stored = connection.scalar(text("SELECT encrypted_provider_key FROM qa_settings"))
    assert Fernet(encryption_key.encode()).decrypt(str(stored).encode()).decode() == FAKE_API_KEY


def test_qa_settings_key_that_cannot_be_decrypted_is_reported_as_not_configured(
    client: TestClient, settings: Settings
) -> None:
    assert client.put(KEY_URL, json={"api_key": FAKE_API_KEY}).status_code == 200
    rotated = settings.model_copy(
        update={"token_encryption_key": SecretStr(Fernet.generate_key().decode())}
    )

    with TestClient(create_app(rotated)) as rotated_client:
        response = rotated_client.put(SETTINGS_URL, json={"model": HAIKU})

    body = response.json()
    assert body["provider_key_configured"] is False
    assert body["can_ask"] is False
    assert "Submit the API key again" in body["provider_key_warning"]
    assert FAKE_API_KEY not in response.text


def test_qa_settings_deleting_the_key_clears_the_ciphertext_and_is_idempotent(
    client: TestClient, db_engine: Engine
) -> None:
    assert client.delete(KEY_URL).status_code == 204  # 沒有設定列也成功
    assert _row_counts(db_engine) == (0, 0)

    client.put(KEY_URL, json={"api_key": FAKE_API_KEY})
    assert client.delete(KEY_URL).status_code == 204
    assert client.delete(KEY_URL).status_code == 204

    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT encrypted_provider_key FROM qa_settings")) is None
    assert _get(client)["provider_key_configured"] is False


# --- 額度上限接到 #2 的用量查詢 ---


def test_qa_settings_usage_remaining_reflects_the_stored_limits(
    client: TestClient, db_engine: Engine
) -> None:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as session:
        reservation = usage_ledger.reserve(
            session,
            estimated_input_tokens=600,
            max_tokens=400,
            price=LEDGER_PRICE,
            limits=UsageLimits(),
            now=now,
            tz=TAIPEI,
        )
    with factory() as session:
        usage_ledger.settle(
            session, reservation.reservation_id, Usage(300, 100), price=LEDGER_PRICE, now=now
        )

    unset = client.get(USAGE_URL).json()
    assert unset["remaining_tokens"] is None
    assert unset["remaining_cost_usd"] is None

    _put(client, daily_tokens=1_000, daily_cost_usd="1")
    usage = client.get(USAGE_URL).json()
    assert usage["today"]["total_tokens"] == 400
    assert usage["remaining_tokens"] == 600
    assert Decimal(usage["remaining_cost_usd"]) == Decimal("0.9992")
    assert _get(client)["usage"]["remaining_tokens"] == 600

    reset = client.post(f"{USAGE_URL}/reset").json()
    assert reset["remaining_tokens"] == 1_000
    assert Decimal(reset["remaining_cost_usd"]) == Decimal("1")

    _put(client, daily_tokens=300)
    assert client.get(USAGE_URL).json()["remaining_tokens"] == 300
    _put(client, daily_tokens=None, daily_cost_usd=None)
    cleared = client.get(USAGE_URL).json()
    assert cleared["remaining_tokens"] is None
    assert cleared["remaining_cost_usd"] is None


def test_qa_settings_decimal_amounts_are_serialized_in_fixed_point(client: TestClient) -> None:
    usage = client.post(f"{USAGE_URL}/reset").json()
    assert usage["today"]["cost_usd"] == "0.00000000"
    assert usage["cumulative"]["cost_usd"] == "0.00000000"
    _put(client, daily_cost_usd="2.5")
    assert _get(client)["usage"]["today"]["cost_usd"] == "0.00000000"


def test_qa_settings_limits_helper_returns_the_stored_limits(
    client: TestClient, db_engine: Engine
) -> None:
    factory = sessionmaker(bind=db_engine)
    with factory() as session:
        assert settings_service.get_limits(session) == UsageLimits()

    _put(client, per_request_tokens=900, daily_tokens=5_000, daily_cost_usd="1.5")

    with factory() as session:
        assert settings_service.get_limits(session) == UsageLimits(900, 5_000, Decimal("1.5"))


# --- AC-4：逐 repo 同意 ---


def test_qa_settings_consent_defaults_to_false_and_records_the_time_when_granted(
    client: TestClient, roots: Path
) -> None:
    repository_id = _register(client, roots)

    default = client.get(_consent_url(repository_id))
    assert default.status_code == 200
    assert default.json() == {
        "repository_id": repository_id,
        "consented": False,
        "consented_at": None,
    }
    assert client.get(CONSENTS_URL).json() == {"items": []}

    before = datetime.now(UTC)
    granted = client.put(_consent_url(repository_id))
    assert granted.status_code == 200
    body = granted.json()
    assert body["consented"] is True
    assert before <= datetime.fromisoformat(body["consented_at"]) <= datetime.now(UTC)
    assert client.get(_consent_url(repository_id)).json() == body
    assert client.get(CONSENTS_URL).json() == {
        "items": [{"repository_id": repository_id, "consented_at": body["consented_at"]}]
    }


def test_qa_settings_granting_twice_keeps_the_first_consent_time(
    client: TestClient, roots: Path
) -> None:
    repository_id = _register(client, roots)

    first = client.put(_consent_url(repository_id)).json()
    second = client.put(_consent_url(repository_id))

    assert second.status_code == 200
    assert second.json() == first
    assert len(client.get(CONSENTS_URL).json()["items"]) == 1


def test_qa_settings_revoking_returns_to_not_consented_and_is_idempotent(
    client: TestClient, roots: Path
) -> None:
    repository_id = _register(client, roots)
    other_id = _register(client, roots, "other")
    client.put(_consent_url(repository_id))
    client.put(_consent_url(other_id))

    revoked = client.delete(_consent_url(repository_id))
    again = client.delete(_consent_url(repository_id))

    expected = {"repository_id": repository_id, "consented": False, "consented_at": None}
    assert revoked.status_code == 200
    assert revoked.json() == expected
    assert again.json() == expected
    assert client.get(_consent_url(repository_id)).json() == expected
    remaining = client.get(CONSENTS_URL).json()["items"]
    assert [item["repository_id"] for item in remaining] == [other_id]


def test_qa_settings_consent_for_an_unknown_repository_is_404(
    client: TestClient, db_engine: Engine
) -> None:
    for method in (client.get, client.put, client.delete):
        response = method(_consent_url(999_999))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "repository_not_found"
    for bad_id in ("0", "-1", "abc", str(2**31)):
        assert client.put(_consent_url(bad_id)).status_code == 422

    assert _row_counts(db_engine) == (0, 0)


def test_qa_settings_consent_is_removed_with_the_repository(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    kept = _register(client, roots, "kept")
    removed = _register(client, roots, "removed")
    client.put(_consent_url(kept))
    client.put(_consent_url(removed))
    assert _row_counts(db_engine)[1] == 2

    response = client.delete(f"{REPOSITORIES_URL}/{removed}")

    assert response.status_code < 300
    assert [item["repository_id"] for item in client.get(CONSENTS_URL).json()["items"]] == [kept]
    assert _row_counts(db_engine)[1] == 1


# --- 資料庫約束 ---


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": 2},
        {"provider": "openai"},
        {"per_request": 0},
        {"daily_tokens": -5},
        {"daily_cost": 0},
    ],
)
def test_qa_settings_database_rejects_invalid_rows(
    db_engine: Engine, overrides: dict[str, Any]
) -> None:
    values: dict[str, Any] = {
        "id": 1,
        "provider": "anthropic",
        "per_request": None,
        "daily_tokens": None,
        "daily_cost": None,
        **overrides,
    }
    with pytest.raises(IntegrityError, match="ck_qa_settings_"), db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO qa_settings (id, provider, per_request_token_limit, "
                "daily_token_limit, daily_cost_limit_usd, price_table, price_table_as_of, "
                "updated_at) VALUES (:id, :provider, :per_request, :daily_tokens, :daily_cost, "
                "'{}', '2026-11-18', now())"
            ),
            values,
        )


def test_qa_settings_database_allows_only_one_row_and_no_default_model(db_engine: Engine) -> None:
    insert = text(
        "INSERT INTO qa_settings (id, price_table, price_table_as_of, updated_at) "
        "VALUES (1, '{}', '2026-11-18', now())"
    )
    with db_engine.begin() as connection:
        connection.execute(insert)
    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(insert)

    with db_engine.connect() as connection:
        row = connection.execute(text("SELECT provider, model FROM qa_settings")).one()
    assert tuple(row) == ("anthropic", None)


# --- migration 0020 ---


def _tables(db_engine: Engine) -> set[str]:
    with db_engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _current_version(db_engine: Engine) -> str | None:
    with db_engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
    return None if version is None else str(version)


def test_qa_settings_models_and_migrations_have_no_drift(
    db_engine: Engine, test_database_url: str
) -> None:
    assert _tables(db_engine) >= NEW_TABLES

    command.check(alembic_config(test_database_url))


def test_qa_settings_migration_downgrade_refuses_while_settings_exist(
    client: TestClient, db_engine: Engine, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    _put(client, model=SONNET)

    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        command.downgrade(config, "0019")

    assert _current_version(db_engine) == head
    assert _tables(db_engine) >= NEW_TABLES
    assert _row_counts(db_engine) == (1, 0)


def test_qa_settings_migration_downgrade_refuses_while_consents_exist(
    client: TestClient, roots: Path, db_engine: Engine, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    client.put(_consent_url(_register(client, roots)))

    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        command.downgrade(config, "0019")

    assert _current_version(db_engine) == head
    assert _row_counts(db_engine) == (0, 1)


def test_qa_settings_migration_downgrades_when_empty_and_upgrades_again(
    db_engine: Engine, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "0019")

        assert _current_version(db_engine) == "0019"
        assert _tables(db_engine).isdisjoint(NEW_TABLES)

        command.upgrade(config, "head")

        assert _tables(db_engine) >= NEW_TABLES
        assert _row_counts(db_engine) == (0, 0)
    finally:
        command.upgrade(config, "head")
