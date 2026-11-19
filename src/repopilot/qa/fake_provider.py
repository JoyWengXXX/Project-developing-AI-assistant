"""測試用假供應商：可模擬成功與各種失敗；不連網、不使用任何金鑰。"""

from typing import Literal

from repopilot.qa.errors import (
    ProviderMalformedResponseError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
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
    "malformed",
    "max_tokens",
    "refusal",
    "context_window_exceeded",
]

_OUTCOMES = {
    "max_tokens": GenerationOutcome.MAX_TOKENS,
    "refusal": GenerationOutcome.REFUSAL,
    "context_window_exceeded": GenerationOutcome.CONTEXT_WINDOW_EXCEEDED,
}


class FakeProvider:
    def __init__(
        self,
        behavior: FakeBehavior = "success",
        *,
        text: str = '{"status": "insufficient_evidence", "statements": []}',
        usage: Usage | None = None,
        input_token_estimate: int = 100,
    ) -> None:
        self.behavior = behavior
        self.text = text
        self.usage = usage or Usage(input_tokens=100, output_tokens=20)
        self.input_token_estimate = input_token_estimate
        self.requests: list[GenerateRequest] = []

    def count_tokens(self, request: GenerateRequest) -> int:
        get_model_info(request.model)
        return self.input_token_estimate

    def generate(self, request: GenerateRequest) -> GenerateResponse:
        get_model_info(request.model)
        self.requests.append(request)
        if self.behavior == "timeout":
            raise ProviderTimeoutError("fake provider timeout")
        if self.behavior == "rate_limit":
            raise ProviderRateLimitedError("fake provider rate limited", status_code=429)
        if self.behavior == "malformed":
            raise ProviderMalformedResponseError("fake provider malformed response")
        outcome = _OUTCOMES.get(self.behavior, GenerationOutcome.COMPLETED)
        if outcome is GenerationOutcome.COMPLETED and request.json_schema is not None:
            validate_json_output(self.text)
        text = self.text if outcome is GenerationOutcome.COMPLETED else ""
        return GenerateResponse(
            model=request.model,
            outcome=outcome,
            text=text,
            usage=self.usage,
        )
