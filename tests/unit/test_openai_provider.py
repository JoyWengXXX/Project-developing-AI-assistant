"""OpenAI 轉接層：以假的 SDK 客戶端與 `httpx.MockTransport` 驗證（不連網、不使用真實金鑰）。"""

import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

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
from repopilot.qa.openai_provider import OpenAIProvider, build_response_params
from repopilot.qa.provider import ChatMessage, GenerateRequest, GenerationOutcome

FAKE_KEY = "FAKE_KEY_FOR_TEST"
QUESTION = "SECRET-QUESTION-MARKER"
SCHEMA: dict[str, object] = {"type": "object"}
GOOD_JSON = '{"status": "answered", "statements": []}'


def make_request(*, json_schema: dict[str, object] | None = SCHEMA) -> GenerateRequest:
    return GenerateRequest(
        model="fake-openai-model",
        system="system prompt",
        messages=(ChatMessage(role="user", content=QUESTION),),
        max_tokens=1000,
        effort="low",
        timeout_seconds=30,
        json_schema=json_schema,
        disable_thinking=True,
    )


def message_item(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": list(parts),
    }


def text_part(text: str = GOOD_JSON) -> dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": []}


def response_body(
    *,
    status: str = "completed",
    output: list[dict[str, Any]] | None = None,
    incomplete_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "fake-openai-model",
        "status": status,
        "output": [message_item(text_part())] if output is None else output,
    }
    if incomplete_reason is not None:
        body["incomplete_details"] = {"reason": incomplete_reason}
    if usage is not None:
        body["usage"] = usage
    return body


USAGE = {
    "input_tokens": 120,
    "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 0},
    "output_tokens": 900,
    "output_tokens_details": {"reasoning_tokens": 700},
    "total_tokens": 1020,
}


class Calls:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


def provider_for(handler: Any, calls: Calls | None = None) -> tuple[OpenAIProvider, Calls]:
    seen = calls or Calls()

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.requests.append(request)
        response: httpx.Response = handler(request)
        return response

    client = openai.OpenAI(
        api_key=FAKE_KEY,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(wrapped)),
    )
    return OpenAIProvider(FAKE_KEY, client=client), seen


def ok(body: dict[str, Any]) -> Any:
    return lambda _request: httpx.Response(200, json=body)


def test_success_maps_text_model_and_request_id() -> None:
    provider, _ = provider_for(
        lambda _r: httpx.Response(
            200,
            json=response_body(usage=USAGE),
            headers={"x-request-id": "req_abc"},
        )
    )

    response = provider.generate(make_request())

    assert response.outcome is GenerationOutcome.COMPLETED
    assert response.text == GOOD_JSON
    assert response.model == "fake-openai-model"
    assert response.request_id == "req_abc"


def test_usage_keeps_input_total_and_reasoning_inside_output() -> None:
    provider, _ = provider_for(ok(response_body(usage=USAGE)))

    usage = provider.generate(make_request()).usage

    assert usage.known
    assert usage.input_tokens == 120  # 已含快取讀取：不再相加
    assert usage.output_tokens == 900  # 已含推理
    assert usage.thinking_tokens == 700


def test_missing_usage_is_unknown() -> None:
    provider, _ = provider_for(ok(response_body()))

    assert not provider.generate(make_request()).usage.known


def test_request_params_carry_schema_effort_limits_and_no_storage() -> None:
    provider, calls = provider_for(ok(response_body(usage=USAGE)))

    provider.generate(make_request())

    sent = json.loads(calls.requests[0].content)
    assert sent["model"] == "fake-openai-model"
    assert sent["instructions"] == "system prompt"
    assert sent["input"] == [{"role": "user", "content": QUESTION}]
    assert sent["max_output_tokens"] == 1000
    assert sent["reasoning"] == {"effort": "low"}
    assert sent["store"] is False
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["schema"] == SCHEMA


def test_params_omit_text_format_without_schema() -> None:
    assert "text" not in build_response_params(make_request(json_schema=None))


def test_client_has_no_sdk_retries_and_uses_request_timeout() -> None:
    real = OpenAIProvider(FAKE_KEY)
    provider, calls = provider_for(ok(response_body(usage=USAGE)))

    provider.generate(make_request())

    assert real._client.max_retries == 0
    assert calls.requests[0].extensions["timeout"]["read"] == 30


def test_server_error_is_not_retried_by_the_sdk() -> None:
    provider, calls = provider_for(lambda _r: httpx.Response(503, json={"error": {"message": "x"}}))

    with pytest.raises(ProviderUnavailableError):
        provider.generate(make_request())

    assert len(calls.requests) == 1


def test_incomplete_max_output_tokens_returns_failure_with_usage() -> None:
    body = response_body(
        status="incomplete", incomplete_reason="max_output_tokens", output=[], usage=USAGE
    )
    provider, _ = provider_for(ok(body))

    response = provider.generate(make_request())

    assert response.outcome is GenerationOutcome.MAX_TOKENS
    assert response.text == ""
    assert response.usage.output_tokens == 900


def test_incomplete_content_filter_is_refusal() -> None:
    body = response_body(
        status="incomplete", incomplete_reason="content_filter", output=[], usage=USAGE
    )
    provider, _ = provider_for(ok(body))

    assert provider.generate(make_request()).outcome is GenerationOutcome.REFUSAL


