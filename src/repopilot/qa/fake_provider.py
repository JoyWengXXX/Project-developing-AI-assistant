"""測試用假供應商：可模擬成功與各種失敗；不連網、不使用任何金鑰。

`script` 是依序消耗的結果清單（例如 `["rate_limit", "rate_limit", "success"]`）：每次 `generate`
取下一個，用完後重複最後一個。清單元素可以是行為名稱、要拋出的 `QAProviderError`，或直接回傳的
`GenerateResponse`。`call_count` 是 `generate` 被呼叫的次數；`on_call(第幾次)` 在每次呼叫開頭執行
（測試用它在呼叫期間要求取消）。
"""

from collections.abc import Callable, Sequence
from typing import Literal

from repopilot.qa.errors import (
    ProviderAuthError,
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

FakeBehavior = Literal[
    "success",
    "timeout",
    "rate_limit",
    "unavailable",
    "payment_required",
    "request_too_large",
    "auth_failed",
    "spend_limit",
    "malformed",
    "max_tokens",
    "refusal",
    "context_window_exceeded",
]
FakeStep = FakeBehavior | QAProviderError | GenerateResponse

_OUTCOMES = {
    "max_tokens": GenerationOutcome.MAX_TOKENS,
    "refusal": GenerationOutcome.REFUSAL,
    "context_window_exceeded": GenerationOutcome.CONTEXT_WINDOW_EXCEEDED,
}

_ERRORS: dict[str, Callable[[], QAProviderError]] = {
    "timeout": lambda: ProviderTimeoutError("fake provider timeout"),
    "rate_limit": lambda: ProviderRateLimitedError("fake provider rate limited", status_code=429),
    "unavailable": lambda: ProviderUnavailableError("fake provider unavailable", status_code=503),
    "payment_required": lambda: ProviderPaymentRequiredError(
        "fake provider payment required", status_code=402
    ),
    "request_too_large": lambda: ProviderRequestTooLargeError(
        "fake provider request too large", status_code=413
    ),
    "auth_failed": lambda: ProviderAuthError("fake provider auth failed", status_code=401),
    "spend_limit": lambda: ProviderSpendLimitReachedError(
        "fake provider spend limit", status_code=429
    ),
    "malformed": lambda: ProviderMalformedResponseError("fake provider malformed response"),
}


class FakeProvider:
    def __init__(
        self,
        behavior: FakeBehavior = "success",
        *,
        script: Sequence[FakeStep] | None = None,
        on_call: Callable[[int], None] | None = None,
        text: str = '{"status": "insufficient_evidence", "statements": []}',
        usage: Usage | None = None,
        input_token_estimate: int = 100,
    ) -> None:
        self.script: list[FakeStep] = list(script) if script else [behavior]
        self.on_call = on_call
        self.text = text
        self.usage = usage or Usage(input_tokens=100, output_tokens=20)
        self.input_token_estimate = input_token_estimate
        self.requests: list[GenerateRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def count_tokens(self, request: GenerateRequest) -> int:
        get_model_info(request.model)
        return self.input_token_estimate

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        get_model_info(request.model)
        self.requests.append(request)
        call_number = len(self.requests)
        if self.on_call is not None:
            self.on_call(call_number)
        step = self.script[min(call_number, len(self.script)) - 1]
        if isinstance(step, GenerateResponse):
            return step
        if isinstance(step, QAProviderError):
            raise step
        if step in _ERRORS:
            raise _ERRORS[step]()
        outcome = _OUTCOMES.get(step, GenerationOutcome.COMPLETED)
        if outcome is GenerationOutcome.COMPLETED and request.json_schema is not None:
            validate_json_output(self.text)
        text = self.text if outcome is GenerationOutcome.COMPLETED else ""
        return GenerateResponse(
            model=request.model,
            outcome=outcome,
            text=text,
            usage=self.usage,
        )
