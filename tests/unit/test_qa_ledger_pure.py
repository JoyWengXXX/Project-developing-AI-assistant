"""問答額度帳本的純邏輯：費用換算、每日視窗、時區設定與錯誤格式（不需資料庫）。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from repopilot.api.errors import register_exception_handlers
from repopilot.config import Settings
from repopilot.errors import QaReservationFinalizedError, TokenBudgetExceededError
from repopilot.qa.usage_ledger import ModelPrice, day_window, token_cost_usd

PRICE = ModelPrice(input_usd_per_million=Decimal("0.80"), output_usd_per_million=Decimal("4.00"))
TAIPEI = ZoneInfo("Asia/Taipei")


def test_cost_converts_input_and_output_with_separate_prices() -> None:
    assert token_cost_usd(1_000, 500, PRICE) == Decimal("0.0028")
    assert token_cost_usd(1_000_000, 0, PRICE) == Decimal("0.80")
    assert token_cost_usd(0, 1_000_000, PRICE) == Decimal("4.00")


def test_cost_of_zero_tokens_is_zero() -> None:
    assert token_cost_usd(0, 0, PRICE) == 0


def test_cost_is_rounded_half_up_to_eight_decimals() -> None:
    price = ModelPrice(Decimal("0.005"), Decimal("0"))

    assert token_cost_usd(1, 0, price) == Decimal("0.00000001")
    assert token_cost_usd(1, 0, ModelPrice(Decimal("0.004"), Decimal("0"))) == 0


def test_cost_uses_decimal_without_float_error() -> None:
    price = ModelPrice(Decimal("0.1"), Decimal("0.2"))

    assert token_cost_usd(3_000_000, 3_000_000, price) == Decimal("0.9")


@pytest.mark.parametrize("tokens", [(-1, 0), (0, -1)])
def test_cost_rejects_negative_token_counts(tokens: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="negative"):
        token_cost_usd(*tokens, PRICE)


def test_day_window_starts_at_local_midnight_in_the_configured_zone() -> None:
    now = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)  # 台北 11:00

    start, end = day_window(now, TAIPEI)

    assert start == datetime(2026, 11, 18, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 19, 16, 0, tzinfo=UTC)


def test_day_window_rolls_over_exactly_at_local_midnight() -> None:
    before = datetime(2026, 11, 19, 15, 59, 59, tzinfo=UTC)
    at = datetime(2026, 11, 19, 16, 0, 0, tzinfo=UTC)

    assert day_window(before, TAIPEI)[1] == at
    assert day_window(at, TAIPEI)[0] == at


def test_day_window_in_utc_matches_the_utc_date() -> None:
    now = datetime(2026, 11, 19, 23, 59, tzinfo=UTC)

    start, end = day_window(now, ZoneInfo("UTC"))

    assert (start, end) == (
        datetime(2026, 11, 19, tzinfo=UTC),
        datetime(2026, 11, 20, tzinfo=UTC),
    )


def test_day_window_follows_daylight_saving_changes() -> None:
    new_york = ZoneInfo("America/New_York")

    spring_start, spring_end = day_window(datetime(2026, 3, 8, 12, tzinfo=UTC), new_york)
    autumn_start, autumn_end = day_window(datetime(2026, 11, 1, 12, tzinfo=UTC), new_york)

    assert spring_end - spring_start == timedelta(hours=23)
    assert autumn_end - autumn_start == timedelta(hours=25)


def test_day_window_accepts_a_now_in_another_offset() -> None:
    now = datetime(2026, 11, 19, 11, 0, tzinfo=TAIPEI)

    assert day_window(now, TAIPEI)[0] == datetime(2026, 11, 18, 16, 0, tzinfo=UTC)


def test_day_window_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        day_window(datetime(2026, 11, 19, 3, 0), TAIPEI)


def test_usage_timezone_defaults_to_taipei() -> None:
    assert Settings(_env_file=None).qa_usage_timezone == "Asia/Taipei"


def test_usage_timezone_loads_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_USAGE_TIMEZONE", "UTC")

    assert Settings(_env_file=None).qa_usage_timezone == "UTC"


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "../etc/passwd", "Asia/Taipei\x00"])
def test_invalid_usage_timezone_is_rejected_at_settings_load(name: str) -> None:
    with pytest.raises(ValidationError, match="QA_USAGE_TIMEZONE"):
        Settings(_env_file=None, qa_usage_timezone=name)


def test_token_budget_error_is_a_409_with_machine_readable_details() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/over-daily")
    def over_daily() -> None:
        raise TokenBudgetExceededError("daily_tokens", limit=500, requested=100, used=450)

    @app.get("/over-cost")
    def over_cost() -> None:
        raise TokenBudgetExceededError("daily_cost_usd", limit="1.5", requested="0.2")

    @app.get("/finalized")
    def finalized() -> None:
        raise QaReservationFinalizedError("done")

    client = TestClient(app)

    daily = client.get("/over-daily")
    cost = client.get("/over-cost")

    assert daily.status_code == 409
    assert daily.json()["error"]["code"] == "token_budget_exceeded"
    assert daily.json()["error"]["details"] == {
        "reason": "daily_tokens",
        "limit": 500,
        "requested": 100,
        "used": 450,
    }
    assert cost.json()["error"]["details"] == {
        "reason": "daily_cost_usd",
        "limit": "1.5",
        "requested": "0.2",
    }
    assert client.get("/finalized").status_code == 409
