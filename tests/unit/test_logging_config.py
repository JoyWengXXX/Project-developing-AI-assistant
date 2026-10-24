import json
import logging
import sys

from repopilot.logging_config import APP_LOGGER_NAME, JsonFormatter, configure_logging
from repopilot.request_context import request_id_var


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("repopilot.test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_request_id_and_extra_fields() -> None:
    token = request_id_var.set("req-log")
    try:
        output = JsonFormatter().format(_record("request_completed", duration_ms=1.5))
    finally:
        request_id_var.reset(token)

    payload = json.loads(output)
    assert payload["message"] == "request_completed"
    assert payload["request_id"] == "req-log"
    assert payload["duration_ms"] == 1.5
    assert payload["level"] == "INFO"
    assert "args" not in payload


def test_json_formatter_outputs_null_request_id_outside_request() -> None:
    payload = json.loads(JsonFormatter().format(_record("startup")))

    assert payload["request_id"] is None


def test_json_formatter_includes_exception_text() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exc_info"]


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")

    logger = logging.getLogger(APP_LOGGER_NAME)
    assert len(logger.handlers) == 1
    assert logger.level == logging.DEBUG
