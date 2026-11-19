"""問答失敗的純規則（4-3b）：錯誤碼對照、退避、`retry-after`、供應商錯誤分類與假供應商腳本。"""

import httpx2
import pytest
from anthropic import APIStatusError
from anthropic.types import Usage as SdkUsage

from repopilot.errors import QaQuestionNotFoundError, TokenBudgetExceededError
from repopilot.qa.anthropic_provider import classify_error
from repopilot.qa.errors import (
    AnswerEmptyStatementsError,
    AnswerInvalidFieldError,
    AnswerNotJsonError,
    AnswerNotObjectError,
    AnswerParseError,
    AnswerTooLargeError,
    AnswerUnexpectedFieldsError,
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
    UnsupportedModelError,
)
from repopilot.qa.failures import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    MAX_ATTEMPTS,
    PROVIDER_MESSAGES,
    ProviderOutcomeError,
    backoff_seconds,
    describe_failure,
    retry_delay_seconds,
)
from repopilot.qa.fake_provider import FakeProvider
from repopilot.qa.provider import GenerationOutcome, Usage
from tests.unit.test_qa_provider import (
    _HTTP_REQUEST,
    FAKE_KEY,
    QUESTION,
    StubMessages,
    make_request,
    provider_with,
    sdk_message,
)


def described(error: BaseException) -> tuple[str, str]:
    result = describe_failure(error)
    assert result is not None
    return result


# --- 預設值（issue #6 規格留言） ---


def test_qa_failures_defaults_match_the_spec() -> None:
    assert (MAX_ATTEMPTS, BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS) == (3, 2.0, 20.0)


@pytest.mark.parametrize(
    ("retry_number", "seconds"),
    [(1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0), (5, 20.0), (6, 20.0), (500, 20.0)],
)
def test_qa_failures_backoff_doubles_from_two_seconds_and_is_capped(
    retry_number: int, seconds: float
) -> None:
    assert backoff_seconds(retry_number) == seconds


def test_qa_failures_backoff_rejects_a_retry_number_below_one() -> None:
    with pytest.raises(ValueError, match="retry_number"):
        backoff_seconds(0)


# --- 是否重試、等多久 ---


@pytest.mark.parametrize(
    "error",
    [ProviderTimeoutError("x"), ProviderRateLimitedError("x"), ProviderUnavailableError("x")],
)
def test_qa_failures_transient_errors_use_the_exponential_backoff(error: QAProviderError) -> None:
    assert retry_delay_seconds(error, 1) == 2.0
    assert retry_delay_seconds(error, 2) == 4.0


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [(0.0, 0.0), (1.5, 1.5), (20.0, 20.0), (20.001, None), (3600.0, None)],
)
def test_qa_failures_retry_after_is_used_up_to_the_cap_else_no_retry(
    retry_after: float, expected: float | None
) -> None:
    error = ProviderRateLimitedError("x", retry_after_seconds=retry_after)

    assert retry_delay_seconds(error, 2) == expected


@pytest.mark.parametrize(
    "error",
    [
        ProviderAuthError("x"),
        ProviderPaymentRequiredError("x"),
        ProviderRequestTooLargeError("x"),
        ProviderBadRequestError("x"),
        ProviderMalformedResponseError("x"),
        ProviderSpendLimitReachedError("x"),
        UnsupportedModelError("x"),
        ProviderSpendLimitReachedError("x", retry_after_seconds=1.0),
        QAProviderError("x"),
    ],
)
def test_qa_failures_permanent_errors_are_never_retried(error: QAProviderError) -> None:
    assert retry_delay_seconds(error, 1) is None


def test_qa_failures_non_provider_errors_are_never_retried() -> None:
    assert retry_delay_seconds(RuntimeError("x"), 1) is None
    assert retry_delay_seconds(ProviderOutcomeError(GenerationOutcome.MAX_TOKENS), 1) is None


# --- 錯誤 → 穩定錯誤碼與固定訊息 ---

