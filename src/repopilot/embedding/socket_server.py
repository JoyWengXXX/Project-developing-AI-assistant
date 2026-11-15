"""不記錄文字的隔離推論訊息處理。"""

import logging
import socket
from collections.abc import Callable

from repopilot.embedding.local_runtime import EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_protocol import receive_message, send_message

logger = logging.getLogger("repopilot.embedding.socket_server")


def handle_request(
    connection: socket.socket,
    *,
    profile: EmbeddingProfile,
    count_tokens: Callable[[str], int],
    embed: Callable[[str], list[float]],
) -> None:
    """處理單一請求；錯誤回覆不含來源文字或執行器細節。"""
    try:
        request = receive_message(connection)
        operation = request.get("operation")
        if operation == "identity" and set(request) == {"operation"}:
            response = {"ok": True, "profile_hash": profile.hash(), "profile": profile.describe()}
        elif (
            isinstance(operation, str)
            and operation in {"tokenize", "embed"}
            and set(request) == {"operation", "text"}
            and isinstance(request["text"], str)
            and 0 < len(request["text"].encode("utf-8")) <= 32768
        ):
            content = request["text"]
            tokens = count_tokens(content)
            if type(tokens) is not int or tokens < 1:
                raise ValueError("Invalid token count.")
            if operation == "tokenize":
                response = {"ok": True, "profile_hash": profile.hash(), "tokens": tokens}
            else:
                if tokens > 512:
                    raise ValueError("Input exceeds the model limit.")
                response = {
                    "ok": True,
                    "profile_hash": profile.hash(),
                    "tokens": tokens,
                    "embedding": embed(content),
                }
        else:
            raise ValueError("Invalid embedding request.")
    except (EmbeddingRuntimeError, ValueError, UnicodeError):
        response = {"ok": False, "error": "embedding_request_failed"}
    except Exception:
        logger.error("embedding_socket_handler_failed")
        response = {"ok": False, "error": "embedding_request_failed"}
    try:
        send_message(connection, response)
    except (TypeError, ValueError, UnicodeError):
        logger.error("embedding_socket_response_invalid")
        send_message(connection, {"ok": False, "error": "embedding_request_failed"})
