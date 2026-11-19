"""僅透過受限 Unix socket 存取隔離的 embedding 執行器。"""

import math
import os
import socket
import stat
from pathlib import Path
from typing import Any

from repopilot.embedding.local_runtime import EmbeddingInputTooLongError, EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_protocol import receive_message, send_message
from repopilot.embedding.vector_validation import validate_vector


class SocketEmbeddingClient:
    def __init__(
        self, *, socket_path: Path, profile: EmbeddingProfile, timeout_seconds: float = 30.0
    ) -> None:
        if (
            not socket_path.is_absolute()
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("The socket path or timeout is invalid.")
        self._socket_path = socket_path
        self._profile = profile
        self._timeout_seconds = timeout_seconds

    def _connect(self) -> socket.socket:
        get_uid = getattr(os, "geteuid", None)
        unix_family = getattr(socket, "AF_UNIX", None)
        if os.name != "posix" or get_uid is None or unix_family is None:
            raise EmbeddingRuntimeError("The isolated embedding socket requires Linux.")
        try:
            directory = self._socket_path.parent.lstat()
            endpoint = self._socket_path.lstat()
            uid = get_uid()
            if (
                not stat.S_ISDIR(directory.st_mode)
                or not stat.S_ISSOCK(endpoint.st_mode)
                or directory.st_uid != uid
                or endpoint.st_uid != uid
                or directory.st_mode & 0o077
                or endpoint.st_mode & 0o077
            ):
                raise EmbeddingRuntimeError("The isolated embedding socket is not private.")
            connection = socket.socket(unix_family, socket.SOCK_STREAM)
            connection.settimeout(self._timeout_seconds)
            try:
                connection.connect(str(self._socket_path))
            except BaseException:
                connection.close()
                raise
            return connection
        except OSError:
            raise EmbeddingRuntimeError("The isolated embedding runtime is unavailable.") from None

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                send_message(connection, request)
                response = receive_message(connection)
        except (OSError, ValueError):
            raise EmbeddingRuntimeError("The isolated embedding runtime request failed.") from None
        if response.get("ok") is not True:
            raise EmbeddingRuntimeError("The isolated embedding runtime rejected a request.")
        if response.get("profile_hash") != self._profile.hash():
            raise EmbeddingRuntimeError("The isolated embedding runtime profile does not match.")
        return response

    def verify_identity(self) -> None:
        response = self._request({"operation": "identity"})
        if response.get("profile") != self._profile.describe():
            raise EmbeddingRuntimeError("The isolated embedding runtime profile does not match.")

    def count_tokens(self, text: str) -> int:
        if len(text.encode("utf-8")) > 32768:
            raise EmbeddingInputTooLongError("Embedding input exceeds the byte limit.")
        response = self._request({"operation": "tokenize", "text": text})
        tokens = response.get("tokens")
        if type(tokens) is not int or tokens < 1:
            raise EmbeddingRuntimeError("The isolated tokenizer returned an invalid response.")
        return tokens

    def embed(self, text: str) -> list[float]:
        tokens = self.count_tokens(text)
        if tokens > 512:
            raise EmbeddingInputTooLongError("Embedding input exceeds the model token limit.")
        response = self._request({"operation": "embed", "text": text})
        vector = response.get("embedding")
        if type(response.get("tokens")) is not int or response.get("tokens") != tokens:
            raise EmbeddingRuntimeError(
                "The isolated embedding runtime returned an invalid vector."
            )
        return validate_vector(vector)
