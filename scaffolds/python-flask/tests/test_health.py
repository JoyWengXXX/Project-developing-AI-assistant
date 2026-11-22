from app.main import app


def test_health() -> None:
    assert app.test_client().get("/health").get_json() == {"status": "ok"}
