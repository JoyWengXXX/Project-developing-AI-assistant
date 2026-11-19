"""生成模型供應商的領域例外。

訊息只含固定文字與狀態碼；不得放入 API key、問題、片段或供應商回應內容。
"""


class QAProviderError(Exception):
    """所有供應商錯誤的基底；`code` 是穩定的機器可讀錯誤碼。"""

    code = "provider_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class UnsupportedModelError(QAProviderError):
    code = "qa_model_unsupported"


class ProviderTimeoutError(QAProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderRateLimitedError(QAProviderError):
    code = "provider_rate_limited"
    retryable = True


class ProviderUnavailableError(QAProviderError):
    """連線失敗、5xx 與供應商過載（例如 529）。"""

    code = "provider_unavailable"
    retryable = True


class ProviderAuthError(QAProviderError):
    code = "provider_auth_failed"


class ProviderPaymentRequiredError(QAProviderError):
    code = "provider_payment_required"


class ProviderRequestTooLargeError(QAProviderError):
    code = "provider_request_too_large"


class ProviderBadRequestError(QAProviderError):
    code = "provider_bad_request"


class ProviderMalformedResponseError(QAProviderError):
    """回應不是可用的結構化輸出（沒有文字、不是 JSON 物件、非預期的停止原因）。"""

    code = "provider_malformed_response"
