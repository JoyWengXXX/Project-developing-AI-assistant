"""以本機假執行器測試模型 subprocess 與 HTTP 邊界。"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from repopilot.embedding.local_runtime import (
    EmbeddingInputTooLongError,
    EmbeddingRuntimeError,
    LocalEmbeddingRuntime,
)

FAKE_SERVER = r"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

model = sys.argv[sys.argv.index("--model") + 1]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, value):
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/v1/models":
            dim = 1 if model.endswith("wrong.gguf") else 768
            self.send_json(200, {"data": [{"id": model, "meta": {"n_ctx": 512, "n_embd": dim}}]})
        else:
            self.send_json(200, {"status": "ok"})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tokenize":
            print(payload["content"], flush=True)
            self.send_json(200, {"tokens": list(range(len(payload["content"]) + 2))})
        elif self.path == "/v1/embeddings":
            text = payload["input"]
            vector = [1.0] + [0.0] * 767
            usage = len(text) + 2
            index = 0
            if text == "bad":
                vector = [0.0] * 768
            elif text == "nan":
                vector[0] = float("nan")
            elif text == "overflow":
                vector[0] = 10 ** 400
            elif text == "wrong-usage":
                usage += 1
            elif text == "bool-index":
                index = False
            self.send_json(200, {"data": [{"index": index, "embedding": vector}],
                                 "usage": {"prompt_tokens": usage}})
        else:
            self.send_json(404, {"error": "missing"})

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(f"server_on_li: listening on http://127.0.0.1:{server.server_port}", flush=True)
server.serve_forever()
"""


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    model = tmp_path / "fake.gguf"
    model.write_bytes(b"fake model")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    real_popen = subprocess.Popen

    def fake_popen(args: list[str], **kwargs: Any) -> Any:
        return real_popen([sys.executable, "-u", str(script), *args[1:]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return Path(sys.executable), model, digest


def test_starts_local_process_embeds_and_stops(fake_runtime: tuple[Path, Path, str]) -> None:
    executable, model, digest = fake_runtime
    with LocalEmbeddingRuntime(executable=executable, model=model, model_sha256=digest) as runtime:
        vector = runtime.embed("hello")
        assert len(vector) == 768 and vector[0] == 1.0
    with pytest.raises(EmbeddingRuntimeError):
        runtime.embed("hello")


def test_rejects_long_input_before_embedding(fake_runtime: tuple[Path, Path, str]) -> None:
    executable, model, digest = fake_runtime
    with LocalEmbeddingRuntime(executable=executable, model=model, model_sha256=digest) as runtime:
        assert len(runtime.embed("x" * 510)) == 768
        with pytest.raises(EmbeddingInputTooLongError):
            runtime.embed("x" * 511)


@pytest.mark.parametrize("text", ["bad", "nan", "overflow", "wrong-usage", "bool-index"])
def test_rejects_invalid_embedding_response(
    fake_runtime: tuple[Path, Path, str], text: str
) -> None:
    executable, model, digest = fake_runtime
    with (
        LocalEmbeddingRuntime(executable=executable, model=model, model_sha256=digest) as runtime,
        pytest.raises(EmbeddingRuntimeError),
    ):
        runtime.embed(text)


def test_rejects_mismatched_model_digest(fake_runtime: tuple[Path, Path, str]) -> None:
    executable, model, _ = fake_runtime
    with (
        pytest.raises(EmbeddingRuntimeError),
        LocalEmbeddingRuntime(executable=executable, model=model, model_sha256="0" * 64),
    ):
        pass


def test_source_and_runtime_output_are_not_logged(
    fake_runtime: tuple[Path, Path, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    executable, model, digest = fake_runtime
    marker = "FAKE_SNAPSHOT_CONTENT_SENTINEL"
    with LocalEmbeddingRuntime(executable=executable, model=model, model_sha256=digest) as runtime:
        runtime.embed(marker)
    assert marker not in caplog.text


def test_wrong_model_metadata_terminates_started_process(
    fake_runtime: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, model, digest = fake_runtime
    wrong_model = model.rename(model.with_name("wrong.gguf"))
    processes: list[subprocess.Popen[str]] = []
    start = subprocess.Popen

    def record_process(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        process: subprocess.Popen[str] = start(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record_process)
    with (
        pytest.raises(EmbeddingRuntimeError),
        LocalEmbeddingRuntime(executable=executable, model=wrong_model, model_sha256=digest),
    ):
        pass
    assert len(processes) == 1
    assert processes[0].poll() is not None
