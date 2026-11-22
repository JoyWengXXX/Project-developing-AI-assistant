"""新專案步驟的單次模型呼叫（第五階段）：送出前檢查 → 預留額度 → 呼叫 → 結算。

同步、不重試（呼叫端決定失敗時怎麼降級）。沿用第四階段的設定與額度帳本：供應商 key、選定的模型、
逐 repo 資料外送同意、每日與單次額度上限。任何檢查失敗都在呼叫供應商之前拋出，沒有外部呼叫也沒有
帳本事件。測試以 `build_provider` 換成假供應商。

.NET 對照：像一個 scoped service 先做授權與配額檢查（類似 policy handler），再呼叫外部 API，
最後在 finally 區塊補記實際用量。
"""

import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.errors import (
    QaEgressNotConsentedError,
    QaModelNotSelectedError,
    QaProviderNotConfiguredError,
)
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.qa import usage_ledger
from repopilot.qa.anthropic_provider import AnthropicProvider
from repopilot.qa.context import estimate_tokens
from repopilot.qa.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderRequestTooLargeError,
    ProviderSpendLimitReachedError,
    QAProviderError,
    UnsupportedModelError,
)
from repopilot.qa.failures import ProviderOutcomeError
from repopilot.qa.provider import (
    ChatMessage,
    GenerateRequest,
    GenerationOutcome,
    QAProvider,
)
from repopilot.qa.usage_ledger import ModelPrice
from repopilot.services import qa_consent
from repopilot.services import qa_settings as settings_service

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.project_ai")

# 供應商明確拒絕、沒有處理請求者：預留全數釋放（其餘失敗一律保守結算），同 `qa_question_runner`
_NOT_CHARGED = (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderRateLimitedError,
    ProviderPaymentRequiredError,
    ProviderRequestTooLargeError,
    ProviderSpendLimitReachedError,
    UnsupportedModelError,
)


def build_provider(api_key: str) -> QAProvider:
    """供應商客戶端的唯一建立點；測試以假供應商取代它（不連網）。"""
    return AnthropicProvider(api_key)


@dataclass(frozen=True)
class AiResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    usage_known: bool
    cost_usd: Decimal


def generate_once(
    session: Session,
    settings: Settings,
    *,
    repository_ids: Collection[int],
    system: str,
    user_message: str,
    json_schema: dict[str, object],
    max_tokens: int,
    timeout_seconds: float = 60.0,
    now: datetime | None = None,
) -> AiResult:
    """一次結構化生成。`repository_ids` 是這次會送出內容的 repo（逐一檢查同意）；沒有則傳空集合。"""
    now = now or datetime.now(UTC)
    view = settings_service.get_view(session, settings)
    if not view.provider_key_configured:
        raise QaProviderNotConfiguredError(
            view.provider_key_warning or "The provider API key is not configured."
        )
    price = None if view.model is None else view.prices.get(view.model)
    if view.model is None or price is None:
        raise QaModelNotSelectedError("No model is selected; choose one in the QA settings.")
    missing = qa_consent.missing_consents(session, repository_ids)
    if missing:
        raise QaEgressNotConsentedError(missing)
    api_key = settings_service.decrypt_provider_key(session, settings)
    if api_key is None:
        raise QaProviderNotConfiguredError("The provider API key is not configured.")

    request = GenerateRequest(
        model=view.model,
        system=system,
        messages=(ChatMessage("user", user_message),),
        max_tokens=max_tokens,
        effort="low",
        timeout_seconds=timeout_seconds,
        json_schema=json_schema,
        disable_thinking=True,
    )
    estimated_input = int(
        (
            estimate_tokens(system)
            + estimate_tokens(json.dumps(json_schema))
            + estimate_tokens(user_message)
        )
        * settings.qa_token_estimate_safety_factor
    )
    reservation_id = uuid4()
    usage_ledger.reserve(
        session,
        estimated_input_tokens=estimated_input,
        max_tokens=max_tokens,
        price=price,
        limits=view.limits,
        now=now,
        tz=ZoneInfo(settings.qa_usage_timezone),
        reservation_id=reservation_id,
    )
    return _call_and_settle(session, build_provider(api_key), request, reservation_id, price)


def _call_and_settle(
    session: Session,
    provider: QAProvider,
    request: GenerateRequest,
    reservation_id: UUID,
    price: ModelPrice,
) -> AiResult:
    try:
        response = provider.generate(request)
    except QAProviderError as error:
        if error.usage is not None:
            usage_ledger.settle(session, reservation_id, error.usage, price=price, now=_now())
        elif isinstance(error, _NOT_CHARGED):
            usage_ledger.release(session, reservation_id, None, price=price, now=_now())
        else:
            usage_ledger.settle(session, reservation_id, None, price=price, now=_now())
        raise
    except BaseException:
        # 不知道供應商有沒有處理：保守沿用預留量，避免低估用量
        usage_ledger.settle(session, reservation_id, None, price=price, now=_now())
        raise
    totals = usage_ledger.settle(session, reservation_id, response.usage, price=price, now=_now())
    if response.outcome is not GenerationOutcome.COMPLETED:
        raise ProviderOutcomeError(response.outcome)
    return AiResult(
        text=response.text,
        model=response.model,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        usage_known=response.usage.known,
        cost_usd=totals.cost_usd,
    )


def _now() -> datetime:
    return datetime.now(UTC)
