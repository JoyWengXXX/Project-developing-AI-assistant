"""本機 gateway 的授權、請求上限與查詢隱私。"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from repopilot.config import Settings
from repopilot.embedding import gateway, gateway_client
from repopilot.embedding.local_runtime import EmbeddingInputTooLongError, EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile

TOKEN = "f" * 40
FAKE_QUERY = "synthetic query marker"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        embedding_model_sha256="a" * 64,
        embedding_runtime_sha256="b" * 64,
        embedding_socket_path=Path.cwd() / "fake.sock",
        embedding_gateway_token=SecretStr(TOKEN),
    )


def test_gateway_requires_auth_and_never_logs_query_or_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            pass

        def verify_identity(self) -> None:
            pass

        def embed(self, value: str) -> list[float]:
            assert value == FAKE_QUERY
            return [1.0] + [0.0] * 767

    monkeypatch.setattr(gateway, "SocketEmbeddingClient", FakeEmbedding)
    profile = EmbeddingProfile.from_settings(settings)
    with TestClient(gateway.create_gateway_app(settings)) as client:
        body = {"query": FAKE_QUERY, "profile_hash": profile.hash()}
        assert client.post("/embed", json=body).status_code == 401
        wrong = client.post("/embed", json=body, headers={"Authorization": "Bearer incorrect"})
        assert wrong.status_code == 401
        response = client.post("/embed", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        assert response.json()["profile_hash"] == profile.hash()
        assert len(response.json()["embedding"]) == 768
        mismatch = client.post(
            "/embed",
            json={"query": FAKE_QUERY, "profile_hash": "0" * 64},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert mismatch.status_code == 409
        large = client.post(
            "/embed", content=b"x" * 65537, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert large.status_code == 413
    assert FAKE_QUERY not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize(
    ("status", "data", "error"),
    [
        (401, {"error": "unauthorized"}, EmbeddingRuntimeError),
        (422, {"error": "query_too_long"}, EmbeddingInputTooLongError),
        (422, {"error": "invalid_request"}, EmbeddingRuntimeError),
        (307, {"error": "redirect"}, EmbeddingRuntimeError),
        (200, {"profile_hash": "0" * 64, "embedding": [1.0] + [0.0] * 767}, EmbeddingRuntimeError),
        (200, {"profile_hash": "correct", "embedding": [0.0] * 768}, EmbeddingRuntimeError),
    ],
)
def test_gateway_client_rejects_bad_responses(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    data: dict[str, object],
    error: type[Exception],
) -> None:
    profile = EmbeddingProfile.from_settings(settings)
    if data.get("profile_hash") == "correct":
        data["profile_hash"] = profile.hash()

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"http://127.0.0.1:{settings.embedding_gateway_port}/embed"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content)["query"] == FAKE_QUERY
        return httpx.Response(status, stream=httpx.ByteStream(json.dumps(data).encode()))

    real_client = httpx.Client

    def make_client(**kwargs: object) -> httpx.Client:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "Client", make_client)
    with pytest.raises(error):
        gateway_client.embed_query(settings, profile, FAKE_QUERY)


def test_gateway_client_accepts_a_verified_unit_vector(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = EmbeddingProfile.from_settings(settings)
    real_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                json.dumps({"profile_hash": profile.hash(), "embedding": [1] + [0] * 767}).encode()
            ),
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond)),
    )
    result = gateway_client.embed_query(settings, profile, FAKE_QUERY)
    assert result == [1.0] + [0.0] * 767


@pytest.mark.parametrize(
    ("body", "headers"),
    [(b"x" * 100_001, {}), (b"{}", {"Content-Encoding": "gzip"})],
    ids=["oversized", "compressed"],
)
def test_gateway_client_rejects_large_or_compressed_response(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    headers: dict[str, str],
) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=transport))
    with pytest.raises(EmbeddingRuntimeError):
        gateway_client.embed_query(settings, EmbeddingProfile.from_settings(settings), FAKE_QUERY)
