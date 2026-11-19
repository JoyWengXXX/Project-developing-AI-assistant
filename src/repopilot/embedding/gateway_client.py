"""API 只向本機 loopback 的受限 gateway 傳送查詢。"""

import json

import httpx

from repopilot.config import Settings
from repopilot.embedding.local_runtime import EmbeddingInputTooLongError, EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.vector_validation import validate_vector
from repopilot.errors import EmbeddingNotConfiguredError

_MAX_RESPONSE_BYTES = 100_000


def _call(settings: Settings, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
    """向 gateway 送一個受限的請求：不跟隨轉址、不接受壓縮、回應有大小上限。"""
    token = settings.embedding_gateway_token.get_secret_value()
    if not token:
        raise EmbeddingNotConfiguredError("The embedding gateway token is not configured.")
    url = f"http://127.0.0.1:{settings.embedding_gateway_port}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with (
            httpx.Client(timeout=30.0, trust_env=False, follow_redirects=False) as client,
            client.stream(method, url, content=body, headers=headers) as response,
        ):
            status_code = response.status_code
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise EmbeddingRuntimeError("The local embedding gateway returned invalid data.")
            result = bytearray()
            for block in response.iter_raw(chunk_size=16384):
                if len(result) + len(block) > _MAX_RESPONSE_BYTES:
                    raise EmbeddingRuntimeError(
                        "The local embedding gateway returned invalid data."
                    )
                result.extend(block)
    except httpx.HTTPError:
        raise EmbeddingRuntimeError("The local embedding gateway is unavailable.") from None
    return status_code, bytes(result)


def embedding_devices(settings: Settings) -> dict[str, dict[str, bool]]:
    """各運算裝置的狀態：`{"cpu": {"configured", "available"}, "gpu": {...}}`。"""
    status_code, raw = _call(settings, "GET", "/devices", None)
    if status_code != 200:
        raise EmbeddingRuntimeError("The local embedding gateway is unavailable.")
    try:
        data = json.loads(raw)
        devices = data["devices"]
        result = {
            name: {
                "configured": devices[name]["configured"],
                "available": devices[name]["available"],
            }
            for name in ("cpu", "gpu")
        }
    except (ValueError, KeyError, TypeError, RecursionError):
        raise EmbeddingRuntimeError("The local embedding gateway returned invalid data.") from None
    if any(type(flag) is not bool for state in result.values() for flag in state.values()):
        raise EmbeddingRuntimeError("The local embedding gateway returned invalid data.")
    return result


def embed_query(settings: Settings, profile: EmbeddingProfile, query: str) -> list[float]:
    token = settings.embedding_gateway_token.get_secret_value()
    if not token:
        raise EmbeddingNotConfiguredError("The embedding gateway token is not configured.")
    try:
        query_bytes = query.encode("utf-8")
    except UnicodeEncodeError:
        raise EmbeddingRuntimeError("The embedding query is not valid UTF-8.") from None
    if len(query_bytes) > 32768:
        raise EmbeddingInputTooLongError("Embedding input exceeds the byte limit.")
    body = json.dumps(
        {"profile_hash": profile.hash(), "query": query}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    status_code, result = _call(settings, "POST", "/embed", body)
    try:
        data = json.loads(result)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise EmbeddingRuntimeError("The local embedding gateway returned invalid data.") from None
    if status_code == 422 and data == {"error": "query_too_long"}:
        raise EmbeddingInputTooLongError("The embedding query exceeds the model limit.")
    if status_code != 200:
        raise EmbeddingRuntimeError("The local embedding gateway is unavailable.")
    if not isinstance(data, dict) or data.get("profile_hash") != profile.hash():
        raise EmbeddingRuntimeError("The local embedding gateway profile does not match.")
    return validate_vector(data.get("embedding"))
