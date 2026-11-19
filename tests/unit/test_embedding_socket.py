"""隔離推論協定的資料與錯誤邊界。"""

import socket
import struct
import threading
from pathlib import Path

import pytest

from repopilot.embedding.local_runtime import EmbeddingInputTooLongError, EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_client import SocketEmbeddingClient
from repopilot.embedding.socket_protocol import receive_message, send_message
from repopilot.embedding.socket_server import handle_request


def test_framed_messages_round_trip_with_unicode() -> None:
    client, server = socket.socketpair()
    message = {"text": "fake source 測試😀"}
    thread = threading.Thread(target=send_message, args=(server, message))
    thread.start()
    try:
        assert receive_message(client) == message
    finally:
        client.close()
        thread.join(timeout=5)
        server.close()


@pytest.mark.parametrize("body", [b"{}", b"[]", b"not-json"])
def test_rejects_malformed_or_oversized_frames(body: bytes) -> None:
    client, server = socket.socketpair()
    try:
        if body == b"{}":
            server.sendall(struct.pack("!I", 1_000_001))
        else:
            server.sendall(struct.pack("!I", len(body)) + body)
        with pytest.raises(ValueError):
            receive_message(client)
    finally:
        client.close()
        server.close()


def test_messages_cannot_exceed_the_protocol_limit() -> None:
    client, server = socket.socketpair()
    try:
        with pytest.raises(ValueError):
            send_message(client, {"text": "x" * 1_000_001})
    finally:
        client.close()
        server.close()


def test_rejects_duplicate_json_fields() -> None:
    client, server = socket.socketpair()
    body = b'{"value":1,"value":2}'
    try:
        server.sendall(struct.pack("!I", len(body)) + body)
        with pytest.raises(ValueError):
            receive_message(client)
    finally:
        client.close()
        server.close()


def test_client_validates_identity_and_vectors_over_framed_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    client = SocketEmbeddingClient(socket_path=Path.cwd() / "fake.sock", profile=profile)
    threads: list[threading.Thread] = []

    def connect() -> socket.socket:
        local, peer = socket.socketpair()

        def serve() -> None:
            with peer:
                handle_request(
                    peer,
                    profile=profile,
                    count_tokens=lambda text: len(text) + 1,
                    embed=lambda text: [1.0] + [0.0] * 767,
                )

        thread = threading.Thread(target=serve)
        thread.start()
        threads.append(thread)
        return local

    monkeypatch.setattr(client, "_connect", connect)
    client.verify_identity()
    assert client.count_tokens("測試") == 3
    assert client.embed("fake source")[0] == 1.0
    with pytest.raises(EmbeddingInputTooLongError):
        client.embed("x" * 512)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_server_does_not_return_or_log_source_in_unexpected_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    local, peer = socket.socketpair()

    def fail(_: str) -> int:
        raise RuntimeError("FAKE_SOURCE_SECRET")

    def serve() -> None:
        with peer:
            handle_request(peer, profile=profile, count_tokens=fail, embed=lambda text: [])

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        send_message(local, {"operation": "tokenize", "text": "FAKE_SOURCE_SECRET"})
        response = receive_message(local)
        assert response == {"ok": False, "error": "embedding_request_failed"}
        assert "embedding_socket_handler_failed" in caplog.text
        assert "FAKE_SOURCE_SECRET" not in caplog.text
    finally:
        local.close()
        thread.join(timeout=5)


def test_client_rejects_an_incompatible_runtime_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    other = EmbeddingProfile(model_sha256="c" * 64, runtime_bundle_sha256="b" * 64)
    client = SocketEmbeddingClient(socket_path=Path.cwd() / "fake.sock", profile=expected)
    threads: list[threading.Thread] = []

    def connect() -> socket.socket:
        local, peer = socket.socketpair()

        def serve() -> None:
            with peer:
                handle_request(
                    peer,
                    profile=other,
                    count_tokens=lambda text: 1,
                    embed=lambda text: [1.0] + [0.0] * 767,
                )

        thread = threading.Thread(target=serve)
        thread.start()
        threads.append(thread)
        return local

    monkeypatch.setattr(client, "_connect", connect)
    with pytest.raises(EmbeddingRuntimeError):
        client.verify_identity()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
