import re

import pytest
from fastapi.testclient import TestClient

from repopilot.api.errors import ERROR_STATUS
from tests.api.conftest import FakeConflictError


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_response_includes_generated_request_id(client: TestClient) -> None:
    response = client.get("/health")

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


def test_valid_incoming_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "client-abc_123.x"})

    assert response.headers["X-Request-ID"] == "client-abc_123.x"


@pytest.mark.parametrize("incoming", ["has space", "a" * 65, "<script>", "a/b", "中文".encode()])
def test_invalid_incoming_request_id_is_replaced(client: TestClient, incoming: str | bytes) -> None:
    value = incoming.encode() if isinstance(incoming, str) else incoming
    response = client.get("/health", headers=[(b"X-Request-ID", value)])

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


def test_request_id_is_visible_inside_sync_handler(client: TestClient) -> None:
    response = client.get("/_test/request-id", headers={"X-Request-ID": "ctx-check"})

    assert response.json() == {"request_id": "ctx-check"}


def test_unknown_route_returns_unified_404(client: TestClient) -> None:
    response = client.get("/does-not-exist", headers={"X-Request-ID": "req-404"})

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == "req-404"


def test_wrong_method_returns_unified_405(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_validation_error_returns_unified_422_without_echoing_input(client: TestClient) -> None:
    response = client.get(
        "/_test/items/not-a-number",
        params={"limit": "SECRET_LOOKING_VALUE"},
        headers={"X-Request-ID": "req-422"},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["request_id"] == "req-422"
    assert {tuple(d["loc"]) for d in error["details"]} == {
        ("path", "item_id"),
        ("query", "limit"),
    }
    assert "SECRET_LOOKING_VALUE" not in response.text


def test_unhandled_exception_returns_unified_500_without_internal_detail(
    client: TestClient,
) -> None:
    response = client.get("/_test/boom", headers={"X-Request-ID": "req-500"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req-500"
    error = response.json()["error"]
    assert error == {
        "code": "internal_error",
        "message": "Internal server error.",
        "request_id": "req-500",
    }
    assert "internal detail" not in response.text


def test_mapped_domain_error_uses_registered_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(ERROR_STATUS, FakeConflictError, 409)

    response = client.get("/_test/conflict")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "fake_conflict"


def test_unmapped_domain_error_returns_500_with_its_code(client: TestClient) -> None:
    response = client.get("/_test/unmapped")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "fake_unmapped"
