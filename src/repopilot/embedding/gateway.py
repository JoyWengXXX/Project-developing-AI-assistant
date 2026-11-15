"""受限的本機查詢橋接：HTTP 只提供已驗證的私有 socket 推論。"""

import hmac
import logging

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repopilot.config import Settings
from repopilot.embedding.local_runtime import EmbeddingInputTooLongError, EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_client import SocketEmbeddingClient
from repopilot.errors import EmbeddingNotConfiguredError

_MAX_REQUEST_BYTES = 65536


class GatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    query: str = Field(min_length=1, max_length=32768, strict=True)


def create_gateway_app(settings: Settings) -> FastAPI:
    profile = EmbeddingProfile.from_settings(settings)
    token = settings.embedding_gateway_token.get_secret_value()
    if not token or settings.embedding_socket_path is None:
        raise EmbeddingNotConfiguredError("The embedding gateway is not configured.")
    client = SocketEmbeddingClient(socket_path=settings.embedding_socket_path, profile=profile)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/embed")
    async def embed(request: Request) -> JSONResponse:
        authorization = request.headers.get("authorization", "").encode("utf-8")
        expected = f"Bearer {token}".encode("ascii")
        if not hmac.compare_digest(authorization, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > _MAX_REQUEST_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body.extend(part)
        try:
            data = GatewayRequest.model_validate_json(body)
            if len(data.query.encode("utf-8")) > 32768:
                return JSONResponse({"error": "query_too_long"}, status_code=422)
        except (ValidationError, UnicodeEncodeError):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        if data.profile_hash != profile.hash():
            return JSONResponse({"error": "profile_mismatch"}, status_code=409)
        try:
            await run_in_threadpool(client.verify_identity)
            vector = await run_in_threadpool(client.embed, data.query)
        except EmbeddingInputTooLongError:
            return JSONResponse({"error": "query_too_long"}, status_code=422)
        except EmbeddingRuntimeError:
            return JSONResponse({"error": "embedding_unavailable"}, status_code=503)
        return JSONResponse({"profile_hash": profile.hash(), "embedding": vector})

    return app


def main() -> int:
    try:
        app = create_gateway_app(Settings(_env_file=None))
    except (EmbeddingNotConfiguredError, ValueError):
        logging.getLogger(__name__).error("The embedding gateway configuration is invalid.")
        return 1
    uvicorn.run(app, host="0.0.0.0", port=8765, access_log=False, proxy_headers=False)  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