def test_refusal_content_is_refusal_even_when_status_is_completed() -> None:
    refusal = {"type": "refusal", "refusal": "no"}
    provider, _ = provider_for(ok(response_body(output=[message_item(refusal)], usage=USAGE)))

    response = provider.generate(make_request())

    assert response.outcome is GenerationOutcome.REFUSAL
    assert response.text == ""
    assert response.usage.known


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress"])
def test_unexpected_status_is_malformed_with_usage(status: str) -> None:
    provider, _ = provider_for(ok(response_body(status=status, usage=USAGE)))

    with pytest.raises(ProviderMalformedResponseError) as caught:
        provider.generate(make_request())

    assert caught.value.usage is not None
    assert caught.value.usage.output_tokens == 900


def test_incomplete_with_unknown_reason_is_malformed() -> None:
    provider, _ = provider_for(ok(response_body(status="incomplete", output=[])))

    with pytest.raises(ProviderMalformedResponseError):
        provider.generate(make_request())


@pytest.mark.parametrize("text", ["not json", "[1, 2]", ""])
def test_completed_non_object_json_is_malformed_and_keeps_usage(text: str) -> None:
    provider, _ = provider_for(
        ok(response_body(output=[message_item(text_part(text))], usage=USAGE))
    )

    with pytest.raises(ProviderMalformedResponseError) as caught:
        provider.generate(make_request())

    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 120


def test_count_tokens_uses_input_token_endpoint_without_output_limits() -> None:
    provider, calls = provider_for(
        lambda _r: httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 77})
    )

    assert provider.count_tokens(make_request()) == 77
    request = calls.requests[0]
    sent = json.loads(request.content)
    assert request.url.path.endswith("/responses/input_tokens")
    assert "max_output_tokens" not in sent
    assert "store" not in sent


def error_response(status: int, code: str | None = None, **headers: str) -> httpx.Response:
    error: dict[str, Any] = {"message": f"{QUESTION} {FAKE_KEY}", "type": "x", "code": code}
    return httpx.Response(
        status, json={"error": error}, headers={"x-request-id": "req_abc", **headers}
    )


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (400, None, ProviderBadRequestError),
        (400, "context_length_exceeded", ProviderRequestTooLargeError),
        (401, None, ProviderAuthError),
        (402, None, ProviderPaymentRequiredError),
        (403, None, ProviderAuthError),
        (404, None, ProviderBadRequestError),
        (408, None, ProviderTimeoutError),
        (413, None, ProviderRequestTooLargeError),
        (429, None, ProviderRateLimitedError),
        (429, "slow_down", ProviderRateLimitedError),
        (429, "credit_balance_exhausted", ProviderPaymentRequiredError),
        (429, "insufficient_quota", ProviderPaymentRequiredError),
        (429, "organization_spend_limit_exceeded", ProviderSpendLimitReachedError),
        (429, "project_spend_limit_exceeded", ProviderSpendLimitReachedError),
        (429, "organization_usage_limit_exceeded", ProviderSpendLimitReachedError),
        (500, None, ProviderUnavailableError),
        (503, "server_is_overloaded", ProviderUnavailableError),
    ],
)
def test_status_and_code_map_to_domain_errors(
    status: int, code: str | None, error: type[QAProviderError]
) -> None:
    provider, _ = provider_for(lambda _r: error_response(status, code))

    with pytest.raises(error) as caught:
        provider.generate(make_request())

    assert caught.value.status_code == status
    assert caught.value.request_id == "req_abc"


def test_retry_after_header_is_carried_and_bad_values_ignored() -> None:
    provider, _ = provider_for(lambda _r: error_response(429, **{"retry-after": "7"}))
    with pytest.raises(ProviderRateLimitedError) as caught:
        provider.generate(make_request())
    assert caught.value.retry_after_seconds == 7.0

    provider, _ = provider_for(lambda _r: error_response(429, **{"retry-after": "soon"}))
    with pytest.raises(ProviderRateLimitedError) as caught:
        provider.generate(make_request())
    assert caught.value.retry_after_seconds is None


def test_timeout_and_connection_errors_map_to_domain_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(ProviderTimeoutError):
        provider_for(timeout)[0].generate(make_request())
    with pytest.raises(ProviderUnavailableError):
        provider_for(refused)[0].generate(make_request())


def test_unknown_exception_becomes_base_provider_error() -> None:
    client: Any = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    )

    with pytest.raises(QAProviderError) as caught:
        OpenAIProvider(FAKE_KEY, client=client).generate(make_request())

    assert type(caught.value) is QAProviderError


def test_errors_and_logs_do_not_leak_key_or_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider, _ = provider_for(lambda _r: error_response(429))

    with pytest.raises(ProviderRateLimitedError) as caught:
        provider.generate(make_request())

    shown = str(caught.value) + repr(caught.value) + caplog.text
    assert FAKE_KEY not in shown
    assert QUESTION not in shown
    assert caught.value.__cause__ is None


def test_success_log_does_not_leak_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider, _ = provider_for(ok(response_body(usage=USAGE)))

    provider.generate(make_request())

    assert QUESTION not in caplog.text
    assert GOOD_JSON not in caplog.text
    assert FAKE_KEY not in caplog.text
