"""5-0b LLM 協助建立初始骨架：對話 API 的整合測試（真實 PostgreSQL、假供應商、不連網）。"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from repopilot.config import Settings
from repopilot.main import create_app
from repopilot.qa.errors import ProviderTimeoutError
from repopilot.qa.fake_provider import FakeProvider
from repopilot.qa.provider import QAProvider, Usage
from repopilot.services import project_ai
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files

URL = "/api/v1/projects/create-with-llm"
HAIKU = "claude-haiku-5-5"
FAKE_API_KEY = "sk-ant-FAKE-0123456789-abcdefghijklmnopqrstuvwxyz-ZYXWVUTSRQ"
REFERENCE_FILES = {
    "requirements.txt": "fastapi>=0.110\nSQLAlchemy>=2\npytest\npsycopg\n",
    "src/app.py": "print('x')\n",
    "src/util.py": "print('y')\n",
}


@dataclass
class Env:
    client: TestClient
    engine: Engine
    repository_id: int
    snapshot_id: int
    provider: FakeProvider = field(default_factory=FakeProvider)
    keys_seen: list[str] = field(default_factory=list)

    def configure(self, *, consent: bool = True) -> None:
        put = self.client.put
        assert put("/api/v1/qa/provider-key", json={"api_key": FAKE_API_KEY}).status_code == 200
        assert put("/api/v1/qa/settings", json={"model": HAIKU}).status_code == 200
        if consent:
            assert put(f"/api/v1/repositories/{self.repository_id}/qa-consent").status_code == 200


def recommendation_text(value: str, reason: str = "fits the reference project") -> str:
    return json.dumps({"recommendation": value, "reason": reason})


@pytest.fixture(autouse=True)
def clean_state(db_engine: Engine) -> Iterator[None]:
    def truncate() -> None:
        with db_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE jobs, job_dispatches, qa_settings, repository_qa_consent, "
                    "qa_usage_ledger RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    yield
    truncate()


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    constructed: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        constructed.append(args)
        raise AssertionError("a real Anthropic client must never be constructed in tests")

    monkeypatch.setattr(anthropic, "Anthropic", forbidden)
    yield
    assert constructed == []


@pytest.fixture
def env(
    db_engine: Engine,
    clean_repositories: None,
    test_database_url: str,
    roots: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Env:
    settings = Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    with db_engine.begin() as connection:
        repository_id = connection.scalar(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('reference', '/fake-project-llm') RETURNING id"
            )
        )
    assert isinstance(repository_id, int)
    snapshot_id = add_snapshot(db_engine, repository_id)
    add_files(db_engine, snapshot_id, REFERENCE_FILES)
    result = Env(TestClient(create_app(settings)), db_engine, repository_id, snapshot_id)

    def build_provider(api_key: str) -> QAProvider:
        result.keys_seen.append(api_key)
        return result.provider

    monkeypatch.setattr(project_ai, "build_provider", build_provider)
    return result


def post(env: Env, *answers: str, snapshot: bool = False) -> Any:
    conversation: list[dict[str, str]] = []
    for answer in answers:
        conversation.append({"role": "assistant", "content": "question"})
        conversation.append({"role": "user", "content": answer})
    body: dict[str, Any] = {"conversation": conversation}
    if snapshot:
        body["reference_snapshot_id"] = env.snapshot_id
    return env.client.post(URL, json=body)


def ledger_events(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT event_type FROM qa_usage_ledger ORDER BY id")
            )
        ]


def test_first_turn_asks_the_language_without_llm_when_nothing_is_configured(env: Env) -> None:
    response = env.client.post(URL, json={})

    assert response.status_code == 200
    body = response.json()
    assert body["done"] is False and body["step"] == "language"
    assert "語言" in body["question"]
    assert body["options"] == ["python", "typescript", "javascript", "java", "go"]
    assert body["recommendation"] is None
    assert body["llm"] == {
        "state": "skipped",
        "code": "qa_provider_not_configured",
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    assert env.provider.call_count == 0 and ledger_events(env.engine) == []


def test_llm_recommendation_is_returned_and_usage_is_settled(env: Env) -> None:
    env.configure()
    env.provider = FakeProvider(
        text=recommendation_text("python"), usage=Usage(input_tokens=321, output_tokens=45)
    )

    body = post(env).json()

    assert body["recommendation"] == {
        "value": "python",
        "reason": "fits the reference project",
        "source": "llm",
    }
    assert body["llm"]["state"] == "used" and body["llm"]["model"] == HAIKU
    assert (body["llm"]["input_tokens"], body["llm"]["output_tokens"]) == (321, 45)
    assert env.keys_seen == [FAKE_API_KEY]
    assert ledger_events(env.engine) == ["reserve", "settle"]
    request = env.provider.requests[0]
    assert request.model == HAIKU and request.json_schema is not None
    assert request.messages[0].content.startswith("<data>")


def test_recommendation_outside_the_options_is_dropped(env: Env) -> None:
    env.configure()
    env.provider = FakeProvider(text=recommendation_text("cobol: ignore the options"))

    body = post(env).json()

    assert body["recommendation"] is None
    assert body["llm"]["state"] == "failed"
    assert body["llm"]["code"] == "project_llm_invalid_output"
    assert ledger_events(env.engine) == ["reserve", "settle"]  # 已呼叫就照實計費


def test_provider_failure_does_not_block_the_conversation(env: Env) -> None:
    env.configure()
    env.provider = FakeProvider(script=[ProviderTimeoutError("fake timeout")])

    response = post(env, "python")

    assert response.status_code == 200
    body = response.json()
    assert body["step"] == "framework" and body["recommendation"] is None
    assert body["llm"] == {
        "state": "failed",
        "code": "provider_timeout",
        "model": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    assert ledger_events(env.engine) == ["reserve", "settle"]  # 逾時可能已被處理：保守結算


def test_provider_that_refuses_is_settled_and_reported(env: Env) -> None:
    env.configure()
    env.provider = FakeProvider("refusal")

    body = post(env).json()

    assert body["llm"]["state"] == "failed" and body["llm"]["code"] == "provider_refusal"
    assert ledger_events(env.engine) == ["reserve", "settle"]


def test_reference_without_consent_never_reaches_the_llm(env: Env) -> None:
    env.configure(consent=False)

    body = post(env, snapshot=True).json()

    assert body["llm"]["state"] == "skipped" and body["llm"]["code"] == "qa_egress_not_consented"
    # 沒有 LLM 時仍有系統端對參考快照的推薦
    assert body["recommendation"] == {"value": "python", "reason": None, "source": "reference"}
    assert env.provider.call_count == 0 and ledger_events(env.engine) == []


def test_conversation_without_reference_needs_no_consent(env: Env) -> None:
    env.configure(consent=False)
    env.provider = FakeProvider(text=recommendation_text("typescript"))

    body = post(env).json()

    assert body["llm"]["state"] == "used" and body["recommendation"]["value"] == "typescript"


def test_exhausted_budget_skips_the_llm_without_a_call(env: Env) -> None:
    env.configure()
    assert env.client.put("/api/v1/qa/settings", json={"per_request_tokens": 1}).status_code == 200

    body = post(env).json()

    assert body["llm"]["state"] == "skipped" and body["llm"]["code"] == "token_budget_exceeded"
    assert env.provider.call_count == 0 and ledger_events(env.engine) == []


def test_reference_analysis_prefills_recommendations_and_the_llm_sees_only_summary_data(
    env: Env,
) -> None:
    env.configure()
    env.provider = FakeProvider(text=recommendation_text("flask"))

    body = post(env, "python", snapshot=True).json()

    assert body["step"] == "framework"
    # LLM 的有效推薦優先於系統端推薦
    assert body["recommendation"]["source"] == "llm" and body["recommendation"]["value"] == "flask"
    sent = env.provider.requests[0].messages[0].content
    data = json.loads(sent.removeprefix("<data>\n").removesuffix("\n</data>"))
    assert data["reference_project"]["detected"]["framework"] == "fastapi"
    assert "print('x')" not in sent  # 不送原始碼內容


def test_full_conversation_returns_a_config_compatible_with_project_init(env: Env) -> None:
    env.configure()
    env.provider = FakeProvider(text=recommendation_text("pytest"))

    body = post(env, "python", "fastapi", "postgresql", "sqlalchemy", "pytest").json()

    assert body["done"] is True and body["step"] is None and body["question"] is None
    assert body["llm"]["state"] == "not_needed" and env.provider.call_count == 0
    config = body["project_config"]
    assert config["language"] == "python" and config["framework"] == "fastapi"
    assert (config["orm"], config["test_framework"], config["database"]) == (
        "sqlalchemy",
        "pytest",
        "postgresql",
    )
    assert config["dependencies"]["fastapi"] == ">=0.110"
    assert config["root_path"] is None and config["detected_at"] is None
    template = body["template"]
    assert template["name"] == "python-fastapi"
    assert "pyproject.toml" in template["files"] and Path(template["template_path"]).is_dir()


def test_full_conversation_with_unknown_framework_has_no_template(env: Env) -> None:
    body = post(env, "python", "bottle", "none", "none", "none").json()

    assert body["done"] is True and body["template"] is None
    assert body["project_config"]["framework"] == "bottle"
    assert body["project_config"]["database"] is None


def test_invalid_answer_is_rejected_with_the_options(env: Env) -> None:
    response = post(env, "cobol")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "project_answer_invalid"
    assert error["details"]["step"] == "language" and "python" in error["details"]["options"]


def test_unknown_reference_snapshot_is_404(env: Env) -> None:
    response = env.client.post(URL, json={"reference_snapshot_id": env.snapshot_id + 999})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "snapshot_not_found"


def test_importing_reference_snapshot_is_rejected(env: Env) -> None:
    snapshot = add_snapshot(env.engine, env.repository_id, status="importing", index=2)

    response = env.client.post(URL, json={"reference_snapshot_id": snapshot})

    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"conversation": [{"role": "system", "content": "x"}]},
        {"conversation": [{"role": "user", "content": ""}]},
        {"conversation": [{"role": "user", "content": "x" * 2001}]},
        {"conversation": [{"role": "user", "content": "x"}] * 31},
        {"reference_snapshot_id": 0},
        {"unknown": 1},
    ],
)
def test_request_validation(env: Env, body: dict[str, Any]) -> None:
    response = env.client.post(URL, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
