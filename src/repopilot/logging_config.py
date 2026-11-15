import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from repopilot.config import LogLevel
from repopilot.request_context import request_id_var

APP_LOGGER_NAME = "repopilot"

_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)


class JsonFormatter(logging.Formatter):
    """將日誌輸出為單行 JSON，並自動帶入目前請求的 request_id。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: LogLevel) -> None:
    """設定應用程式 logger；可重複呼叫而不重複加入 handler。"""
    logger = logging.getLogger(APP_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    # httpx 在 INFO 會記錄請求的 URL；縱使目前不含標頭，仍壓到 WARNING，
    # 避免日後升級或除錯設定意外把含 Authorization 的請求細節寫進日誌
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # SQLFluff 的 debug 日誌可能包含來源文字；解析錯誤由索引狀態回報。
    for sql_logger in ("sqlfluff.lexer", "sqlfluff.parser", "sqlfluff.linter"):
        logging.getLogger(sql_logger).setLevel(logging.CRITICAL + 1)
