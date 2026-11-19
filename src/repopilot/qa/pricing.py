"""問答可選模型、預設單價表與每題預估費用（第四階段 4-1c）。

模型清單與預設單價取自規格第 10 節（2026-11-18 讀自 Anthropic 官方價格頁）；價格常變，
使用者可在設定中更新，更新後的單價才是之後預估與換算使用的值。
"""

from decimal import Decimal
from typing import Final, Literal

from repopilot.qa.usage_ledger import ModelPrice, token_cost_usd

PROVIDER: Final = "anthropic"

Tier = Literal["low", "mid", "high"]

# 低、中、高階皆可選，沒有預設模型（規格 D11）；與 `qa.provider.SUPPORTED_MODELS` 同一份清單
MODEL_TIERS: Final[dict[str, Tier]] = {
    "claude-haiku-5-5": "low",
    "claude-sonnet-5-5": "mid",
    "claude-opus-5-5": "high",
}

DEFAULT_PRICES: Final[dict[str, ModelPrice]] = {
    "claude-haiku-5-5": ModelPrice(Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": ModelPrice(Decimal("2"), Decimal("10")),
    "claude-opus-5-5": ModelPrice(Decimal("4"), Decimal("20")),
}
DEFAULT_PRICE_TABLE_AS_OF: Final = "2026-11-18"

# 只提示、不參與計算：Haiku 的提示超過 100K token 時單價跳為 0.50／2.50
PRICE_NOTES: Final[dict[str, str]] = {
    "claude-haiku-5-5": (
        "Prompts over 100K input tokens are priced at 0.50 / 2.50 USD per million tokens; "
        "the per-question estimate uses the listed price."
    )
}

# 每題預估的假設值（不是實測）：約 40K 輸入片段加 1.5K 輸出，以標價計算，不含快取與長上下文加價
ESTIMATE_INPUT_TOKENS: Final = 40_000
ESTIMATE_OUTPUT_TOKENS: Final = 1_500


def estimated_cost_per_question_usd(price: ModelPrice) -> Decimal:
    return token_cost_usd(ESTIMATE_INPUT_TOKENS, ESTIMATE_OUTPUT_TOKENS, price)
