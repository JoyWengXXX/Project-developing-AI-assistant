"""問答可選模型、預設單價、每題預估與設定輸入驗證（不需資料庫）。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from repopilot.api.schemas import QaProviderKeySubmit, QaSettingsUpdate
from repopilot.qa.pricing import (
    DEFAULT_PRICE_TABLE_AS_OF,
    DEFAULT_PRICES,
    ESTIMATE_INPUT_TOKENS,
    ESTIMATE_OUTPUT_TOKENS,
    MODEL_TIERS,
    estimated_cost_per_question_usd,
)
from repopilot.qa.provider import SUPPORTED_MODELS
from repopilot.qa.usage_ledger import ModelPrice


def test_known_models_match_the_provider_supported_models_and_cover_every_tier() -> None:
    assert set(MODEL_TIERS) == set(SUPPORTED_MODELS) == set(DEFAULT_PRICES)
    assert sorted(MODEL_TIERS.values()) == ["high", "low", "mid"]


def test_default_prices_are_the_spec_values() -> None:
    assert DEFAULT_PRICES["claude-haiku-5-5"] == ModelPrice(Decimal("0.10"), Decimal("0.50"))
    assert DEFAULT_PRICES["claude-sonnet-5-5"] == ModelPrice(Decimal("2"), Decimal("10"))
    assert DEFAULT_PRICES["claude-opus-5-5"] == ModelPrice(Decimal("4"), Decimal("20"))
    assert DEFAULT_PRICE_TABLE_AS_OF == "2026-11-18"


def test_estimate_uses_the_documented_assumption_and_the_given_price() -> None:
    assert (ESTIMATE_INPUT_TOKENS, ESTIMATE_OUTPUT_TOKENS) == (40_000, 1_500)

    assert estimated_cost_per_question_usd(DEFAULT_PRICES["claude-sonnet-5-5"]) == Decimal("0.095")
    assert estimated_cost_per_question_usd(ModelPrice(Decimal("1"), Decimal("5.5"))) == Decimal(
        "0.04825"
    )


def test_settings_update_tracks_which_fields_were_sent() -> None:
    update = QaSettingsUpdate.model_validate({"model": None, "daily_tokens": 5})

    assert update.model_dump(exclude_unset=True) == {"model": None, "daily_tokens": 5}
    assert QaSettingsUpdate.model_validate({}).model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize(
    "body",
    [
        {"model": "unknown-model"},
        {"per_request_tokens": 0},
        {"daily_tokens": 2**63},
        {"daily_cost_usd": "0"},
        {"price_table": None},
        {"price_table": {}},
        {"price_table_as_of": "2026-12-01"},
        {"surprise": 1},
    ],
)
def test_settings_update_rejects_invalid_input(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QaSettingsUpdate.model_validate(body)


def test_provider_key_submit_trims_and_never_shows_the_key_in_repr() -> None:
    submit = QaProviderKeySubmit.model_validate({"api_key": "  sk-ant-secret-value \n"})

    assert submit.api_key.get_secret_value() == "sk-ant-secret-value"
    assert "secret-value" not in repr(submit)
    assert "secret-value" not in str(submit.model_dump())


@pytest.mark.parametrize("key", ["", "   ", "has space", "tab\tinside", "ctrl\x00char"])
def test_provider_key_submit_rejects_blank_or_whitespace_keys(key: str) -> None:
    with pytest.raises(ValidationError):
        QaProviderKeySubmit.model_validate({"api_key": key})