_LEAK = "LEAK-QUESTION-CHUNK-ANSWER"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderTimeoutError(_LEAK), "provider_timeout"),
        (ProviderRateLimitedError(_LEAK), "provider_rate_limited"),
        (ProviderUnavailableError(_LEAK), "provider_unavailable"),
        (ProviderPaymentRequiredError(_LEAK), "provider_payment_required"),
        (ProviderRequestTooLargeError(_LEAK), "provider_request_too_large"),
        (ProviderMalformedResponseError(_LEAK), "provider_malformed_response"),
        (ProviderAuthError(_LEAK), "provider_auth_failed"),
        (ProviderSpendLimitReachedError(_LEAK), "provider_spend_limit_reached"),
        (ProviderBadRequestError(_LEAK), "provider_bad_request"),
        (UnsupportedModelError(_LEAK), "qa_model_unsupported"),
        (ProviderOutcomeError(GenerationOutcome.MAX_TOKENS), "provider_max_tokens"),
        (ProviderOutcomeError(GenerationOutcome.REFUSAL), "provider_refusal"),
        (
            ProviderOutcomeError(GenerationOutcome.CONTEXT_WINDOW_EXCEEDED),
            "provider_context_window_exceeded",
        ),
    ],
)
def test_qa_failures_each_provider_failure_has_its_own_code_and_fixed_message(
    error: Exception, code: str
) -> None:
    code_and_message = described(error)

    assert code_and_message[0] == code
    assert code_and_message[1] == PROVIDER_MESSAGES[code]
    assert _LEAK not in code_and_message[1]


@pytest.mark.parametrize(
    "error",
    [
        AnswerParseError,
        AnswerTooLargeError,
        AnswerNotJsonError,
        AnswerNotObjectError,
        AnswerUnexpectedFieldsError,
        AnswerInvalidFieldError,
        AnswerEmptyStatementsError,
    ],
)
def test_qa_failures_unusable_answers_keep_distinct_codes_with_one_fixed_message(
    error: type[AnswerParseError],
) -> None:
    code, message = described(error(_LEAK))

    assert code == error.code
    assert _LEAK not in message
    assert message == described(AnswerNotJsonError(_LEAK))[1]


def test_qa_failures_spend_limit_message_says_it_is_the_providers_limit() -> None:
    _, message = described(ProviderSpendLimitReachedError("x"))

    assert "PROVIDER side" in message and "not RepoPilot's token budget" in message


def test_qa_failures_own_budget_error_is_reported_with_its_reason_only() -> None:
    error = TokenBudgetExceededError("daily_tokens", limit=10, requested=20, used=5)

    assert described(error) == (
        "token_budget_exceeded",
        "The token budget does not allow this request (daily_tokens).",
    )


def test_qa_failures_domain_errors_keep_their_own_code_and_message() -> None:
    assert described(QaQuestionNotFoundError("Question not found.")) == (
        "qa_question_not_found",
        "Question not found.",
    )


def test_qa_failures_unknown_exceptions_are_left_to_the_caller() -> None:
    assert describe_failure(RuntimeError(_LEAK)) is None


# --- Anthropic 轉接層：花費上限與 retry-after ---


def api_error(
    status: int, *, body: object = None, headers: dict[str, str] | None = None
) -> APIStatusError:
    response = httpx2.Response(
        status, request=_HTTP_REQUEST, headers={"request-id": "req_abc", **(headers or {})}
    )
    return APIStatusError(f"{QUESTION} {FAKE_KEY}", response=response, body=body)


_SPEND_BODY = {
    "type": "error",
    "error": {"type": "invalid_request_error", "message": "enforced_spend_limit_reached"},
}


@pytest.mark.parametrize("status", [400, 429])
def test_qa_failures_org_spend_limit_signature_is_classified_as_spend_limit(status: int) -> None:
    error = classify_error(api_error(status, body=_SPEND_BODY))

    assert isinstance(error, ProviderSpendLimitReachedError)
    assert not error.retryable and error.status_code == status
    assert retry_delay_seconds(error, 1) is None


def test_qa_failures_spend_limit_signature_wins_even_with_a_retry_after_header() -> None:
    error = classify_error(api_error(429, body=_SPEND_BODY, headers={"retry-after": "5"}))

    assert isinstance(error, ProviderSpendLimitReachedError)
    assert retry_delay_seconds(error, 1) is None


def test_qa_failures_spend_limit_signature_in_the_message_is_also_recognised() -> None:
    response = httpx2.Response(429, request=_HTTP_REQUEST)
    error = classify_error(
        APIStatusError("enforced_spend_limit_reached", response=response, body=None)
    )

    assert isinstance(error, ProviderSpendLimitReachedError)


