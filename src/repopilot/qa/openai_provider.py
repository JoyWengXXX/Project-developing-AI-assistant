"""OpenAI 轉接層（Responses API；第四階段 4-5，只供評估腳本對照，不接入設定與問答流程）。

SDK 自動重試一律關閉（重試由呼叫端自己控制）。`store=False`：不讓供應商保存請求與回應。
日誌與例外只含模型、狀態碼、request id 與 token 數，不含 API key、問題或片段內容。

核對來源（2026-11-19）：已安裝的 openai SDK 2.54.0 型別定義，以及官方文件 error-codes、
reasoning、structured-outputs、token-counting 指南的 Markdown 版。

已核對：`responses.create` 的參數（`instructions`、`input`、`max_output_tokens`、`reasoning`、
`store`、`text.format`）；`Response.status`／`incomplete_details.reason`（`max_output_tokens`、
`content_filter`）；輸出內容的 `output_text`／`refusal`；`usage.input_tokens`（已含快取讀取與
寫入）、`usage.output_tokens`（已含推理）、`output_tokens_details.reasoning_tokens`；
`responses.input_tokens.count`；例外類別與 `exc.code`（本文的 `error.code`）；429 的
`credit_balance_exhausted`、`organization_spend_limit_exceeded`、`project_spend_limit_exceeded`、
`organization_usage_limit_exceeded`，`error.type` 為 `insufficient_quota`。

未驗證（沒有真實呼叫）：各模型支援哪些 `reasoning.effort`；`disable_thinking` 在這裡不套用（不確定
`effort="none"` 每個模型都接受）；輸入過長是否回 400 `context_length_exceeded`；
`status="failed"` 的回應是否該重試（目前當成不可用的回應，不重試）；`count_tokens` 是否計入
結構化輸出 schema；413 是否真的會出現。
"""

import logging
import math
from typing import Any

import openai

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
    validate_json_output,
)

logger = logging.getLogger(__name__)

_SCHEMA_NAME = "repopilot_answer"
_PAYMENT_CODES = {"credit_balance_exhausted", "insufficient_quota"}
_SPEND_LIMIT_CODES = {
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}
_CONTEXT_LENGTH_CODE = "context_length_exceeded"  # 未驗證：官方文件沒有列出


def build_response_params(request: GenerateRequest) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": request.model,
        "instructions": request.system,
        "input": [{"role": m.role, "content": m.content} for m in request.messages],
        "max_output_tokens": request.max_tokens,
        "reasoning": {"effort": request.effort},
        "store": False,
    }
    if request.json_schema is not None:
        params["text"] = {
            "format": {
                "type": "json_schema",
                "name": _SCHEMA_NAME,
                "schema": request.json_schema,
                "strict": True,
            }
        }
    return params


def parse_usage(raw: Any) -> Usage:
    if raw is None:
        return Usage.unknown()
    details = getattr(raw, "output_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    return Usage(
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
        thinking_tokens=reasoning,
    )


def _retry_after_seconds(exc: openai.APIStatusError) -> float | None:
    raw = exc.response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def classify_error(exc: Exception) -> QAProviderError:
    """把 SDK 例外轉成與 Anthropic 轉接層相同的領域例外；402、413 沒有專屬 SDK 類別。"""
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeoutError("provider request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderUnavailableError("provider connection failed")
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        request_id = exc.request_id
        retry_after = _retry_after_seconds(exc)
        code = exc.code
        if status == 408:
            error_type: type[QAProviderError] = ProviderTimeoutError
        elif code in _SPEND_LIMIT_CODES:
            error_type = ProviderSpendLimitReachedError
        elif code in _PAYMENT_CODES or exc.type == "insufficient_quota" or status == 402:
            error_type = ProviderPaymentRequiredError
        elif status in (401, 403):
            error_type = ProviderAuthError
        elif status == 413 or (status == 400 and code == _CONTEXT_LENGTH_CODE):
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


class OpenAIProvider:
    def __init__(self, api_key: str, *, client: openai.OpenAI | None = None) -> None:
        # SDK 在 DEBUG 等級會記錄完整請求本文（含問題與片段）；壓到 WARNING 以免意外寫進日誌
        logging.getLogger("openai").setLevel(logging.WARNING)
        self._client = client or openai.OpenAI(api_key=api_key, max_retries=0)

    def count_tokens(self, request: GenerateRequest) -> int:
        params = build_response_params(request)
        params.pop("max_output_tokens")
        params.pop("store")
        try:
            result = self._client.responses.input_tokens.count(
                **params, timeout=request.timeout_seconds
            )
        except Exception as exc:
            raise self._translate(exc) from None
        return int(result.input_tokens)

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        params = build_response_params(request)
        try:
            raw = self._client.responses.create(**params, timeout=request.timeout_seconds)
        except Exception as exc:
            raise self._translate(exc) from None
        try:
            response = self._to_response(request, raw)
        except ProviderMalformedResponseError as error:
            error.usage = parse_usage(getattr(raw, "usage", None))  # 回應已計費，用量照實結算
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

    def _to_response(self, request: GenerateRequest, raw: Any) -> GenerateResponse:
        outcome = _outcome(raw)
        text = ""
        if outcome is GenerationOutcome.COMPLETED:
            text = "".join(
                part.text
                for item in raw.output
                if getattr(item, "type", None) == "message"
                for part in item.content
                if getattr(part, "type", None) == "output_text"
            )
            if request.json_schema is not None:
                validate_json_output(text)
        return GenerateResponse(
            model=str(raw.model) or request.model,
            outcome=outcome,
            text=text,
            usage=parse_usage(getattr(raw, "usage", None)),
            request_id=getattr(raw, "_request_id", None),
        )


def _outcome(raw: Any) -> GenerationOutcome:
    """狀態與內容 → 結果；拒答優先於其他（拒答時沒有可用的 JSON）。"""
    for item in raw.output:
        if getattr(item, "type", None) != "message":
            continue
        if any(getattr(part, "type", None) == "refusal" for part in item.content):
            return GenerationOutcome.REFUSAL
    if raw.status == "completed":
        return GenerationOutcome.COMPLETED
    if raw.status == "incomplete":
        reason = getattr(raw.incomplete_details, "reason", None)
        if reason == "max_output_tokens":
            return GenerationOutcome.MAX_TOKENS
        if reason == "content_filter":
            return GenerationOutcome.REFUSAL
    raise ProviderMalformedResponseError("unexpected response status")
