"""QA 供應商介面、假供應商與 Anthropic 轉接層（不呼叫真實 API）。"""

import json
import logging
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message, TextBlock
from anthropic.types import Usage as SdkUsage
from anthropic.types.output_tokens_details import OutputTokensDetails

from repopilot.qa.anthropic_provider import AnthropicProvider, build_message_params
from repopilot.qa.errors import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderMalformedResponseError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderRequestTooLargeError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QAProviderError,
    UnsupportedModelError,
)
from repopilot.qa.fake_provider import FakeBehavior, FakeProvider
from repopilot.qa.provider import (
    ChatMessage,
    Effort,
    GenerateRequest,
    GenerationOutcome,
)

FAKE_KEY = "sk-ant-fake-key-for-tests"
QUESTION = "SECRET-QUESTION-MARKER"
SCHEMA: dict[str, object] = {"type": "object"}
GOOD_JSON = '{"status": "answered", "statements": []}'


def make_request(
    model: str = "claude-sonnet-5-5",
    *,
    effort: Effort = "medium",
    disable_thinking: bool = False,
    json_schema: dict[str, object] | None = SCHEMA,
) -> GenerateRequest:
    return GenerateRequest(
        model=model,
        system="system prompt",
        messages=(ChatMessage(role="user", content=QUESTION),),
        max_tokens=1000,
        effort=effort,
        timeout_seconds=30,
        json_schema=json_schema,
        disable_thinking=disable_thinking,
    )


@pytest.mark.parametrize(
    ("behavior", "error"),
    [
        ("timeout", ProviderTimeoutError),
        ("rate_limit", ProviderRateLimitedError),
        ("malformed", ProviderMalformedResponseError),
    ],
)
def test_fake_provider_failures_raise_domain_errors(
    behavior: FakeBehavior, error: type[QAProviderError]
) -> None:
    with pytest.raises(error):
        FakeProvider(behavior).generate(make_request())


def test_fake_provider_success_returns_unified_result() -> None:
    response = FakeProvider("success", text=GOOD_JSON).generate(make_request())

    assert response.outcome is GenerationOutcome.COMPLETED
    assert response.text == GOOD_JSON
    assert response.usage.known
    assert response.model == "claude-sonnet-5-5"


def test_fake_provider_non_json_text_is_malformed_when_schema_requested() -> None:
    with pytest.raises(ProviderMalformedResponseError):
        FakeProvider("success", text="not json").generate(make_request())


@pytest.mark.parametrize(
    ("behavior", "outcome"),
    [
        ("max_tokens", GenerationOutcome.MAX_TOKENS),
        ("refusal", GenerationOutcome.REFUSAL),
        ("context_window_exceeded", GenerationOutcome.CONTEXT_WINDOW_EXCEEDED),
    ],
)
def test_fake_provider_stop_failures_keep_usage(
    behavior: FakeBehavior, outcome: GenerationOutcome
) -> None:
    response = FakeProvider(behavior).generate(make_request())

    assert response.outcome is outcome
    assert response.text == ""
    assert response.usage.output_tokens == 20


def test_unsupported_model_is_domain_error() -> None:
    with pytest.raises(UnsupportedModelError):
        FakeProvider().generate(make_request("gpt-unknown"))


class StubMessages:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.create_kwargs: dict[str, Any] | None = None
        self.count_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        if self.error:
            raise self.error
        return self.result

    def count_tokens(self, **kwargs: Any) -> Any:
        self.count_kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(input_tokens=1234)


def provider_with(messages: StubMessages) -> AnthropicProvider:
    client: Any = SimpleNamespace(messages=messages)
    return AnthropicProvider(FAKE_KEY, client=client)


def sdk_message(
    stop_reason: str = "end_turn",
    text: str = GOOD_JSON,
    usage: SdkUsage | None = None,
) -> Message:
    fields: dict[str, Any] = {
        "id": "msg_1",
        "content": [TextBlock(type="text", text=text)],
        "model": "claude-sonnet-5-5",
        "role": "assistant",
        "stop_reason": stop_reason,
        "type": "message",
    }
    if usage is None:
        return Message.model_construct(**fields)
    return Message(**fields, usage=usage)