@pytest.mark.parametrize("status", [401, 402, 413, 500, 529])
def test_qa_failures_spend_limit_signature_only_applies_to_400_and_429(status: int) -> None:
    error = classify_error(api_error(status, body=_SPEND_BODY))

    assert not isinstance(error, ProviderSpendLimitReachedError)


def test_qa_failures_low_credit_balance_400_is_a_payment_problem_not_a_bad_request() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API.",
        },
    }
    error = classify_error(api_error(400, body=body))

    assert isinstance(error, ProviderPaymentRequiredError)
    assert not error.retryable and error.status_code == 400


def test_qa_failures_other_400_stays_a_bad_request() -> None:
    error = classify_error(api_error(400, body={"error": {"message": "max_tokens too large"}}))

    assert isinstance(error, ProviderBadRequestError)


def test_qa_failures_plain_429_is_a_transient_rate_limit_with_or_without_retry_after() -> None:
    plain = classify_error(api_error(429, body={"error": {"type": "rate_limit_error"}}))
    timed = classify_error(api_error(429, headers={"retry-after": "7"}))

    assert isinstance(plain, ProviderRateLimitedError) and plain.retry_after_seconds is None
    assert isinstance(timed, ProviderRateLimitedError) and timed.retry_after_seconds == 7.0
    assert retry_delay_seconds(plain, 1) == 2.0 and retry_delay_seconds(timed, 1) == 7.0


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "-5", "nan", "inf", "", "abc"])
def test_qa_failures_unusable_retry_after_values_are_ignored(header: str) -> None:
    error = classify_error(api_error(503, headers={"retry-after": header}))

    assert isinstance(error, ProviderUnavailableError) and error.retry_after_seconds is None


def test_qa_failures_retry_after_is_kept_on_529_and_408() -> None:
    assert classify_error(api_error(529, headers={"retry-after": "3"})).retry_after_seconds == 3.0
    timeout = classify_error(api_error(408, headers={"retry-after": "4"}))
    assert isinstance(timeout, ProviderTimeoutError) and timeout.retry_after_seconds == 4.0


def test_qa_failures_classified_errors_never_carry_provider_text() -> None:
    error = classify_error(api_error(429, body=_SPEND_BODY))

    shown = str(error) + repr(error)
    assert QUESTION not in shown and FAKE_KEY not in shown and "enforced" not in shown


def test_qa_failures_malformed_response_keeps_the_reported_usage() -> None:
    stub = StubMessages(
        sdk_message(text="not json", usage=SdkUsage(input_tokens=300, output_tokens=40))
    )

    with pytest.raises(ProviderMalformedResponseError) as caught:
        provider_with(stub).generate(make_request())

    assert caught.value.usage == Usage(input_tokens=300, output_tokens=40)


# --- 假供應商腳本 ---


def test_qa_failures_fake_provider_plays_the_script_and_repeats_the_last_step() -> None:
    provider = FakeProvider(script=["rate_limit", "timeout", "success"], text='{"status": "x"}')
    request = make_request()

    with pytest.raises(ProviderRateLimitedError):
        provider.generate(request)
    with pytest.raises(ProviderTimeoutError):
        provider.generate(request)
    assert provider.generate(request).outcome is GenerationOutcome.COMPLETED
    assert provider.generate(request).outcome is GenerationOutcome.COMPLETED  # 重複最後一步
    assert provider.call_count == 4 == len(provider.requests)


def test_qa_failures_fake_provider_script_accepts_error_instances_and_the_hook_sees_each_call() -> (
    None
):
    seen: list[int] = []
    error = ProviderUnavailableError("busy", status_code=529, retry_after_seconds=9.0)
    provider = FakeProvider(script=[error], on_call=seen.append)

    with pytest.raises(ProviderUnavailableError) as caught:
        provider.generate(make_request())

    assert caught.value is error and seen == [1]


@pytest.mark.parametrize(
    ("behavior", "error"),
    [
        ("unavailable", ProviderUnavailableError),
        ("payment_required", ProviderPaymentRequiredError),
        ("request_too_large", ProviderRequestTooLargeError),
        ("auth_failed", ProviderAuthError),
        ("spend_limit", ProviderSpendLimitReachedError),
    ],
)
def test_qa_failures_fake_provider_new_behaviors_raise_the_matching_errors(
    behavior: str, error: type[QAProviderError]
) -> None:
    provider = FakeProvider(behavior)  # type: ignore[arg-type]

    with pytest.raises(error):
        provider.generate(make_request())
