"""問答失敗的純規則：例外 → 穩定錯誤碼與固定訊息、重試與退避（不碰資料庫與供應商）。

預設值（最多 3 次嘗試、退避 2 秒起指數增加、上限 20 秒、`retry-after` 同樣以 20 秒為上限）是
issue #6 規格留言決定的具名常數。訊息一律是固定文字：不含問題、片段、回答或供應商的原始回應。

.NET 對照：像 Polly 的 retry policy——只有暫時性錯誤重試，且尊重 `Retry-After`；這裡的退避沒有抖動
（單一使用者的本機工作，不需要打散並發重試）。
"""

from typing import Final

from repopilot.errors import RepoPilotError, TokenBudgetExceededError
from repopilot.qa.errors import AnswerParseError, QAProviderError
from repopilot.qa.provider import GenerationOutcome

MAX_ATTEMPTS: Final = 3
BACKOFF_BASE_SECONDS: Final = 2.0
BACKOFF_CAP_SECONDS: Final = 20.0
# 等待期間檢查取消的間隔
SLEEP_SLICE_SECONDS: Final = 0.5

OUTCOME_CODES: Final = {
    GenerationOutcome.MAX_TOKENS: "provider_max_tokens",
    GenerationOutcome.REFUSAL: "provider_refusal",
    GenerationOutcome.CONTEXT_WINDOW_EXCEEDED: "provider_context_window_exceeded",
}

PROVIDER_MESSAGES: Final = {
    "provider_max_tokens": "The provider stopped at max_tokens; the answer is incomplete.",
    "provider_refusal": "The provider refused to answer.",
    "provider_context_window_exceeded": "The request exceeded the model's context window.",
    "provider_timeout": "The provider did not respond in time.",
    "provider_rate_limited": "The provider rate-limited the request.",
    "provider_unavailable": "The provider was unavailable or overloaded.",
    "provider_payment_required": (
        "The provider requires payment; check the billing of the provider account."
    ),
    "provider_request_too_large": "The request was too large for the provider.",
    "provider_malformed_response": "The provider returned a response that could not be used.",
    "provider_auth_failed": "The provider rejected the API key.",
    "provider_bad_request": "The provider rejected the request as invalid.",
    "provider_spend_limit_reached": (
        "The provider's organization spend limit was reached. This is a limit on the PROVIDER "
        "side, not RepoPilot's token budget; raise it in the provider console or wait for it "
        "to reset."
    ),
    "qa_model_unsupported": "The selected model is not supported.",
}
_PROVIDER_FALLBACK_MESSAGE: Final = "The provider call failed."
_ANSWER_MESSAGE: Final = "The model's answer did not follow the required format."


class ProviderOutcomeError(Exception):
    """供應商回了不是 COMPLETED 的結果（max_tokens、拒答、超出上下文）；不可重試。"""

    def __init__(self, outcome: GenerationOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


def describe_failure(exc: BaseException) -> tuple[str, str] | None:
    """已知的問答失敗 → (穩定錯誤碼, 固定訊息)；不認得則 None（呼叫端處理基礎設施錯誤）。"""
    if isinstance(exc, ProviderOutcomeError):
        code = OUTCOME_CODES[exc.outcome]
        return code, PROVIDER_MESSAGES[code]
    if isinstance(exc, TokenBudgetExceededError):
        return exc.code, f"The token budget does not allow this request ({exc.reason})."
    if isinstance(exc, QAProviderError):
        return exc.code, PROVIDER_MESSAGES.get(exc.code, _PROVIDER_FALLBACK_MESSAGE)
    if isinstance(exc, AnswerParseError):
        return exc.code, _ANSWER_MESSAGE
    if isinstance(exc, RepoPilotError):
        return exc.code, exc.message
    return None


def backoff_seconds(retry_number: int) -> float:
    """第 n 次重試前的等待：`min(上限, 2 秒 × 2^(n-1))`；`retry_number` 從 1 起算。"""
    if retry_number < 1:
        raise ValueError("retry_number must be >= 1.")
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * float(2 ** min(retry_number - 1, 62)))


def retry_delay_seconds(exc: BaseException, retry_number: int) -> float | None:
    """這個錯誤之後要等多久再重試；None＝不重試。

    只有暫時性的供應商錯誤才重試。供應商給了 `retry-after` 就照它等；超過上限
    （`BACKOFF_CAP_SECONDS`）視為不重試，直接失敗。
    """
    if not isinstance(exc, QAProviderError) or not exc.retryable:
        return None
    if exc.retry_after_seconds is None:
        return backoff_seconds(retry_number)
    if exc.retry_after_seconds > BACKOFF_CAP_SECONDS:
        return None
    return max(0.0, exc.retry_after_seconds)