def test_usage_sums_cache_tokens_into_total_input() -> None:
    usage = SdkUsage(
        input_tokens=10,
        output_tokens=50,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=3000,
    )
    stub = StubMessages(sdk_message(usage=usage))

    response = provider_with(stub).generate(make_request())

    assert response.usage.input_tokens == 3210
    assert response.usage.output_tokens == 50
    assert response.usage.known


def test_missing_cache_fields_count_as_zero() -> None:
    stub = StubMessages(sdk_message(usage=SdkUsage(input_tokens=7, output_tokens=3)))

    response = provider_with(stub).generate(make_request())

    assert response.usage.input_tokens == 7


def test_missing_usage_is_marked_unknown() -> None:
    response = provider_with(StubMessages(sdk_message())).generate(make_request())

    assert not response.usage.known
    assert response.outcome is GenerationOutcome.COMPLETED


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5"])
def test_models_that_cannot_disable_thinking_never_get_disabled(model: str) -> None:
    params = build_message_params(make_request(model, disable_thinking=True))

    assert "thinking" not in params


def test_haiku_gets_disabled_thinking_up_to_high_effort() -> None:
    low = build_message_params(
        make_request("claude-haiku-5-5", effort="high", disable_thinking=True)
    )
    high = build_message_params(
        make_request("claude-haiku-5-5", effort="xhigh", disable_thinking=True)
    )
    default = build_message_params(make_request("claude-haiku-5-5", effort="high"))

    assert low["thinking"] == {"type": "disabled"}
    assert "thinking" not in high
    assert "thinking" not in default


def test_params_carry_structured_output_and_effort() -> None:
    params = build_message_params(make_request(effort="low"))

    assert params["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert params["max_tokens"] == 1000
    assert "format" not in build_message_params(make_request(json_schema=None))["output_config"]


def test_request_uses_request_timeout_and_no_sdk_retries() -> None:
    stub = StubMessages(sdk_message(usage=SdkUsage(input_tokens=1, output_tokens=1)))

    provider_with(stub).generate(make_request())
    real = AnthropicProvider(FAKE_KEY)

    assert stub.create_kwargs is not None
    assert stub.create_kwargs["timeout"] == 30
    assert real._client.max_retries == 0


@pytest.mark.parametrize(
    ("stop_reason", "outcome"),
    [
        ("max_tokens", GenerationOutcome.MAX_TOKENS),
        ("refusal", GenerationOutcome.REFUSAL),
        ("model_context_window_exceeded", GenerationOutcome.CONTEXT_WINDOW_EXCEEDED),
    ],
)
def test_failure_stop_reasons_return_failure_with_usage(
    stop_reason: str, outcome: GenerationOutcome
) -> None:
    usage = SdkUsage(input_tokens=100, output_tokens=900)
    stub = StubMessages(sdk_message(stop_reason, text="partial {", usage=usage))

    response = provider_with(stub).generate(make_request())

    assert response.outcome is outcome
    assert response.usage.output_tokens == 900
    assert response.usage.input_tokens == 100


def test_thinking_tokens_reported_when_sdk_provides_them() -> None:
    usage = SdkUsage.model_construct(
        input_tokens=1,
        output_tokens=500,
        output_tokens_details=OutputTokensDetails(thinking_tokens=400),
    )

    response = provider_with(StubMessages(sdk_message(usage=usage))).generate(make_request())

    assert response.usage.thinking_tokens == 400
    assert response.usage.output_tokens == 500


@pytest.mark.parametrize("stop_reason", ["tool_use", "pause_turn", None])
def test_unexpected_stop_reason_is_malformed(stop_reason: str | None) -> None:
    stub = StubMessages(sdk_message(stop_reason or "end_turn"))
    assert stub.result is not None
    stub.result.stop_reason = stop_reason

    with pytest.raises(ProviderMalformedResponseError):
        provider_with(stub).generate(make_request())


def test_completed_non_json_text_is_malformed() -> None:
    stub = StubMessages(sdk_message(text="not json"))

    with pytest.raises(ProviderMalformedResponseError):
        provider_with(stub).generate(make_request())


def test_count_tokens_returns_estimate_without_output_config() -> None:
    stub = StubMessages()

    estimate = provider_with(stub).count_tokens(make_request("claude-haiku-5-5"))

    assert estimate == 1234
    assert stub.count_kwargs is not None
    assert "output_config" not in stub.count_kwargs
    assert "max_tokens" not in stub.count_kwargs


_HTTP_REQUEST = httpx2.Request("POST", "https://example.invalid/v1/messages")


def status_error(status: int) -> anthropic.APIStatusError:
    response = httpx2.Response(status, request=_HTTP_REQUEST, headers={"request-id": "req_abc"})
    return anthropic.APIStatusError(f"{QUESTION} {FAKE_KEY}", response=response, body=None)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, ProviderBadRequestError),
        (401, ProviderAuthError),
        (402, ProviderPaymentRequiredError),
        (403, ProviderAuthError),
        (404, ProviderBadRequestError),
        (408, ProviderTimeoutError),
        (413, ProviderRequestTooLargeError),
        (429, ProviderRateLimitedError),
        (500, ProviderUnavailableError),
        (529, ProviderUnavailableError),
    ],
)
def test_status_codes_map_to_domain_errors(status: int, error: type[QAProviderError]) -> None:
    stub = StubMessages(error=status_error(status))

    with pytest.raises(error) as caught:
        provider_with(stub).generate(make_request())

    assert caught.value.status_code == status
    assert caught.value.request_id == "req_abc"


