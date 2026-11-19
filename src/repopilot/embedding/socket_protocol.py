"""隔離推論的有界訊息格式。"""

import json
import socket
import struct
from typing import Any

MAX_MESSAGE_BYTES = 1_000_000


def _read_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        part = connection.recv(length - len(data))
        if not part:
            raise ValueError("The embedding message was truncated.")
        data.extend(part)
    return bytes(data)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The embedding message contains duplicate fields.")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("The embedding message contains an invalid number.")


def send_message(connection: socket.socket, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) > MAX_MESSAGE_BYTES:
        raise ValueError("The embedding message is too large.")
    connection.sendall(struct.pack("!I", len(body)) + body)


def receive_message(connection: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _read_exact(connection, 4))[0]
    if size == 0 or size > MAX_MESSAGE_BYTES:
        raise ValueError("The embedding message has an invalid size.")
    try:
        message = json.loads(
            _read_exact(connection, size),
            object_pairs_hook=_unique_pairs,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("The embedding message is invalid.") from None
    if not isinstance(message, dict):
        raise ValueError("The embedding message must be an object.")
    return message
