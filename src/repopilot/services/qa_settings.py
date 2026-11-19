"""問答設定（第四階段 4-1c）：模型、額度、單價表與加密保存的供應商 API key。

資料表 `qa_settings` 最多一列（`id = 1`）。沒有資料列＝全部是預設值（未選模型、不限額、預設單價）；
第一次寫入時才以 `INSERT ... ON CONFLICT DO NOTHING` 建立，之後以 `SELECT ... FOR UPDATE` 序列化
更新，所以併發 PUT 不會產生第二列或互相覆蓋。

API key 只寫不讀：沿用 `TOKEN_ENCRYPTION_KEY` 的 Fernet（`github_credentials.build_fernet`），
資料庫只存密文；金鑰未設定時在碰資料庫之前就拒絕。本模組不記錄、也不回傳 key 的任何內容。

.NET 對照：像 `IOptions` 的單例設定列，加上 Data Protection API 加密的機密欄位。
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from decimal import Decimal
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import QaSettings
from repopilot.errors import TokenEncryptionNotConfiguredError
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.qa.pricing import (
    DEFAULT_PRICE_TABLE_AS_OF,
    DEFAULT_PRICES,
    MODEL_TIERS,
    PROVIDER,
)
from repopilot.qa.usage_ledger import ModelPrice, UsageLimits
from repopilot.services.github_credentials import build_fernet

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.qa_settings")

_SINGLE_ROW_ID = 1

UNUSABLE_KEY_WARNING = (
    "The stored API key cannot be decrypted (TOKEN_ENCRYPTION_KEY is missing or has changed). "
    "Submit the API key again."
)

# 更新請求的欄位（API 欄位名）→ 資料表欄位
_LIMIT_COLUMNS = {
    "per_request_tokens": "per_request_token_limit",
    "daily_tokens": "daily_token_limit",
    "daily_cost_usd": "daily_cost_limit_usd",
}


@dataclass(frozen=True)
class SettingsView:
    provider: str
    model: str | None
    limits: UsageLimits
    prices: dict[str, ModelPrice]
    price_table_as_of: date
    provider_key_configured: bool
    provider_key_warning: str | None
    updated_at: datetime | None

    @property
    def can_ask(self) -> bool:
        return self.model is not None and self.provider_key_configured


def _default_prices_json() -> dict[str, Any]:
    return {model: _price_json(price) for model, price in DEFAULT_PRICES.items()}


def _price_json(price: ModelPrice) -> dict[str, str]:
    # 以字串保存 Decimal；固定點記法，避免 `1E+1` 這類指數表示
    return {
        "input_usd_per_million": format(price.input_usd_per_million, "f"),
        "output_usd_per_million": format(price.output_usd_per_million, "f"),
    }


def _prices_of(row: QaSettings | None) -> dict[str, ModelPrice]:
    prices = dict(DEFAULT_PRICES)
    if row is not None:
        for model in MODEL_TIERS:
            stored = row.price_table.get(model)
            if stored is not None:
                prices[model] = ModelPrice(
                    Decimal(stored["input_usd_per_million"]),
                    Decimal(stored["output_usd_per_million"]),
                )
    return prices


def _limits_of(row: QaSettings | None) -> UsageLimits:
    if row is None:
        return UsageLimits()
    return UsageLimits(
        per_request_tokens=row.per_request_token_limit,
        daily_tokens=row.daily_token_limit,
        daily_cost_usd=row.daily_cost_limit_usd,
    )


def _key_status(row: QaSettings | None, settings: Settings) -> tuple[bool, str | None]:
    if row is None or row.encrypted_provider_key is None:
        return False, None
    try:
        build_fernet(settings).decrypt(row.encrypted_provider_key.encode("ascii"))
    except (TokenEncryptionNotConfiguredError, InvalidToken, ValueError):
        logger.warning("qa_provider_key_unusable")
        return False, UNUSABLE_KEY_WARNING
    return True, None


def get_limits(session: Session) -> UsageLimits:
    """目前的額度上限（沒有設定列時全部不限制）。"""
    return _limits_of(session.get(QaSettings, _SINGLE_ROW_ID))


def decrypt_provider_key(session: Session, settings: Settings) -> str | None:
    """解密保存的 API key，只給組供應商客戶端用；未設定或無法解密時回 None（不記錄內容）。"""
    row = session.get(QaSettings, _SINGLE_ROW_ID)
    if row is None or row.encrypted_provider_key is None:
        return None
    try:
        return (
            build_fernet(settings)
            .decrypt(row.encrypted_provider_key.encode("ascii"))
            .decode("utf-8")
        )
    except (TokenEncryptionNotConfiguredError, InvalidToken, ValueError):
        logger.warning("qa_provider_key_unusable")
        return None


def get_prices(session: Session) -> dict[str, ModelPrice]:
    """目前的單價表（使用者更新後的值，沒有設定列時為預設值）。"""
    return _prices_of(session.get(QaSettings, _SINGLE_ROW_ID))


def get_view(session: Session, settings: Settings) -> SettingsView:
    row = session.get(QaSettings, _SINGLE_ROW_ID)
    configured, warning = _key_status(row, settings)
    return SettingsView(
        provider=PROVIDER,
        model=None if row is None else row.model,
        limits=_limits_of(row),
        prices=_prices_of(row),
        price_table_as_of=(
            date.fromisoformat(DEFAULT_PRICE_TABLE_AS_OF) if row is None else row.price_table_as_of
        ),
        provider_key_configured=configured,
        provider_key_warning=warning,
        updated_at=None if row is None else row.updated_at,
    )


def _locked_row(session: Session, now: datetime) -> QaSettings:
    session.execute(
        insert(QaSettings)
        .values(
            id=_SINGLE_ROW_ID,
            provider=PROVIDER,
            price_table=_default_prices_json(),
            price_table_as_of=date.fromisoformat(DEFAULT_PRICE_TABLE_AS_OF),
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[QaSettings.id])
    )
    return session.execute(
        select(QaSettings).where(QaSettings.id == _SINGLE_ROW_ID).with_for_update()
    ).scalar_one()


def update_settings(
    session: Session, changes: Mapping[str, Any], *, now: datetime, tz: tzinfo
) -> None:
    """套用部分更新；`changes` 只含請求中明確送出的欄位（見 `QaSettingsUpdate`）。

    送出 `None` 的 `model`／額度欄位會清除該值；`price_table` 只覆蓋有列出的模型。
    """
    try:
        row = _locked_row(session, now)
        if "model" in changes:
            row.model = changes["model"]
        for field, column in _LIMIT_COLUMNS.items():
            if field in changes:
                setattr(row, column, changes[field])
        if "price_table" in changes:
            merged = dict(row.price_table)  # 指派新物件，SQLAlchemy 才會偵測到 JSONB 變更
            for model, price in changes["price_table"].items():
                merged[model] = _price_json(
                    ModelPrice(price["input_usd_per_million"], price["output_usd_per_million"])
                )
            row.price_table = merged
            row.price_table_as_of = changes.get("price_table_as_of") or now.astimezone(tz).date()
        row.updated_at = now
        session.commit()
    except BaseException:
        session.rollback()
        raise


def save_provider_key(session: Session, api_key: str, *, settings: Settings, now: datetime) -> None:
    """加密並保存（覆蓋舊的）；`TOKEN_ENCRYPTION_KEY` 不可用時拋錯，資料庫完全不動。"""
    fernet = build_fernet(settings)
    ciphertext = fernet.encrypt(api_key.encode("utf-8")).decode("ascii")
    try:
        row = _locked_row(session, now)
        row.encrypted_provider_key = ciphertext
        row.updated_at = now
        session.commit()
    except BaseException:
        session.rollback()
        raise
    logger.info("qa_provider_key_saved")


def clear_provider_key(session: Session, *, now: datetime) -> None:
    """清除保存的 API key；沒有也視為成功（冪等）。"""
    session.execute(
        update(QaSettings)
        .where(QaSettings.id == _SINGLE_ROW_ID)
        .values(encrypted_provider_key=None, updated_at=now)
    )
    session.commit()
    logger.info("qa_provider_key_cleared")