def test_timeout_and_connection_errors_map_to_domain_errors() -> None:
    with pytest.raises(ProviderTimeoutError):
        provider_with(StubMessages(error=anthropic.APITimeoutError(_HTTP_REQUEST))).generate(
            make_request()
        )
    with pytest.raises(ProviderUnavailableError):
        provider_with(
            StubMessages(error=anthropic.APIConnectionError(request=_HTTP_REQUEST))
        ).generate(make_request())


def test_errors_and_logs_do_not_leak_key_or_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    stub = StubMessages(error=status_error(429))

    with pytest.raises(ProviderRateLimitedError) as caught:
        provider_with(stub).generate(make_request())

    shown = str(caught.value) + repr(caught.value) + caplog.text
    assert FAKE_KEY not in shown
    assert QUESTION not in shown
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_success_log_does_not_leak_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    stub = StubMessages(sdk_message(usage=SdkUsage(input_tokens=1, output_tokens=1)))

    provider_with(stub).generate(make_request())

    assert QUESTION not in caplog.text
    assert GOOD_JSON not in caplog.text
    assert FAKE_KEY not in caplog.text


def sdk_client(handler: Any) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=FAKE_KEY,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def test_real_sdk_pipeline_sends_params_and_parses_response() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = sdk_message(
            usage=SdkUsage(input_tokens=5, output_tokens=6, cache_read_input_tokens=100)
        ).model_dump(mode="json")
        return httpx2.Response(200, json=body)

    provider = AnthropicProvider(FAKE_KEY, client=sdk_client(handler))

    response = provider.generate(make_request("claude-haiku-5-5", disable_thinking=True))

    sent = json.loads(seen[0].content)
    assert sent["thinking"] == {"type": "disabled"}
    assert sent["output_config"]["format"]["schema"] == SCHEMA
    assert sent["max_tokens"] == 1000
    assert response.usage.input_tokens == 105
    assert response.outcome is GenerationOutcome.COMPLETED


def test_real_sdk_pipeline_does_not_retry_and_classifies_529() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        error = {"type": "overloaded_error", "message": "overloaded"}
        return httpx2.Response(529, json={"type": "error", "error": error})

    provider = AnthropicProvider(FAKE_KEY, client=sdk_client(handler))

    with pytest.raises(ProviderUnavailableError) as caught:
        provider.generate(make_request())

    assert calls == 1
    assert caught.value.status_code == 529
