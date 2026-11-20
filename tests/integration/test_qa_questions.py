"""問答流程成功路徑（4-3a）：提問驗證、worker 執行、引用驗證、追蹤記錄、額度結算與 migration 0021。

全程使用假供應商與假 embedding（不連網、不載入模型）；真實 PostgreSQL（`db-test`）。
`anthropic.Anthropic` 被換成會讓測試失敗的守衛，任何真實客戶端的建立都會被發現。
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import anthropic
import pytest
from alembic import command
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.main import create_app
from repopilot.qa import usage_ledger
from repopilot.qa.evaluation import EvalConfigError, load_dataset
from repopilot.qa.fake_provider import FakeProvider
from repopilot.qa.pricing import DEFAULT_PRICES
from repopilot.qa.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from repopilot.qa.provider import QAProvider, Usage
from repopilot.qa.question_rules import DEFAULT_MAX_TOKENS, input_tokens_upper_bound
from repopilot.qa.usage_ledger import UsageLimits, token_cost_usd
from repopilot.services import job_runner, qa_question_runner, retrieval_job_runner, retrieval_jobs
from repopilot.services.qa_eval_cli import DbEvalBackend, resolve_snapshot_ids
from tests.github_helpers import capture_logs
from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files

QUESTIONS_URL = "/api/v1/qa/questions"
HAIKU = "claude-haiku-5-5"
FAKE_API_KEY = "sk-ant-FAKE-0123456789-abcdefghijklmnopqrstuvwxyz-ZYXWVUTSRQ"
QUESTION_MARKER = "QMARKER4471"
CHUNK_MARKER = "CMARKER8842"
ANSWER_MARKER = "AMARKER5519"
QUESTION = f"How does the widget handler work {QUESTION_MARKER}"
PATH = "src/app.py"
QA_TABLES = {"qa_questions", "qa_context_chunks", "qa_statements", "qa_citations"}
ALL_MARKERS = (QUESTION_MARKER, CHUNK_MARKER, ANSWER_MARKER)


@dataclass(frozen=True)
class Repo:
    repository_id: int
    snapshot_id: int
    name: str
    commit_sha: str


@dataclass
class Env:
    client: TestClient
    settings: Settings
    engine: Engine
    repos: list[Repo]
    provider: FakeProvider = field(default_factory=FakeProvider)
    keys_seen: list[str] = field(default_factory=list)

    @property
    def snapshot_ids(self) -> list[int]:
        return [repo.snapshot_id for repo in self.repos]


class _FakeEmbedding:
    def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
        pass

    def verify_identity(self) -> None:
        pass

    def count_tokens(self, value: str) -> int:
        return len(value) + 1

    def embed(self, value: str) -> list[float]:
        return [1.0] + [0.0] * 767


@pytest.fixture(autouse=True)
def clean_qa_state(db_engine: Engine) -> Iterator[None]:
    # 其他 migration 測試會降版經過 0021（有資料會拒絕）：前後都清空
    def truncate() -> None:
        with db_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE qa_questions, jobs, job_dispatches, qa_settings, "
                    "repository_qa_consent, qa_usage_ledger RESTART IDENTITY CASCADE"
                )
            )

    truncate()
    yield
    truncate()


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """AC-6：任何真實的 Anthropic 客戶端建立都讓測試失敗。"""
    constructed: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        constructed.append(args)
        raise AssertionError("a real Anthropic client must never be constructed in tests")

    monkeypatch.setattr(anthropic, "Anthropic", forbidden)
    yield
    assert constructed == []


@pytest.fixture
def qa_settings(test_database_url: str, roots: Path) -> Settings:
    return Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
        embedding_model_sha256="a" * 64,
        embedding_runtime_sha256="b" * 64,
        embedding_socket_path=Path("/fake/socket"),
        job_lease_seconds=60,
        job_heartbeat_seconds=15,
        job_max_attempts=3,
    )


def _make_repo(engine: Engine, name: str, number: int) -> int:
    with engine.begin() as connection:
        repository_id = connection.scalar(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES (:name, :path) RETURNING id"
            ),
            {"name": name, "path": f"/fake-qa-{number}"},
        )
    assert isinstance(repository_id, int)
    return repository_id


def _snapshot_sha(engine: Engine, snapshot_id: int) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                text("SELECT commit_sha FROM snapshots WHERE id = :id"), {"id": snapshot_id}
            )
        )


def _index_snapshot(engine: Engine, snapshot_id: int, settings: Settings) -> None:
    with Session(engine, expire_on_commit=False) as session:
        submission = retrieval_jobs.submit_retrieval_job(session, snapshot_id, settings)
        assert submission.job is not None
        job_id = submission.job.id
    # 直接用 retrieval 服務來避免租約檢查複雜性（測試用）
    with Session(engine, expire_on_commit=False) as session:
        job = session.query(Job).filter(Job.id == job_id).one()
        from uuid import uuid4

        # 模擬租約取得（簡化版：跳過心跳）
        job.lease_token = uuid4()
        job.status = "running"
        session.flush()

        def dummy_checkpoint() -> None:
            pass

        from repopilot.services.retrieval_job_runner import run_retrieval

        try:
            finished: dict[str, str] = {}
            run_retrieval(
                session, job, job.lease_token, engine, settings, dummy_checkpoint, finished
            )
            session.commit()
        except Exception:
            session.rollback()
            raise


@pytest.fixture
def env(
    db_engine: Engine,
    clean_repositories: None,
    qa_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Env:
    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", _FakeEmbedding)
    monkeypatch.setattr(qa_question_runner, "SocketEmbeddingClient", _FakeEmbedding)
    monkeypatch.setattr(qa_question_runner, "sleep", lambda seconds: None)  # 重試退避不真的等待
    alpha = _make_repo(db_engine, "alpha", 1)
    beta = _make_repo(db_engine, "beta", 2)
    layout = [("alpha", alpha, 1), ("beta", beta, 1), ("alpha", alpha, 2)]
    repos = []
    for name, repository_id, index in layout:
        snapshot_id = add_snapshot(db_engine, repository_id, index=index)
        add_files(
            db_engine,
            snapshot_id,
            {PATH: f"widget handler {name} v{index} {CHUNK_MARKER}\n"},
        )
        _index_snapshot(db_engine, snapshot_id, qa_settings)
        repos.append(Repo(repository_id, snapshot_id, name, _snapshot_sha(db_engine, snapshot_id)))
    result = Env(TestClient(create_app(qa_settings)), qa_settings, db_engine, repos)

    def build_provider(api_key: str) -> QAProvider:
        result.keys_seen.append(api_key)
        return result.provider

    monkeypatch.setattr(qa_question_runner, "build_provider", build_provider)
    assert (
        result.client.put("/api/v1/qa/provider-key", json={"api_key": FAKE_API_KEY}).status_code
        == 200
    )
    assert result.client.put("/api/v1/qa/settings", json={"model": HAIKU}).status_code == 200
    for repository_id in (alpha, beta):
        assert (
            result.client.put(f"/api/v1/repositories/{repository_id}/qa-consent").status_code == 200
        )
    return result


# --- 輔助 ---


def _statement(text_: str, kind: str, *citations: tuple[str, int, int]) -> dict[str, object]:
    return {
        "text": text_,
        "kind": kind,
        "citations": [
            {"chunk": chunk, "start_line": start, "end_line": end}
            for chunk, start, end in citations
        ],
    }


def _answer(*statements: dict[str, object], status: str = "answered") -> str:
    return json.dumps({"status": status, "statements": list(statements)})


def _ask(env: Env, **overrides: Any) -> Any:
    body = {"snapshot_ids": env.snapshot_ids, "question": QUESTION, **overrides}
    return env.client.post(QUESTIONS_URL, json=body)


def _submit(env: Env, **overrides: Any) -> dict[str, Any]:
    response = _ask(env, **overrides)
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def _run(env: Env, submitted: dict[str, Any]) -> str:
    outcome: str = job_runner.run_job(submitted["job"]["id"], env.engine, env.settings)
    return outcome


def _get(env: Env, question_id: int) -> dict[str, Any]:
    response = env.client.get(f"{QUESTIONS_URL}/{question_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _ask_and_run(env: Env, **overrides: Any) -> dict[str, Any]:
    submitted = _submit(env, **overrides)
    _run(env, submitted)
    return _get(env, submitted["id"])


def _ledger(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT event_type, reservation_id, input_tokens, output_tokens, cost_usd, "
                "usage_unknown FROM qa_usage_ledger ORDER BY id"
            )
        )
        return [dict(row._mapping) for row in rows]


def _assert_no_open_reservations(engine: Engine) -> None:
    events = _ledger(engine)
    reserved = {e["reservation_id"] for e in events if e["event_type"] == "reserve"}
    ended = [e["reservation_id"] for e in events if e["event_type"] in ("settle", "release")]
    assert sorted(map(str, ended)) == sorted(map(str, reserved))


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


def _success_text() -> str:
    return _answer(
        _statement(f"The widget handler lives here {ANSWER_MARKER}", "fact", ("C1", 1, 1))
    )


# --- AC-1：提問 → 202 → worker → 驗證後的答案 ---


def test_qa_questions_submit_returns_202_with_estimate_and_runs_to_a_verified_answer(
    env: Env,
) -> None:
    env.provider = FakeProvider(
        text=_answer(
            _statement("Alpha handles widgets", "fact", ("C1", 1, 1), ("C2", 1, 1)),
            _statement("Probably the main entry point", "inference", ("C3", 1, 1)),
            _statement("Is there a retry policy?", "open_question"),
        ),
        usage=Usage(input_tokens=1234, output_tokens=56),
    )

    response = _ask(env)

    assert response.status_code == 202
    submitted = response.json()
    question_id = submitted["id"]
    assert response.headers["Location"] == f"/api/v1/qa/questions/{question_id}"
    assert submitted["status"] == "queued" and submitted["model"] == HAIKU
    assert submitted["status_url"] == response.headers["Location"]
    job = submitted["job"]
    assert job["kind"] == "qa_question" and job["status"] == "queued"
    assert job["params"] == {"question_id": question_id}
    assert job["repository_id"] is None and job["snapshot_id"] is None
    bound = input_tokens_upper_bound(QUESTION)
    price = DEFAULT_PRICES[HAIKU]
    assert submitted["estimate"] == {
        "input_tokens_upper_bound": bound,
        "max_output_tokens": DEFAULT_MAX_TOKENS,
        "total_tokens_upper_bound": bound + DEFAULT_MAX_TOKENS,
        "cost_usd": format(token_cost_usd(bound, DEFAULT_MAX_TOKENS, price), "f"),
    }
    queued = _get(env, question_id)
    assert queued["status"] == "queued" and queued["statements"] == []
    assert queued["job_id"] == job["id"]
    assert env.provider.requests == []  # 提問本身不呼叫供應商

    assert _run(env, submitted) == "succeeded"

    done = _get(env, question_id)
    assert done["status"] == "succeeded" and done["answer_status"] == "answered"
    assert done["error"] is None and done["rejected_statements"] == []
    shas = {repo.commit_sha for repo in env.repos}
    facts = [s for s in done["statements"] if s["kind"] == "fact"]
    assert len(done["statements"]) == 3 and len(facts) == 1
    for statement in done["statements"]:
        for citation in statement["citations"]:
            assert len(citation["commit_sha"]) == 40 and citation["commit_sha"] in shas
            assert citation["path"] == PATH
            assert citation["start_line"] == citation["end_line"] == 1
            assert citation["repository"] in ("alpha", "beta")
    assert all(s["citations"] for s in done["statements"] if s["kind"] != "open_question")
    finished_job = env.client.get(f"/api/v1/jobs/{job['id']}").json()
    assert finished_job["status"] == "succeeded" and finished_job["phase"] == "completed"
    assert finished_job["result"] == {"question_id": question_id, "answer_status": "answered"}


def test_qa_questions_provider_receives_the_expected_request(env: Env) -> None:
    env.provider = FakeProvider(text=_success_text())

    _ask_and_run(env, max_tokens=1000)

    [request] = env.provider.requests
    assert request.model == HAIKU and request.max_tokens == 1000
    assert request.system == SYSTEM_PROMPT and request.json_schema is not None
    [message] = request.messages
    assert QUESTION in message.content and CHUNK_MARKER in message.content
    assert env.keys_seen == [FAKE_API_KEY]  # 解密後的 key 只交給工廠函式


def test_qa_questions_default_max_tokens_is_used_when_omitted(env: Env) -> None:
    env.provider = FakeProvider(text=_success_text())

    done = _ask_and_run(env)

    assert done["max_tokens"] == DEFAULT_MAX_TOKENS
    assert env.provider.requests[0].max_tokens == DEFAULT_MAX_TOKENS


# --- AC-2：無效引用被標記並排除 ---


def test_qa_questions_invalid_citations_are_flagged_and_excluded(env: Env) -> None:
    env.provider = FakeProvider(
        text=_answer(
            _statement(f"good {ANSWER_MARKER}", "fact", ("C1", 1, 1)),
            _statement("unknown chunk", "fact", ("C99", 1, 1)),
            _statement("line out of range", "fact", ("C1", 5, 9)),
            _statement("fact with nothing", "fact"),
            _statement("mixed", "inference", ("C1", 1, 1), ("C2", 40, 41)),
        )
    )

    done = _ask_and_run(env)

    assert done["status"] == "succeeded" and done["answer_status"] == "answered"
    assert [s["text"] for s in done["statements"]] == [f"good {ANSWER_MARKER}"]
    rejected = {s["text"]: s for s in done["rejected_statements"]}
    assert set(rejected) == {"unknown chunk", "line out of range", "fact with nothing", "mixed"}
    reason = {text_: [r["reason"] for r in s["reasons"]] for text_, s in rejected.items()}
    assert reason["unknown chunk"] == ["unknown_chunk_code"]
    assert reason["line out of range"] == ["line_out_of_range"]
    assert reason["fact with nothing"] == ["fact_without_citation"]
    assert reason["mixed"] == ["line_out_of_range"]
    mixed = rejected["mixed"]
    assert mixed["reasons"][0]["citation_index"] == 1
    assert [c["valid"] for c in mixed["claimed_citations"]] == [True, False]
    assert mixed["claimed_citations"][1]["reasons"] == ["line_out_of_range"]
    assert (
        mixed["claimed_citations"][1]["start_line"],
        mixed["claimed_citations"][1]["end_line"],
    ) == (40, 41)


def test_qa_questions_insufficient_evidence_explanation_is_verified_and_shown(env: Env) -> None:
    env.provider = FakeProvider(
        text=_answer(
            _statement("repo covers jobs", "fact", ("C1", 1, 1)),
            _statement("invented", "fact", ("C9", 1, 1)),
            _statement("need a shop repo", "open_question"),
            status="insufficient_evidence",
        )
    )

    done = _ask_and_run(env)

    assert done["answer_status"] == "insufficient_evidence"
    assert [s["text"] for s in done["statements"]] == ["repo covers jobs", "need a shop repo"]
    assert [r["text"] for r in done["rejected_statements"]] == ["invented"]
    assert [r["reason"] for r in done["rejected_statements"][0]["reasons"]] == [
        "unknown_chunk_code"
    ]


# --- AC-3：外部呼叫前的拒絕 ---


def _assert_nothing_happened(env: Env, jobs_before: int) -> None:
    assert env.provider.requests == [] and env.keys_seen == []
    assert _ledger(env.engine) == []
    assert _count(env.engine, "qa_questions") == 0
    assert _count(env.engine, "jobs") == jobs_before


def _jobs_now(env: Env) -> int:
    return _count(env.engine, "jobs")


def test_qa_questions_no_provider_key_is_rejected_before_any_call(env: Env) -> None:
    assert env.client.delete("/api/v1/qa/provider-key").status_code == 204
    jobs_before = _jobs_now(env)

    response = _ask(env)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "qa_provider_not_configured"
    _assert_nothing_happened(env, jobs_before)


def test_qa_questions_no_model_is_rejected_before_any_call(env: Env) -> None:
    assert env.client.put("/api/v1/qa/settings", json={"model": None}).status_code == 200
    jobs_before = _jobs_now(env)

    response = _ask(env)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "qa_model_not_selected"
    _assert_nothing_happened(env, jobs_before)


def test_qa_questions_unconsented_repository_is_named_and_rejected_before_any_call(
    env: Env,
) -> None:
    beta = env.repos[1]
    assert (
        env.client.delete(f"/api/v1/repositories/{beta.repository_id}/qa-consent").status_code
        == 200
    )
    jobs_before = _jobs_now(env)

    response = _ask(env)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "qa_egress_not_consented"
    assert error["details"] == {
        "repositories": [{"repository_id": beta.repository_id, "name": "beta"}]
    }
    _assert_nothing_happened(env, jobs_before)


def test_qa_questions_consent_is_checked_for_every_selected_snapshot_only(env: Env) -> None:
    beta = env.repos[1]
    env.client.delete(f"/api/v1/repositories/{beta.repository_id}/qa-consent")
    env.provider = FakeProvider(text=_success_text())

    # 只選已同意的 repo 的快照：可以提問
    assert _ask(env, snapshot_ids=[env.repos[0].snapshot_id]).status_code == 202


def test_qa_questions_unknown_snapshot_is_404_and_unfinished_is_409(
    env: Env, db_engine: Engine
) -> None:
    missing = _ask(env, snapshot_ids=[env.repos[0].snapshot_id, 999_999])
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "snapshot_not_found"
    assert missing.json()["error"]["details"] == {"snapshot_id": 999_999}

    importing = add_snapshot(db_engine, env.repos[0].repository_id, status="importing", index=9)
    unfinished = _ask(env, snapshot_ids=[importing])
    assert unfinished.status_code == 409
    assert unfinished.json()["error"]["code"] == "snapshot_not_completed"
    assert unfinished.json()["error"]["details"] == {"snapshot_id": importing}
    assert _count(db_engine, "qa_questions") == 0


def test_qa_questions_snapshot_without_retrieval_index_is_409(env: Env, db_engine: Engine) -> None:
    unindexed = add_snapshot(db_engine, env.repos[0].repository_id, index=8)

    response = _ask(env, snapshot_ids=[unindexed])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "retrieval_index_not_ready"
    assert response.json()["error"]["details"]["snapshot_id"] == unindexed
    assert _count(db_engine, "qa_questions") == 0


def test_qa_questions_validation_runs_snapshots_first_then_key_then_consent(env: Env) -> None:
    env.client.delete("/api/v1/qa/provider-key")
    env.client.delete(f"/api/v1/repositories/{env.repos[0].repository_id}/qa-consent")

    assert _ask(env, snapshot_ids=[999_999]).json()["error"]["code"] == "snapshot_not_found"
    assert _ask(env).json()["error"]["code"] == "qa_provider_not_configured"
    env.client.put("/api/v1/qa/provider-key", json={"api_key": FAKE_API_KEY})
    assert _ask(env).json()["error"]["code"] == "qa_egress_not_consented"


def test_qa_questions_stored_key_that_cannot_be_decrypted_counts_as_not_configured(
    env: Env,
) -> None:
    other = env.settings.model_copy(
        update={"token_encryption_key": SecretStr(Fernet.generate_key().decode())}
    )
    with TestClient(create_app(other)) as client:
        response = client.post(
            QUESTIONS_URL, json={"snapshot_ids": env.snapshot_ids, "question": QUESTION}
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "qa_provider_not_configured"


@pytest.mark.parametrize(
    "body",
    [
        {"question": ""},
        {"question": "?!"},
        {"snapshot_ids": []},
        {"snapshot_ids": [1, 1]},
        {"max_tokens": 0},
        {"file_kinds": ["source"]},
    ],
)
def test_qa_questions_invalid_request_is_422(env: Env, body: dict[str, Any]) -> None:
    response = _ask(env, **body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert _count(env.engine, "qa_questions") == 0


# --- 粗略額度預檢（提問時）與精確預留（worker） ---


def test_qa_questions_per_request_limit_below_max_tokens_is_rejected_without_ledger_events(
    env: Env,
) -> None:
    env.client.put("/api/v1/qa/settings", json={"per_request_tokens": 1000})
    jobs_before = _jobs_now(env)

    response = _ask(env, max_tokens=2000)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "token_budget_exceeded"
    assert error["details"]["reason"] == "per_request_tokens"
    _assert_nothing_happened(env, jobs_before)


def test_qa_questions_exhausted_daily_budget_is_rejected_at_submit(env: Env) -> None:
    env.client.put("/api/v1/qa/settings", json={"daily_tokens": 100_000})
    with Session(env.engine) as session:
        usage_ledger.reserve(
            session,
            estimated_input_tokens=50_000,
            max_tokens=50_000,
            price=DEFAULT_PRICES[HAIKU],
            limits=UsageLimits(daily_tokens=100_000),
            now=datetime.now(UTC),
            tz=ZoneInfo("Asia/Taipei"),
        )
    events_before = len(_ledger(env.engine))

    response = _ask(env)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "token_budget_exceeded"
    assert response.json()["error"]["details"]["reason"] == "daily_tokens"
    assert len(_ledger(env.engine)) == events_before
    assert _count(env.engine, "qa_questions") == 0
    assert env.provider.requests == []


def test_qa_questions_exact_budget_check_in_worker_fails_without_calling_the_provider(
    env: Env,
) -> None:
    # 粗略預檢（上限 ≥ max_tokens）通過，但估算輸入加 max_tokens 超過單次上限
    env.client.put("/api/v1/qa/settings", json={"per_request_tokens": 300})
    submitted = _submit(env, max_tokens=256)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["status"] == "failed"
    assert done["error"]["code"] == "token_budget_exceeded"
    assert done["usage"] == {"input_tokens": None, "output_tokens": None, "usage_unknown": False}
    assert done["cost"]["actual_usd"] is None
    assert len(done["context_chunks"]) == 3  # 追蹤記錄保留已組好的片段
    assert env.provider.requests == [] and env.keys_seen == []
    assert _ledger(env.engine) == []
    job = env.client.get(f"/api/v1/jobs/{submitted['job']['id']}").json()
    assert job["status"] == "failed" and job["error"]["code"] == "token_budget_exceeded"


# --- 結算：實際用量與用量未知 ---


def test_qa_questions_settlement_uses_the_providers_actual_usage(env: Env) -> None:
    env.provider = FakeProvider(
        text=_success_text(), usage=Usage(input_tokens=1234, output_tokens=56)
    )

    done = _ask_and_run(env)

    price = DEFAULT_PRICES[HAIKU]
    actual = token_cost_usd(1234, 56, price)
    assert done["usage"] == {"input_tokens": 1234, "output_tokens": 56, "usage_unknown": False}
    assert Decimal(done["cost"]["actual_usd"]) == actual
    events = _ledger(env.engine)
    assert [e["event_type"] for e in events] == ["reserve", "settle"]
    reserve, settle = events
    assert reserve["reservation_id"] == settle["reservation_id"]
    assert (settle["input_tokens"], settle["output_tokens"], settle["cost_usd"]) == (
        1234,
        56,
        actual,
    )
    assert settle["usage_unknown"] is False
    assert reserve["output_tokens"] == DEFAULT_MAX_TOKENS
    # 預估值是預留值（依真實片段計算），並且大於 0
    assert done["cost"]["estimated_input_tokens"] == reserve["input_tokens"] > 0
    assert Decimal(done["cost"]["estimated_usd"]) == reserve["cost_usd"]
    today = env.client.get("/api/v1/qa/usage").json()["today"]
    assert (today["input_tokens"], today["output_tokens"]) == (1234, 56)


def test_qa_questions_unknown_usage_keeps_the_reserved_amount_and_is_flagged(env: Env) -> None:
    env.provider = FakeProvider(text=_success_text(), usage=Usage.unknown())

    done = _ask_and_run(env)

    assert done["status"] == "succeeded" and done["usage"]["usage_unknown"] is True
    reserve, settle = _ledger(env.engine)
    assert settle["usage_unknown"] is True
    assert done["usage"]["input_tokens"] == reserve["input_tokens"]
    assert done["usage"]["output_tokens"] == reserve["output_tokens"] == DEFAULT_MAX_TOKENS
    assert Decimal(done["cost"]["actual_usd"]) == reserve["cost_usd"]


# --- AC-4：追蹤記錄 ---


def test_qa_questions_trace_has_model_versions_chunks_tokens_cost_duration_and_status(
    env: Env,
) -> None:
    env.provider = FakeProvider(text=_success_text(), usage=Usage(900, 40))
    submitted = _submit(env)
    _run(env, submitted)

    done = _get(env, submitted["id"])

    assert done["question"] == QUESTION  # D8：保存全文
    assert done["snapshot_ids"] == env.snapshot_ids
    assert (done["provider"], done["model"], done["prompt_version"]) == (
        "anthropic",
        HAIKU,
        PROMPT_VERSION,
    )
    assert done["status"] == "succeeded" and done["job_id"] == submitted["job"]["id"]
    assert done["usage"]["input_tokens"] == 900 and done["usage"]["output_tokens"] == 40
    assert Decimal(done["cost"]["estimated_usd"]) > 0 and Decimal(done["cost"]["actual_usd"]) > 0
    assert isinstance(done["duration_ms"], int) and done["duration_ms"] >= 0
    assert done["created_at"] and done["started_at"] and done["finished_at"]
    chunks = done["context_chunks"]
    assert [c["rank"] for c in chunks] == [1, 2, 3]
    assert [c["code"] for c in chunks] == ["C1", "C2", "C3"]
    for chunk in chunks:
        assert chunk["skipped_reason"] is None and chunk["truncated"] is False
        assert len(chunk["commit_sha"]) == 40 and chunk["path"] == PATH
        assert chunk["start_line"] == chunk["end_line"] == 1
        assert chunk["repository"] in ("alpha", "beta")
    assert {c["snapshot_id"] for c in chunks} == set(env.snapshot_ids)
    assert {c["repository_id"] for c in chunks} == {r.repository_id for r in env.repos}


def test_qa_questions_chunks_that_do_not_fit_are_listed_as_skipped(
    env: Env, db_engine: Engine
) -> None:
    # 上限 20 個片段：一個有 25 個檔案（25 個片段）的快照讓其中 5 個成為 count_cap
    env.provider = FakeProvider(text=_success_text())
    big = add_snapshot(db_engine, env.repos[0].repository_id, index=100)
    files = {f"src/file{n}.py": f"widget handler number {n}\n" for n in range(25)}
    add_files(db_engine, big, files)
    _index_snapshot(db_engine, big, env.settings)
    submitted = _submit(env, snapshot_ids=[big])

    _run(env, submitted)

    chunks = _get(env, submitted["id"])["context_chunks"]
    assert len(chunks) == 25
    sent = [c for c in chunks if c["code"] is not None]
    skipped = [c for c in chunks if c["code"] is None]
    assert len(sent) == 20 and len(skipped) == 5
    assert {c["skipped_reason"] for c in skipped} == {"count_cap"}
    assert [c["rank"] for c in chunks] == list(range(1, 26))
    assert {c["snapshot_id"] for c in chunks} == {big}


# --- AC-5：快照或 repo 之後被移除，追蹤記錄仍可解釋引用 ---


def test_qa_questions_trace_survives_snapshot_and_repository_removal(env: Env) -> None:
    env.provider = FakeProvider(
        text=_answer(_statement("alpha", "fact", ("C1", 1, 1), ("C2", 1, 1), ("C3", 1, 1)))
    )
    submitted = _submit(env)
    _run(env, submitted)
    before = _get(env, submitted["id"])
    assert before["statements"][0]["citations"]

    removed = env.client.delete(f"/api/v1/snapshots/{env.repos[0].snapshot_id}")
    assert removed.status_code == 200, removed.text
    assert _get(env, submitted["id"]) == before

    removed_repository = env.client.delete(f"/api/v1/repositories/{env.repos[1].repository_id}")
    assert removed_repository.status_code == 200, removed_repository.text
    after = _get(env, submitted["id"])
    assert after == before
    assert {c["repository"] for c in after["statements"][0]["citations"]} <= {"alpha", "beta"}
    assert all(len(c["commit_sha"]) == 40 for c in after["statements"][0]["citations"])


# --- AC-13：多個快照的引用各自帶快照與 SHA，不因路徑相同合併 ---


def test_qa_questions_citations_across_snapshots_stay_separate_even_with_the_same_path(
    env: Env,
) -> None:
    env.provider = FakeProvider(
        text=_answer(
            _statement("all three", "fact", ("C1", 1, 1), ("C2", 1, 1), ("C3", 1, 1)),
        )
    )

    done = _ask_and_run(env)

    citations = done["statements"][0]["citations"]
    assert len(citations) == 3
    assert {c["path"] for c in citations} == {PATH}
    assert {c["snapshot_id"] for c in citations} == set(env.snapshot_ids)
    assert {(c["repository"], c["commit_sha"]) for c in citations} == {
        (repo.name, repo.commit_sha) for repo in env.repos
    }
    # 每個引用的座標與它引用的那個片段記錄一致
    by_code = {c["code"]: c for c in done["context_chunks"]}
    for citation in citations:
        chunk = by_code[citation["chunk_code"]]
        assert (citation["snapshot_id"], citation["commit_sha"], citation["repository"]) == (
            chunk["snapshot_id"],
            chunk["commit_sha"],
            chunk["repository"],
        )


# --- 沒有證據 ---


def test_qa_questions_without_sendable_evidence_answers_insufficient_without_a_provider_call(
    env: Env, db_engine: Engine
) -> None:
    empty = add_snapshot(db_engine, env.repos[0].repository_id, index=50)
    add_files(db_engine, empty, {"blank.txt": "   \n\n"})
    _index_snapshot(db_engine, empty, env.settings)

    done = _ask_and_run(env, snapshot_ids=[empty])

    assert done["status"] == "succeeded" and done["answer_status"] == "insufficient_evidence"
    assert done["statements"] == [] and done["usage"]["input_tokens"] == 0
    assert env.provider.requests == [] and _ledger(env.engine) == []


# --- 失敗：預留不外洩、追蹤記錄保留、錯誤碼穩定 ---

_PROVIDER_FAILURES: list[tuple[str, dict[str, Any], str, str, bool]] = [
    # (標籤, FakeProvider 參數, 錯誤碼, 帳本結束事件, 用量是否未知)
    ("timeout", {"behavior": "timeout"}, "provider_timeout", "settle", True),
    ("rate_limit", {"behavior": "rate_limit"}, "provider_rate_limited", "release", False),
    ("malformed", {"behavior": "malformed"}, "provider_malformed_response", "settle", True),
    (
        "max_tokens",
        {"behavior": "max_tokens", "usage": Usage(500, DEFAULT_MAX_TOKENS)},
        "provider_max_tokens",
        "settle",
        False,
    ),
    (
        "refusal",
        {"behavior": "refusal", "usage": Usage(500, 5)},
        "provider_refusal",
        "settle",
        False,
    ),
    (
        "context_window",
        {"behavior": "context_window_exceeded", "usage": Usage(500, 0)},
        "provider_context_window_exceeded",
        "settle",
        False,
    ),
    (
        "not_the_contract",
        {"text": '{"unexpected": true}', "usage": Usage(700, 20)},
        "answer_unexpected_fields",
        "settle",
        False,
    ),
    (
        "invalid_field",
        {
            "text": json.dumps(
                {
                    "status": "answered",
                    "statements": [{"text": ANSWER_MARKER, "kind": "bogus", "citations": []}],
                }
            ),
            "usage": Usage(700, 20),
        },
        "answer_invalid_field",
        "settle",
        False,
    ),
]


@pytest.mark.parametrize(
    ("kwargs", "code", "final_event", "unknown"),
    [case[1:] for case in _PROVIDER_FAILURES],
    ids=[case[0] for case in _PROVIDER_FAILURES],
)
def test_qa_questions_provider_failures_close_the_reservation_and_keep_the_trace(
    env: Env, kwargs: dict[str, Any], code: str, final_event: str, unknown: bool
) -> None:
    env.provider = FakeProvider(**kwargs)
    submitted = _submit(env)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["status"] == "failed" and done["answer_status"] is None
    assert done["error"]["code"] == code and done["statements"] == []
    assert len(done["context_chunks"]) == 3  # 追蹤記錄保留
    # 暫時性錯誤（逾時、限流）依 #6 重試到上限，其餘只嘗試一次；每次嘗試各有一個預留
    attempts = 3 if code in ("provider_timeout", "provider_rate_limited") else 1
    assert done["attempts"] == attempts == env.provider.call_count
    events = _ledger(env.engine)
    assert [e["event_type"] for e in events] == ["reserve", final_event] * attempts
    _assert_no_open_reservations(env.engine)
    finals = events[1::2]
    assert all(e["usage_unknown"] is unknown for e in finals)
    assert done["usage"]["usage_unknown"] is unknown
    assert done["usage"]["input_tokens"] == sum(e["input_tokens"] for e in finals)
    assert done["usage"]["output_tokens"] == sum(e["output_tokens"] for e in finals)
    if final_event == "release":
        assert done["usage"]["input_tokens"] == 0 and Decimal(done["cost"]["actual_usd"]) == 0
    job = env.client.get(f"/api/v1/jobs/{submitted['job']['id']}").json()
    assert job["status"] == "failed" and job["error"]["code"] == code
    assert job["attempt"] == 1  # 問答自己重試；job 層不會再重跑


def test_qa_questions_unexpected_error_after_the_call_settles_conservatively(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.provider = FakeProvider(text=_success_text(), usage=Usage(321, 12))

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError(f"explodes with {QUESTION_MARKER} {CHUNK_MARKER}")

    monkeypatch.setattr(qa_question_runner, "verify_answer", boom)
    submitted = _submit(env)

    with capture_logs() as logs:
        assert _run(env, submitted) == "failed"
        captured = logs.text()

    done = _get(env, submitted["id"])
    assert done["error"]["code"] == "internal_error"
    assert not any(marker in json.dumps(done["error"]) for marker in ALL_MARKERS)
    assert not any(marker in captured for marker in ALL_MARKERS)  # 例外訊息與堆疊都不進日誌
    # 呼叫已發生且有實際用量：以實際用量結算
    assert done["usage"] == {"input_tokens": 321, "output_tokens": 12, "usage_unknown": False}
    _assert_no_open_reservations(env.engine)


def test_qa_questions_failure_before_the_call_releases_the_reservation(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(api_key: str) -> QAProvider:
        raise RuntimeError("cannot build")

    monkeypatch.setattr(qa_question_runner, "build_provider", refuse)
    submitted = _submit(env)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["error"]["code"] == "internal_error"
    assert [e["event_type"] for e in _ledger(env.engine)] == ["reserve", "release"]
    assert done["usage"] == {"input_tokens": 0, "output_tokens": 0, "usage_unknown": False}
    _assert_no_open_reservations(env.engine)


@pytest.mark.parametrize("change", ["revoke_consent", "clear_key", "clear_model"])
def test_qa_questions_worker_revalidates_settings_that_changed_while_queued(
    env: Env, change: str
) -> None:
    submitted = _submit(env)
    if change == "revoke_consent":
        env.client.delete(f"/api/v1/repositories/{env.repos[0].repository_id}/qa-consent")
        expected = "qa_egress_not_consented"
    elif change == "clear_key":
        env.client.delete("/api/v1/qa/provider-key")
        expected = "qa_provider_not_configured"
    else:
        env.client.put("/api/v1/qa/settings", json={"model": None})
        expected = "qa_model_not_selected"

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["error"]["code"] == expected
    assert env.provider.requests == [] and env.keys_seen == []
    assert _ledger(env.engine) == []


def test_qa_questions_rerun_of_an_interrupted_question_settles_its_reservation(
    env: Env,
) -> None:
    submitted = _submit(env)
    reservation_id = uuid4()
    with Session(env.engine) as session:
        usage_ledger.reserve(
            session,
            estimated_input_tokens=1000,
            max_tokens=500,
            price=DEFAULT_PRICES[HAIKU],
            limits=UsageLimits(),
            now=datetime.now(UTC),
            tz=ZoneInfo("Asia/Taipei"),
            reservation_id=reservation_id,
        )
    with env.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE qa_questions SET status = 'running', started_at = now(), "
                "reservation_id = :rid WHERE id = :id"
            ),
            {"rid": reservation_id, "id": submitted["id"]},
        )

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["status"] == "failed" and done["error"]["code"] == "qa_interrupted"
    assert done["usage"] == {"input_tokens": 1000, "output_tokens": 500, "usage_unknown": True}
    assert [e["event_type"] for e in _ledger(env.engine)] == ["reserve", "settle"]
    assert env.provider.requests == []


def test_qa_questions_job_rerun_of_a_finished_question_does_not_run_it_again(env: Env) -> None:
    env.provider = FakeProvider(text=_success_text())
    submitted = _submit(env)
    _run(env, submitted)
    done = _get(env, submitted["id"])
    with env.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status = 'queued', lease_token = NULL, lease_expires_at = NULL, "
                "finished_at = NULL WHERE id = :id"
            ),
            {"id": submitted["job"]["id"]},
        )

    assert _run(env, submitted) == "succeeded"

    assert len(env.provider.requests) == 1
    assert _get(env, submitted["id"]) == done
    assert len(_ledger(env.engine)) == 2


# --- 日誌與錯誤訊息不含問題、片段或回答 ---


@pytest.mark.parametrize(
    "provider_kwargs",
    [
        {"text": _success_text()},
        {"behavior": "timeout"},
        {"text": json.dumps({"status": "answered", "statements": [{"text": ANSWER_MARKER}]})},
    ],
    ids=["success", "provider_timeout", "invalid_answer"],
)
def test_qa_questions_logs_and_errors_never_contain_question_chunk_or_answer_text(
    env: Env, provider_kwargs: dict[str, Any]
) -> None:
    env.provider = FakeProvider(**provider_kwargs)
    with capture_logs() as logs:
        submitted = _submit(env)
        _run(env, submitted)
        done = _get(env, submitted["id"])
        env.client.get(QUESTIONS_URL)
        env.client.delete(f"{QUESTIONS_URL}/{submitted['id']}")
        env.client.get(f"{QUESTIONS_URL}/{submitted['id']}")
        captured = logs.text()

    assert len(captured) > 0
    for marker in ALL_MARKERS:
        assert marker not in captured
    assert FAKE_API_KEY not in captured
    error = done["error"]
    if error is not None:
        assert not any(marker in json.dumps(error) for marker in ALL_MARKERS)
    with env.engine.connect() as connection:
        job_row = connection.execute(
            text("SELECT params, result, error_summary FROM jobs WHERE kind = 'qa_question'")
        ).one()
    assert not any(marker in json.dumps(list(job_row), default=str) for marker in ALL_MARKERS)


# --- 查詢、刪除（D8） ---


def test_qa_questions_get_unknown_id_is_404(env: Env) -> None:
    response = env.client.get(f"{QUESTIONS_URL}/12345")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "qa_question_not_found"


def test_qa_questions_list_is_paged_newest_first_and_filterable_by_status(env: Env) -> None:
    env.provider = FakeProvider(text=_success_text())
    ids = []
    for _ in range(3):
        submitted = _submit(env)
        ids.append(submitted["id"])
    _run(env, {"job": {"id": _job_of(env, ids[0])}})
    queued_only = env.client.get(QUESTIONS_URL, params={"status": "queued"}).json()
    assert [item["id"] for item in queued_only["items"]] == [ids[2], ids[1]]
    assert queued_only["total"] == 2

    everything = env.client.get(QUESTIONS_URL).json()
    assert [item["id"] for item in everything["items"]] == [ids[2], ids[1], ids[0]]
    assert everything["total"] == 3 and everything["limit"] == 50 and everything["offset"] == 0
    first = everything["items"][2]
    assert first["status"] == "succeeded" and first["answer_status"] == "answered"
    assert first["question"] == QUESTION and first["model"] == HAIKU
    assert "statements" not in first and "context_chunks" not in first

    page = env.client.get(QUESTIONS_URL, params={"limit": 1, "offset": 1}).json()
    assert [item["id"] for item in page["items"]] == [ids[1]] and page["total"] == 3
    assert env.client.get(QUESTIONS_URL, params={"status": "bogus"}).status_code == 422


def _job_of(env: Env, question_id: int) -> int:
    with env.engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT job_id FROM qa_questions WHERE id = :id"), {"id": question_id}
            )
            or 0
        )


def test_qa_questions_delete_one_removes_the_whole_trace_but_not_the_ledger(env: Env) -> None:
    env.provider = FakeProvider(text=_success_text())
    keep = _ask_and_run(env)
    gone = _ask_and_run(env)
    ledger_before = len(_ledger(env.engine))

    response = env.client.delete(f"{QUESTIONS_URL}/{gone['id']}")

    assert response.status_code == 204 and response.content == b""
    assert env.client.get(f"{QUESTIONS_URL}/{gone['id']}").status_code == 404
    assert _get(env, keep["id"]) == keep
    for table in ("qa_context_chunks", "qa_statements", "qa_citations"):
        assert _count(env.engine, table) > 0
    with env.engine.connect() as connection:
        leftover = connection.scalar(
            text("SELECT count(*) FROM qa_context_chunks WHERE question_id = :id"),
            {"id": gone["id"]},
        )
    assert leftover == 0
    assert len(_ledger(env.engine)) == ledger_before  # 用量是事實，不隨問答刪除
    assert env.client.delete(f"{QUESTIONS_URL}/{gone['id']}").status_code == 404


@pytest.mark.parametrize("active_status", ["queued", "running"])
def test_qa_questions_delete_refuses_a_queued_or_running_question(
    env: Env, active_status: str
) -> None:
    submitted = _submit(env)
    if active_status == "running":
        with env.engine.begin() as connection:
            connection.execute(
                text("UPDATE qa_questions SET status = 'running', started_at = now()")
            )

    response = env.client.delete(f"{QUESTIONS_URL}/{submitted['id']}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "qa_question_active"
    assert _get(env, submitted["id"])["status"] == active_status


def test_qa_questions_delete_all_removes_finished_ones_and_keeps_active_ones(env: Env) -> None:
    env.provider = FakeProvider(text=_success_text())
    _ask_and_run(env)
    _ask_and_run(env)
    active = _submit(env)

    response = env.client.delete(QUESTIONS_URL)

    assert response.status_code == 200
    assert response.json() == {"deleted": 2, "skipped_active": 1}
    listing = env.client.get(QUESTIONS_URL).json()
    assert [item["id"] for item in listing["items"]] == [active["id"]]
    assert env.client.delete(QUESTIONS_URL).json() == {"deleted": 0, "skipped_active": 1}


# --- migration 0021 ---


def _version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _insert_question(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO qa_questions (question, snapshot_ids, provider, model, "
                "prompt_version, max_tokens, estimated_input_tokens, estimated_cost_usd, "
                "created_at) VALUES ('q', ARRAY[1], 'anthropic', 'm', 'v1', 10, 0, 0, now())"
            )
        )


def test_qa_questions_models_and_migrations_have_no_drift(
    db_engine: Engine, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    assert _tables(db_engine) >= QA_TABLES

    command.check(config)


def test_qa_questions_migration_downgrade_refuses_while_questions_exist(
    db_engine: Engine, test_database_url: str
) -> None:
    _insert_question(db_engine)

    with pytest.raises(DBAPIError, match="Cannot downgrade|check constraint"):
        command.downgrade(alembic_config(test_database_url), "0020")

    # 降版應因約束檢查失敗而停止，版本應不低於 0022
    version = _version(db_engine)
    assert int(version) >= 22 and _tables(db_engine) >= QA_TABLES
    assert _count(db_engine, "qa_questions") == 1


def test_qa_questions_migration_downgrade_refuses_while_question_jobs_exist(
    db_engine: Engine, test_database_url: str
) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (kind, dedupe_key, max_attempts) "
                "VALUES ('qa_question', 'qa_question:1', 3)"
            )
        )

    with pytest.raises(DBAPIError, match="Cannot downgrade|check constraint"):
        command.downgrade(alembic_config(test_database_url), "0020")

    # 降版應因約束檢查或明確錯誤而停止
    version = _version(db_engine)
    assert int(version) >= 22
    assert _count(db_engine, "jobs") == 1


def test_qa_questions_migration_round_trip_when_empty(
    db_engine: Engine, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "0020")

        assert _version(db_engine) == "0020"
        assert _tables(db_engine).isdisjoint(QA_TABLES)
        with pytest.raises(IntegrityError), db_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO jobs (kind, dedupe_key, max_attempts) "
                    "VALUES ('qa_question', 'x', 3)"
                )
            )

        command.upgrade(config, "head")

        assert _tables(db_engine) >= QA_TABLES
        command.check(config)
    finally:
        command.upgrade(config, "head")


def test_qa_questions_job_kind_and_phases_are_accepted_by_the_jobs_table(
    db_engine: Engine,
) -> None:
    with db_engine.begin() as connection:
        for phase in ("pending", "retrieving", "generating", "completed"):
            connection.execute(
                text(
                    "INSERT INTO jobs (kind, phase, dedupe_key, max_attempts, status, "
                    "finished_at) VALUES ('qa_question', :phase, :key, 3, 'succeeded', now())"
                ),
                {"phase": phase, "key": f"qa_question:{phase}"},
            )


def test_qa_questions_database_rejects_inconsistent_rows(db_engine: Engine) -> None:
    _insert_question(db_engine)
    bad_updates: list[Callable[[], None]] = []

    def update(sql: str) -> Callable[[], None]:
        def run() -> None:
            with db_engine.begin() as connection:
                connection.execute(text(sql))

        return run

    bad_updates.append(update("UPDATE qa_questions SET status = 'succeeded'"))  # 沒有 answer
    bad_updates.append(update("UPDATE qa_questions SET status = 'failed', finished_at = now()"))
    bad_updates.append(update("UPDATE qa_questions SET answer_status = 'answered'"))
    bad_updates.append(update("UPDATE qa_questions SET snapshot_ids = '{}'"))
    bad_updates.append(update("UPDATE qa_questions SET input_tokens = -1"))
    for bad in bad_updates:
        with pytest.raises(IntegrityError):
            bad()


# --- 4-5 評估腳本的資料庫後端 ---


@pytest.fixture
def eval_backend(env: Env) -> Iterator[Any]:
    with Session(env.engine, expire_on_commit=False) as session:
        yield DbEvalBackend(
            session, env.settings, env.snapshot_ids, lambda query: [1.0] + [0.0] * 767
        )


def test_eval_backend_reports_repositories_without_consent(env: Env, eval_backend: Any) -> None:
    backend = eval_backend
    assert backend.unconsented_repositories() == []

    with env.engine.begin() as connection:
        connection.execute(
            text("DELETE FROM repository_qa_consent WHERE repository_id = :id"),
            {"id": env.repos[1].repository_id},
        )

    assert backend.unconsented_repositories() == ["beta"]


def test_eval_backend_retrieval_matches_the_question_worker(env: Env, eval_backend: Any) -> None:
    context = eval_backend.retrieve(QUESTION)

    with Session(env.engine, expire_on_commit=False) as session:
        expected = qa_question_runner.retrieve_context(
            session, env.settings, env.snapshot_ids, QUESTION
        ).context
    assert context.included
    assert context.data_block == expected.data_block
    assert CHUNK_MARKER in context.data_block


def test_eval_dataset_repositories_resolve_to_latest_completed_snapshots(env: Env) -> None:
    def dataset(**scope: object) -> Any:
        item = {"id": "u", "question": "q?", "answerable": False, "expected": []}
        return load_dataset(json.dumps({"dataset_version": "v", "items": [item], **scope}))

    with Session(env.engine) as session:
        assert resolve_snapshot_ids(session, dataset(repositories=["alpha", "beta"])) == [
            env.repos[2].snapshot_id,
            env.repos[1].snapshot_id,
        ]
        assert resolve_snapshot_ids(session, dataset(snapshot_ids=env.snapshot_ids)) == (
            env.snapshot_ids
        )
        with pytest.raises(EvalConfigError):
            resolve_snapshot_ids(session, dataset(repositories=["missing"]))
        with pytest.raises(EvalConfigError):
            resolve_snapshot_ids(session, dataset(snapshot_ids=[999_999]))
