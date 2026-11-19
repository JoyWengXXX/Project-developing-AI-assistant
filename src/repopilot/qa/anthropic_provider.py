"""Anthropic 轉接層。

SDK 自動重試一律關閉（重試與額度重新核對由我們自己控制）。
日誌與例外只含模型、狀態碼、request id 與 token 數，不含 API key、問題或片段內容。
"""

import json
import logging
import math
from typing import Any

import anthropic

from repopilot.qa.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderMalformedResponseError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderRequestTooLargeError,
    ProviderSpendLimitReachedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QAProviderError,
)
from repopilot.qa.provider import (
    GenerateRequest,
    GenerateResponse,
    GenerationOutcome,
    Usage,
    get_model_info,
    validate_json_output,
)

logger = logging.getLogger(__name__)

_THINKING_DISABLE_MAX_EFFORT = {"low", "medium", "high"}
_SPEND_LIMIT_SIGNAL = "enforced_spend_limit_reached"
_LOW_CREDIT_SIGNAL = "credit balance is too low"

_STOP_REASON_OUTCOMES = {
    "end_turn": GenerationOutcome.COMPLETED,
    "max_tokens": GenerationOutcome.MAX_TOKENS,
    "refusal": GenerationOutcome.REFUSAL,
    "model_context_window_exceeded": GenerationOutcome.CONTEXT_WINDOW_EXCEEDED,
}


def build_message_params(request: GenerateRequest) -> dict[str, Any]:
    """組 `messages.create` 的參數；不送模型不支援而會被拒絕的設定。"""
    info = get_model_info(request.model)
    output_config: dict[str, Any] = {"effort": request.effort}
    if request.json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": request.system,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "output_config": output_config,
    }
    if (
        request.disable_thinking
        and info.can_disable_thinking
        and request.effort in _THINKING_DISABLE_MAX_EFFORT
    ):
        params["thinking"] = {"type": "disabled"}
    return params


def parse_usage(raw: Any) -> Usage:
    if raw is None:
        return Usage.unknown()
    input_total = (
        raw.input_tokens
        + (getattr(raw, "cache_creation_input_tokens", None) or 0)
        + (getattr(raw, "cache_read_input_tokens", None) or 0)
    )
    details = getattr(raw, "output_tokens_details", None)
    thinking = getattr(details, "thinking_tokens", None) if details is not None else None
    return Usage(
        input_tokens=input_total,
        output_tokens=raw.output_tokens,
        thinking_tokens=thinking,
    )


def _retry_after_seconds(exc: anthropic.APIStatusError) -> float | None:
    """`retry-after` 標頭的秒數；缺少、非數字（HTTP 日期）或為負則 None。"""
    raw = exc.response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _is_spend_limit(exc: anthropic.APIStatusError) -> bool:
    """組織層花費上限的訊號（400／429）；只比對固定識別字串，內容不會被保存或記錄。

    回應本文的確切形狀未實測（未驗證）：同時檢查解析後的本文與例外訊息。
    """
    if exc.status_code not in (400, 429):
        return False
    return _SPEND_LIMIT_SIGNAL in f"{exc.message} {json.dumps(exc.body, default=str)}"


def _is_low_credit_balance(exc: anthropic.APIStatusError) -> bool:
    """餘額不足：實測 Anthropic 以 400 `invalid_request_error` 回報，而不是 402。"""
    if exc.status_code != 400:
        return False
    return _LOW_CREDIT_SIGNAL in f"{exc.message} {json.dumps(exc.body, default=str)}".lower()


def classify_error(exc: Exception) -> QAProviderError:
    """把 SDK 例外轉成領域例外；402、413、529 沒有專屬 SDK 類別，依 `status_code` 分派。"""
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError("provider request timed out")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError("provider connection failed")
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        request_id = exc.request_id
        retry_after = _retry_after_seconds(exc)
        if status == 408:
            return ProviderTimeoutError(
                f"provider returned {status}",
                status_code=status,
                request_id=request_id,
                retry_after_seconds=retry_after,
            )
        if _is_spend_limit(exc):
            return ProviderSpendLimitReachedError(
                f"provider returned {status}", status_code=status, request_id=request_id
            )
        if status in (401, 403):
            error_type: type[QAProviderError] = ProviderAuthError
        elif status == 402 or _is_low_credit_balance(exc):
            error_type = ProviderPaymentRequiredError
        elif status == 413:
            error_type = ProviderRequestTooLargeError
        elif status == 429:
            error_type = ProviderRateLimitedError
        elif status >= 500:
            error_type = ProviderUnavailableError
        else:
            error_type = ProviderBadRequestError
        return error_type(
            f"provider returned {status}",
            status_code=status,
            request_id=request_id,
            retry_after_seconds=retry_after,
        )
    return QAProviderError("provider call failed")


class AnthropicProvider:
    def __init__(self, api_key: str, *, client: anthropic.Anthropic | None = None) -> None:
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=0)

    def count_tokens(self, request: GenerateRequest) -> int:
        # 官方文件未寫明 count_tokens 是否接受 output_config（未驗證），所以不送；
        # 結構化輸出的 schema 不會計入，呼叫端預留額度時須自留餘裕。
        params = build_message_params(request)
        params.pop("max_tokens")
        params.pop("output_config")
        try:
            result = self._client.messages.count_tokens(**params, timeout=request.timeout_seconds)
        except Exception as exc:
            raise self._translate(exc) from None
        return int(result.input_tokens)

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        params = build_message_params(request)
        try:
            message = self._client.messages.create(**params, timeout=request.timeout_seconds)
        except Exception as exc:
            raise self._translate(exc) from None
        try:
            response = self._to_response(request, message)
        except ProviderMalformedResponseError as error:
            error.usage = parse_usage(getattr(message, "usage", None))  # 回應已計費，用量照實結算
            raise
        logger.info(
            "qa provider call: model=%s outcome=%s input_tokens=%d output_tokens=%d",
            response.model,
            response.outcome,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return response

    def _translate(self, exc: Exception) -> QAProviderError:
        error = classify_error(exc)
        logger.warning(
            "qa provider call failed: code=%s status=%s request_id=%s",
            error.code,
            error.status_code,
            error.request_id,
        )
        return error

    def _to_response(self, request: GenerateRequest, message: Any) -> GenerateResponse:
        outcome = _STOP_REASON_OUTCOMES.get(message.stop_reason)
        if outcome is None:
            raise ProviderMalformedResponseError("unexpected stop_reason")
        text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
        if outcome is GenerationOutcome.COMPLETED and request.json_schema is not None:
            validate_json_output(text)
        return GenerateResponse(
            model=message.model or request.model,
            outcome=outcome,
            text=text,
            usage=parse_usage(getattr(message, "usage", None)),
            request_id=getattr(message, "_request_id", None),
        )
