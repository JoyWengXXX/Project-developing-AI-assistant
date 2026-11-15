"""本機 Granite embedding 執行環境。"""

import hashlib
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from repopilot.errors import RepoPilotError

_DIMENSIONS = 768
_MAX_TOKENS = 512
_MAX_INPUT_BYTES = 32 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_LISTENING = re.compile(r"listening on http://127\.0\.0\.1:(\d{1,5})(?=[\s/])")


class EmbeddingRuntimeError(RepoPilotError):
    """本機 embedding 執行環境失敗。"""

    code = "embedding_runtime_error"


class EmbeddingInputTooLongError(EmbeddingRuntimeError):
    """輸入超過模型限制。"""

    code = "embedding_input_too_long"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class LocalEmbeddingRuntime:
    """啟動專用本機 llama-server；輸入僅傳給此行程的 loopback 埠。"""

    def __init__(
        self,
        *,
        executable: Path,
        model: Path,
        model_sha256: str,
        timeout_seconds: float = 30,
        startup_timeout_seconds: float = 120,
    ) -> None:
        if not executable.is_absolute() or not model.is_absolute():
            raise ValueError("Executable and model paths must be absolute.")
        if re.fullmatch(r"[0-9a-f]{64}", model_sha256) is None:
            raise ValueError("model_sha256 must be a lowercase SHA-256 digest.")
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not math.isfinite(startup_timeout_seconds)
            or startup_timeout_seconds <= 0
        ):
            raise ValueError("Timeouts must be positive finite values.")
        self._executable = executable
        self._model = model
        self._expected_hash = model_sha256
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self.executable_sha256: str | None = None

    def __enter__(self) -> "LocalEmbeddingRuntime":
        if self._process is not None:
            raise EmbeddingRuntimeError("The embedding runtime is already running.")
        try:
            self._executable = self._executable.resolve(strict=True)
            self._model = self._model.resolve(strict=True)
            if not self._executable.is_file() or not self._model.is_file():
                raise EmbeddingRuntimeError("Executable or model is not a regular file.")
            if _file_sha256(self._model) != self._expected_hash:
                raise EmbeddingRuntimeError("The local model digest does not match.")
            self.executable_sha256 = _file_sha256(self._executable)
        except (OSError, RuntimeError, ValueError):
            raise EmbeddingRuntimeError("Could not read the local executable or model.") from None

        args = [
            str(self._executable),
            "--model",
            str(self._model),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--offline",
            "--embedding",
            "--pooling",
            "cls",
            "--embd-normalize",
            "2",
            "--ctx-size",
            str(_MAX_TOKENS),
            "--parallel",
            "1",
            "--threads",
            "2",
            "--threads-batch",
            "2",
            "--n-gpu-layers",
            "0",
            "--no-webui",
        ]
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LD_LIBRARY_PATH"}
        }
        try:
            process = subprocess.Popen(  # noqa: S603 - 執行檔由本機設定指定，參數不是 repo 輸入
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                shell=False,
            )
        except OSError:
            raise EmbeddingRuntimeError("Could not start the local embedding runtime.") from None
        self._process = process
        ports: queue.Queue[int | None] = queue.Queue()

        def drain_output() -> None:
            announced = False
            if process.stdout is not None:
                tail = ""
                while chunk := process.stdout.readline(4096):
                    if not announced:
                        output = tail + chunk
                        match = _LISTENING.search(output)
                        if match:
                            ports.put(int(match.group(1)))
                            announced = True
                        tail = output[-256:]
            ports.put(None)

        self._reader = threading.Thread(target=drain_output, daemon=True)
        self._reader.start()
        ready = False
        deadline = time.monotonic() + self._startup_timeout_seconds
        try:
            try:
                port = ports.get(timeout=self._startup_timeout_seconds)
            except queue.Empty:
                raise EmbeddingRuntimeError("The local embedding runtime did not start.") from None
            if port is None or not 1 <= port <= 65535 or process.poll() is not None:
                raise EmbeddingRuntimeError("The local embedding runtime stopped during startup.")
            self._client = httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=self._timeout_seconds,
                trust_env=False,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            )
            self._wait_until_ready(deadline)
            self._validate_model()
            try:
                if _file_sha256(self._model) != self._expected_hash:
                    raise EmbeddingRuntimeError("The local model changed during startup.")
            except OSError:
                raise EmbeddingRuntimeError("Could not verify the local model.") from None
            ready = True
            return self
        finally:
            if not ready:
                self._stop()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop()

    def embed(self, text: str) -> list[float]:
        if len(text.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise EmbeddingInputTooLongError("Embedding input exceeds the byte limit.")
        token_data = self._request("POST", "/tokenize", {"content": text, "add_special": True})
        tokens = token_data.get("tokens")
        if (
            not isinstance(tokens, list)
            or not tokens
            or any(not isinstance(token, int) or isinstance(token, bool) for token in tokens)
        ):
            raise EmbeddingRuntimeError("The tokenizer returned an invalid response.")
        if len(tokens) > _MAX_TOKENS:
            raise EmbeddingInputTooLongError("Embedding input exceeds the model token limit.")
        data = self._request("POST", "/v1/embeddings", {"model": str(self._model), "input": text})
        usage = data.get("usage")
        rows = data.get("data")
        if (
            not isinstance(usage, dict)
            or type(usage.get("prompt_tokens")) is not int
            or usage.get("prompt_tokens") != len(tokens)
            or not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or type(rows[0].get("index")) is not int
            or rows[0].get("index") != 0
        ):
            raise EmbeddingRuntimeError("The embedding runtime returned an invalid response.")
        vector = rows[0].get("embedding")
        if not isinstance(vector, list) or len(vector) != _DIMENSIONS:
            raise EmbeddingRuntimeError("The embedding runtime returned an invalid vector.")
        converted: list[float] = []
        try:
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise EmbeddingRuntimeError("The embedding runtime returned an invalid vector.")
                converted.append(float(value))
        except OverflowError:
            raise EmbeddingRuntimeError(
                "The embedding runtime returned an invalid vector."
            ) from None
        if not all(math.isfinite(value) for value in converted):
            raise EmbeddingRuntimeError("The embedding runtime returned an invalid vector.")
        norm = math.sqrt(sum(value * value for value in converted))
        if not math.isfinite(norm) or abs(norm - 1.0) > 0.0001:
            raise EmbeddingRuntimeError("The embedding runtime returned an unnormalized vector.")
        return converted

    def _wait_until_ready(self, deadline: float) -> None:
        if self._client is None or self._process is None:
            raise EmbeddingRuntimeError("The embedding runtime is not running.")
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise EmbeddingRuntimeError("The local embedding runtime stopped during startup.")
            remaining = deadline - time.monotonic()
            try:
                with self._client.stream(
                    "GET", "/health", timeout=max(0.01, min(1.0, remaining))
                ) as response:
                    if response.status_code == 200:
                        return
                    if response.status_code != 503:
                        raise EmbeddingRuntimeError("The local embedding runtime is not ready.")
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            except httpx.HTTPError:
                raise EmbeddingRuntimeError("The local embedding runtime is not ready.") from None
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise EmbeddingRuntimeError("The local embedding runtime did not become ready.")

    def _validate_model(self) -> None:
        data = self._request("GET", "/v1/models")
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise EmbeddingRuntimeError("The embedding runtime reported an invalid model.")
        model_id = rows[0].get("id")
        metadata = rows[0].get("meta")
        try:
            same_path = isinstance(model_id, str) and Path(model_id).resolve(strict=True) == (
                self._model
            )
        except (OSError, ValueError):
            same_path = False
        if (
            not same_path
            or not isinstance(metadata, dict)
            or metadata.get("n_embd") != _DIMENSIONS
            or metadata.get("n_ctx") != _MAX_TOKENS
        ):
            raise EmbeddingRuntimeError("The embedding runtime loaded an unexpected model.")

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._client is None or self._process is None or self._process.poll() is not None:
            raise EmbeddingRuntimeError("The embedding runtime is not running.")
        try:
            with self._client.stream(method, path, json=payload) as response:
                if response.status_code != 200:
                    raise EmbeddingRuntimeError("The embedding runtime rejected a request.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise EmbeddingRuntimeError("The embedding response encoding is unsupported.")
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise EmbeddingRuntimeError("The embedding response is too large.")
            if self._process.poll() is not None:
                raise EmbeddingRuntimeError("The embedding runtime stopped during a request.")
            result = json.loads(body)
        except (httpx.HTTPError, ValueError):
            raise EmbeddingRuntimeError("The embedding runtime request failed.") from None
        if not isinstance(result, dict):
            raise EmbeddingRuntimeError("The embedding runtime returned invalid JSON.")
        return result

    def _stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if self._reader is not None:
                self._reader.join(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
        self._reader = None
