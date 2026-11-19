"""問答失敗與取消路徑（4-3b）：錯誤碼、有上限的重試、取消、額度帳本一致性與 migration 0022。

全程使用假供應商（`FakeProvider` 的 `script` 可排出 `[rate_limit, rate_limit, success]`，
`call_count` 可觀察呼叫次數）；退避睡眠被換成會記錄秒數的函式，測試不真的等待。
"""

import json
import threading
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from repopilot.errors import JobNotCancellableError
from repopilot.qa.errors import ProviderRateLimitedError, ProviderUnavailableError
from repopilot.qa.failures import MAX_ATTEMPTS, SLEEP_SLICE_SECONDS
from repopilot.qa.fake_provider import FakeBehavior, FakeProvider
from repopilot.qa.provider import GenerateResponse, GenerationOutcome, Usage
from repopilot.services import jobs, qa_question_runner
from tests.github_helpers import capture_logs
from tests.integration import test_qa_questions as base
from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import wait_until_a_query_is_blocked
from tests.integration.test_qa_questions import (
    ALL_MARKERS,
    ANSWER_MARKER,
    CHUNK_MARKER,
    FAKE_API_KEY,
    HAIKU,
    QUESTION_MARKER,
    QUESTIONS_URL,
    Env,
    _ask_and_run,
    _count,
    _get,
    _insert_question,
    _ledger,
    _run,
    _submit,
    _success_text,
    _version,
)

# 共用 fixture 以賦值方式帶入（不是 import，避免參數遮蔽匯入名稱的 lint 誤報）
clean_qa_state = base.clean_qa_state
no_real_provider = base.no_real_provider
qa_settings = base.qa_settings
env = base.env

# --- 輔助 ---


@pytest.fixture
def sleeps(env: Env, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """記錄退避睡眠（每一段都是小於等於 `SLEEP_SLICE_SECONDS` 的片段）；不真的睡。"""
    recorded: list[float] = []
    monkeypatch.setattr(qa_question_runner, "sleep", recorded.append)
    return recorded


def assert_ledger_consistent(engine: Engine, question: dict[str, Any] | None = None) -> None:
    """所有問答都已結束時：每個預留恰好有一個 reserve 與一個 settle／release（沒有懸空、沒有重複）；
    給了問答就再核對它的累計用量等於其預留的結束事件加總。"""
    with engine.connect() as connection:
        active = connection.scalar(
            text("SELECT count(*) FROM qa_questions WHERE status IN ('queued', 'running')")
        )
    assert active == 0
    by_reservation: dict[str, list[dict[str, Any]]] = {}
    for event in _ledger(engine):
        if event["event_type"] in ("reserve", "settle", "release"):
            by_reservation.setdefault(str(event["reservation_id"]), []).append(event)
    finals: list[dict[str, Any]] = []
    for reservation_id, events in by_reservation.items():
        kinds = [event["event_type"] for event in events]
        assert kinds[0] == "reserve" and len(kinds) == 2, (reservation_id, kinds)
        assert kinds[1] in ("settle", "release"), (reservation_id, kinds)
        finals.append(events[1])
    if question is not None and finals:
        usage = question["usage"]
        assert usage["input_tokens"] == sum(e["input_tokens"] for e in finals)
        assert usage["output_tokens"] == sum(e["output_tokens"] for e in finals)
        assert Decimal(question["cost"]["actual_usd"]) == sum(
            (e["cost_usd"] for e in finals), Decimal(0)
        )


def _event_types(engine: Engine) -> list[str]:
    return [event["event_type"] for event in _ledger(engine)]


def _cancel(env: Env, submitted: dict[str, Any]) -> int:
    response = env.client.post(f"/api/v1/jobs/{submitted['job']['id']}/cancel")
    assert response.status_code in (200, 202), response.text
    return int(response.status_code)


def _cancel_during_call(env: Env, submitted: dict[str, Any]) -> Callable[[int], None]:
    """給 `FakeProvider.on_call`：供應商呼叫進行中，使用者按下取消。"""

    def hook(call_number: int) -> None:
        _cancel(env, submitted)

    return hook


def _job(env: Env, submitted: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = env.client.get(f"/api/v1/jobs/{submitted['job']['id']}").json()
    return body


# --- AC-1：max_tokens、拒答、超出上下文：失敗並計入用量（含思考） ---


def test_qa_failures_max_tokens_fails_and_counts_thinking_tokens(env: Env) -> None:
    env.provider = FakeProvider(
        script=[
            GenerateResponse(
                model=HAIKU,
                outcome=GenerationOutcome.MAX_TOKENS,
                text="",
                usage=Usage(input_tokens=500, output_tokens=4096, thinking_tokens=3000),
            )
        ]
    )

    done = _ask_and_run(env)

    assert done["status"] == "failed" and done["error"]["code"] == "provider_max_tokens"
    assert done["usage"] == {"input_tokens": 500, "output_tokens": 4096, "usage_unknown": False}
    assert done["attempts"] == 1 == env.provider.call_count  # 不重試
    assert _event_types(env.engine) == ["reserve", "settle"]
    assert_ledger_consistent(env.engine, done)


@pytest.mark.parametrize(
    ("behavior", "code"),
    [
        ("refusal", "provider_refusal"),
        ("context_window_exceeded", "provider_context_window_exceeded"),
    ],
)
def test_qa_failures_refusal_and_context_window_fail_with_usage_and_no_retry(
    env: Env, behavior: FakeBehavior, code: str
) -> None:
    env.provider = FakeProvider(behavior, usage=Usage(input_tokens=700, output_tokens=9))

    done = _ask_and_run(env)

    assert done["status"] == "failed" and done["error"]["code"] == code
    assert done["usage"] == {"input_tokens": 700, "output_tokens": 9, "usage_unknown": False}
    assert done["attempts"] == 1 == env.provider.call_count
    assert_ledger_consistent(env.engine, done)


# --- AC-2：每種供應商錯誤有明確錯誤碼；不可重試者只呼叫一次 ---

_NO_RETRY: list[tuple[FakeBehavior, str, str, bool]] = [
    # (行為, 錯誤碼, 帳本結束事件, 用量是否未知)
    ("payment_required", "provider_payment_required", "release", False),
    ("request_too_large", "provider_request_too_large", "release", False),
    ("auth_failed", "provider_auth_failed", "release", False),
    ("spend_limit", "provider_spend_limit_reached", "release", False),
    ("malformed", "provider_malformed_response", "settle", True),
]


@pytest.mark.parametrize(
    ("behavior", "code", "final_event", "unknown"),
    _NO_RETRY,
    ids=[case[0] for case in _NO_RETRY],
)
def test_qa_failures_permanent_provider_errors_are_not_retried(
    env: Env,
    sleeps: list[float],
    behavior: FakeBehavior,
    code: str,
    final_event: str,
    unknown: bool,
) -> None:
    env.provider = FakeProvider(behavior)
    submitted = _submit(env)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["status"] == "failed" and done["error"]["code"] == code
    assert done["error"]["message"]  # 固定文字，不是供應商原文
    assert done["attempts"] == 1 == env.provider.call_count and sleeps == []
    assert _event_types(env.engine) == ["reserve", final_event]
    assert done["usage"]["usage_unknown"] is unknown
    assert_ledger_consistent(env.engine, done)
    job = _job(env, submitted)
    assert job["status"] == "failed" and job["error"]["code"] == code and job["attempt"] == 1


def test_qa_failures_transient_errors_are_retried_with_backoff_until_success(
    env: Env, sleeps: list[float]
) -> None:
    env.provider = FakeProvider(
        script=["rate_limit", "rate_limit", "success"],
        text=_success_text(),
        usage=Usage(input_tokens=1234, output_tokens=56),
    )

    done = _ask_and_run(env)

    assert done["status"] == "succeeded" and done["answer_status"] == "answered"
    assert done["attempts"] == 3 == env.provider.call_count
    assert sum(sleeps) == 2.0 + 4.0 and max(sleeps) <= SLEEP_SLICE_SECONDS
    # 限流沒有處理請求：失敗的嘗試釋放預留、不計用量；用量只有成功的那一次
    assert _event_types(env.engine) == [
        "reserve",
        "release",
        "reserve",
        "release",
        "reserve",
        "settle",
    ]
    assert done["usage"] == {"input_tokens": 1234, "output_tokens": 56, "usage_unknown": False}
    assert_ledger_consistent(env.engine, done)


@pytest.mark.parametrize(
    ("behavior", "code", "final_event"),
    [
        ("timeout", "provider_timeout", "settle"),
        ("rate_limit", "provider_rate_limited", "release"),
        ("unavailable", "provider_unavailable", "settle"),
    ],
)
def test_qa_failures_transient_errors_stop_at_the_attempt_cap(
    env: Env, sleeps: list[float], behavior: FakeBehavior, code: str, final_event: str
) -> None:
    env.provider = FakeProvider(behavior)
    submitted = _submit(env)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["error"]["code"] == code
    assert done["attempts"] == MAX_ATTEMPTS == env.provider.call_count
    # 兩次重試：2 秒＋4 秒（以 0.5 秒為單位切片）；最後一次失敗後不再等待
    assert sleeps == [SLEEP_SLICE_SECONDS] * 12
    assert _event_types(env.engine) == ["reserve", final_event] * MAX_ATTEMPTS
    assert_ledger_consistent(env.engine, done)
    job = _job(env, submitted)
    assert job["status"] == "failed" and job["attempt"] == 1  # job 層不重跑，避免重複計費


def test_qa_failures_failed_attempts_that_may_have_been_billed_are_counted_cumulatively(
    env: Env, sleeps: list[float]
) -> None:
    env.provider = FakeProvider(
        script=["timeout", "success"],
        text=_success_text(),
        usage=Usage(input_tokens=1000, output_tokens=40),
    )

    done = _ask_and_run(env)

    assert done["status"] == "succeeded" and done["attempts"] == 2
    events = _ledger(env.engine)
    assert [e["event_type"] for e in events] == ["reserve", "settle", "reserve", "settle"]
    assert events[1]["usage_unknown"] is True  # 逾時：不知道用量，沿用預留量
    assert (events[1]["input_tokens"], events[1]["output_tokens"]) == (
        events[0]["input_tokens"],
        events[0]["output_tokens"],
    )
    assert done["usage"]["input_tokens"] == events[0]["input_tokens"] + 1000
    assert done["usage"]["output_tokens"] == events[0]["output_tokens"] + 40
    assert done["usage"]["usage_unknown"] is True
    assert_ledger_consistent(env.engine, done)
    assert env.client.get("/api/v1/qa/usage").json()["today"]["output_tokens"] == (
        events[0]["output_tokens"] + 40
    )


def test_qa_failures_every_retry_rechecks_the_quota_and_never_masks_a_budget_error(
    env: Env, sleeps: list[float]
) -> None:
    def lower_the_daily_limit(call_number: int) -> None:
        reserved = _ledger(env.engine)[0]
        limit = reserved["input_tokens"] + reserved["output_tokens"] + 1
        response = env.client.put("/api/v1/qa/settings", json={"daily_tokens": limit})
        assert response.status_code == 200

    env.provider = FakeProvider("timeout", on_call=lower_the_daily_limit)
    submitted = _submit(env)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["error"]["code"] == "token_budget_exceeded"
    assert done["attempts"] == 1 and env.provider.call_count == 1  # 第二次沒有呼叫供應商
    assert _event_types(env.engine) == ["reserve", "settle"]  # 被拒絕的預留不寫事件
    assert_ledger_consistent(env.engine, done)
    job = _job(env, submitted)
    assert job["status"] == "failed" and job["error"]["code"] == "token_budget_exceeded"


def test_qa_failures_retry_revalidates_consent_before_sending_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def revoke_during_backoff(seconds: float) -> None:
        url = f"/api/v1/repositories/{env.repos[0].repository_id}/qa-consent"
        assert env.client.delete(url).status_code in (200, 204)

    monkeypatch.setattr(qa_question_runner, "sleep", revoke_during_backoff)
    env.provider = FakeProvider("timeout")
    submitted = _submit(env)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["error"]["code"] == "qa_egress_not_consented"
    assert env.provider.call_count == 1
    assert_ledger_consistent(env.engine, done)


# --- retry-after ---


@pytest.mark.parametrize("retry_after", [0.0, 1.0, 7.0, 20.0])
def test_qa_failures_retry_after_is_honoured_up_to_the_cap(
    env: Env, sleeps: list[float], retry_after: float
) -> None:
    error = ProviderRateLimitedError("limited", status_code=429, retry_after_seconds=retry_after)
    env.provider = FakeProvider(script=[error, "success"], text=_success_text())

    done = _ask_and_run(env)

    assert done["status"] == "succeeded" and env.provider.call_count == 2
    assert sum(sleeps) == retry_after  # 不是 2 秒的預設退避


@pytest.mark.parametrize(
    "error",
    [
        ProviderRateLimitedError("limited", status_code=429, retry_after_seconds=20.5),
        ProviderUnavailableError("busy", status_code=503, retry_after_seconds=3600.0),
    ],
    ids=["rate_limited", "unavailable"],
)
def test_qa_failures_retry_after_beyond_the_cap_fails_without_waiting_or_retrying(
    env: Env, sleeps: list[float], error: Any
) -> None:
    env.provider = FakeProvider(script=[error, "success"], text=_success_text())

    done = _ask_and_run(env)

    assert done["status"] == "failed" and done["error"]["code"] == error.code
    assert env.provider.call_count == 1 and sleeps == []
    assert_ledger_consistent(env.engine, done)


# --- AC-5：供應商端的花費上限 ---


def test_qa_failures_spend_limit_is_never_retried_and_says_it_is_the_providers_limit(
    env: Env, sleeps: list[float]
) -> None:
    env.provider = FakeProvider("spend_limit")
    submitted = _submit(env)

    assert _run(env, submitted) == "failed"

    done = _get(env, submitted["id"])
    assert done["error"]["code"] == "provider_spend_limit_reached"
    message = done["error"]["message"]
    assert "PROVIDER" in message and "not RepoPilot's token budget" in message
    assert env.provider.call_count == 1 and sleeps == []
    assert done["usage"] == {"input_tokens": 0, "output_tokens": 0, "usage_unknown": False}
    assert_ledger_consistent(env.engine, done)


# --- AC-3：失敗也保留追蹤記錄與固定訊息 ---


def test_qa_failures_failed_question_keeps_its_trace_and_exposes_attempts_in_the_list(
    env: Env, sleeps: list[float]
) -> None:
    env.provider = FakeProvider("unavailable")

    done = _ask_and_run(env)

    assert done["status"] == "failed" and done["answer_status"] is None
    assert len(done["context_chunks"]) == 3 and done["statements"] == []
    assert done["started_at"] and done["finished_at"] and done["duration_ms"] is not None
    assert set(done["error"]) == {"code", "message"}
    listed = env.client.get(f"{QUESTIONS_URL}?status=failed").json()["items"]
    assert [item["attempts"] for item in listed] == [MAX_ATTEMPTS]


def test_qa_failures_error_messages_are_fixed_text_not_provider_or_user_content(
    env: Env, sleeps: list[float]
) -> None:
    leaky = ProviderRateLimitedError(
        f"{QUESTION_MARKER} {CHUNK_MARKER} {ANSWER_MARKER} {FAKE_API_KEY}", status_code=429
    )
    env.provider = FakeProvider(script=[leaky])
    submitted = _submit(env)

    with capture_logs() as logs:
        _run(env, submitted)
        captured = logs.text()

    done = _get(env, submitted["id"])
    job_error = json.dumps(_job(env, submitted)["error"])
    shown = json.dumps(done["error"]) + job_error + captured
    assert done["error"]["code"] == "provider_rate_limited"
    for marker in (*ALL_MARKERS, FAKE_API_KEY):
        assert marker not in shown


# --- AC-4：取消 ---


def test_qa_failures_cancel_queued_question_marks_the_question_cancelled_in_the_same_change(
    env: Env,
) -> None:
    submitted = _submit(env)

    assert _cancel(env, submitted) == 200

    done = _get(env, submitted["id"])
    assert done["status"] == "cancelled" and done["error"] is None
    assert done["finished_at"] is not None and done["answer_status"] is None
    assert _job(env, submitted)["status"] == "cancelled"
    assert _run(env, submitted) == "discarded"  # 晚到的訊息拿不到租約
    assert env.provider.call_count == 0 and _ledger(env.engine) == []  # 還沒有預留
    assert _get(env, submitted["id"]) == done
    assert env.client.delete(f"{QUESTIONS_URL}/{submitted['id']}").status_code == 204


def test_qa_failures_cancelling_a_queued_job_does_not_touch_other_questions(env: Env) -> None:
    first, second = _submit(env), _submit(env)

    _cancel(env, first)

    assert _get(env, first["id"])["status"] == "cancelled"
    assert _get(env, second["id"])["status"] == "queued"


def test_qa_failures_cancel_during_a_successful_call_settles_usage_and_saves_no_answer(
    env: Env,
) -> None:
    submitted = _submit(env)
    env.provider = FakeProvider(
        text=_success_text(),
        usage=Usage(input_tokens=1234, output_tokens=56),
        on_call=_cancel_during_call(env, submitted),
    )

    assert _run(env, submitted) == "cancelled"

    done = _get(env, submitted["id"])
    assert done["status"] == "cancelled" and done["error"] is None
    assert done["statements"] == [] and done["answer_status"] is None
    assert len(done["context_chunks"]) == 3  # 追蹤記錄保留
    assert done["usage"] == {"input_tokens": 1234, "output_tokens": 56, "usage_unknown": False}
    assert _job(env, submitted)["status"] == "cancelled"
    assert env.provider.call_count == 1
    assert _event_types(env.engine) == ["reserve", "settle"]
    assert_ledger_consistent(env.engine, done)


@pytest.mark.parametrize(
    ("behavior", "final_event", "unknown"),
    [
        ("rate_limit", "release", False),  # 供應商沒處理：預留全數釋放
        ("timeout", "settle", True),  # 可能已處理：沿用預留量並標記
        ("auth_failed", "release", False),  # 不可重試的失敗：使用者的取消優先
    ],
)
def test_qa_failures_cancel_during_a_failing_call_makes_no_later_call(
    env: Env, sleeps: list[float], behavior: FakeBehavior, final_event: str, unknown: bool
) -> None:
    submitted = _submit(env)
    env.provider = FakeProvider(
        script=[behavior, "success"],
        text=_success_text(),
        on_call=_cancel_during_call(env, submitted),
    )

    assert _run(env, submitted) == "cancelled"

    done = _get(env, submitted["id"])
    assert done["status"] == "cancelled" and done["error"] is None
    assert env.provider.call_count == 1 and done["attempts"] == 1
    assert _event_types(env.engine) == ["reserve", final_event]
    assert done["usage"]["usage_unknown"] is unknown
    assert _job(env, submitted)["status"] == "cancelled"
    assert_ledger_consistent(env.engine, done)


def test_qa_failures_cancel_during_backoff_stops_within_one_slice(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted = _submit(env)
    waited: list[float] = []

    def cancel_on_first_wait(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) == 1:
            _cancel(env, submitted)

    monkeypatch.setattr(qa_question_runner, "sleep", cancel_on_first_wait)
    env.provider = FakeProvider(script=["rate_limit", "success"], text=_success_text())

    assert _run(env, submitted) == "cancelled"

    done = _get(env, submitted["id"])
    assert done["status"] == "cancelled"
    assert waited == [SLEEP_SLICE_SECONDS]  # 沒有睡完 2 秒
    assert env.provider.call_count == 1
    assert _event_types(env.engine) == ["reserve", "release"]
    assert_ledger_consistent(env.engine, done)


def test_qa_failures_cancel_losing_the_race_to_publish_leaves_everything_succeeded(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """發布交易持有 job 列鎖時才來的取消：等鎖、看到 succeeded、被拒（409）；不會雙重結算。"""
    env.provider = FakeProvider(text=_success_text(), usage=Usage(1234, 56))
    submitted = _submit(env)
    job_id = submitted["job"]["id"]
    outcome: dict[str, Any] = {}
    original = qa_question_runner._persist_success

    def cancel_from_another_connection() -> None:
        with Session(env.engine) as session:
            try:
                outcome["result"] = jobs.cancel_job(session, job_id)
            except JobNotCancellableError as error:
                outcome["error"] = error

    def persist_while_a_cancel_is_waiting(*args: Any, **kwargs: Any) -> None:
        thread = threading.Thread(target=cancel_from_another_connection)
        thread.start()
        outcome["thread"] = thread
        wait_until_a_query_is_blocked(env.engine)  # 取消確實卡在 job 列鎖上
        original(*args, **kwargs)

    monkeypatch.setattr(qa_question_runner, "_persist_success", persist_while_a_cancel_is_waiting)

    assert _run(env, submitted) == "succeeded"
    outcome["thread"].join(timeout=15)

    assert "result" not in outcome and isinstance(outcome["error"], JobNotCancellableError)
    done = _get(env, submitted["id"])
    assert done["status"] == "succeeded" and done["answer_status"] == "answered"
    assert _job(env, submitted)["status"] == "succeeded"
    assert _event_types(env.engine) == ["reserve", "settle"]
    assert_ledger_consistent(env.engine, done)


# --- 日誌 ---


@pytest.mark.parametrize("flow", ["exhausted_retries", "cancelled", "spend_limit"])
def test_qa_failures_logs_never_contain_question_chunk_or_answer_text(
    env: Env, sleeps: list[float], flow: str
) -> None:
    with capture_logs() as logs:
        submitted = _submit(env)
        if flow == "exhausted_retries":
            env.provider = FakeProvider("timeout")
        elif flow == "spend_limit":
            env.provider = FakeProvider("spend_limit")
        else:
            env.provider = FakeProvider("rate_limit", on_call=_cancel_during_call(env, submitted))
        _run(env, submitted)
        done = _get(env, submitted["id"])
        captured = logs.text()

    assert len(captured) > 0
    for marker in (*ALL_MARKERS, FAKE_API_KEY):
        assert marker not in captured
    assert not any(marker in json.dumps(done["error"]) for marker in ALL_MARKERS)
    with env.engine.connect() as connection:
        job_row = connection.execute(
            text("SELECT params, result, error_summary FROM jobs WHERE kind = 'qa_question'")
        ).one()
    assert not any(marker in json.dumps(list(job_row), default=str) for marker in ALL_MARKERS)


# --- migration 0022 ---


def test_qa_failures_migration_refuses_downgrade_while_attempts_are_recorded(
    db_engine: Engine, test_database_url: str
) -> None:
    _insert_question(db_engine)
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE qa_questions SET attempts = 2"))

    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        command.downgrade(alembic_config(test_database_url), "0021")

    assert _version(db_engine) == "0022"
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT attempts FROM qa_questions")) == 2


def test_qa_failures_migration_round_trip_keeps_questions_without_attempts(
    db_engine: Engine, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    _insert_question(db_engine)  # attempts 取預設值 0
    try:
        command.downgrade(config, "0021")

        assert _version(db_engine) == "0021"
        assert _count(db_engine, "qa_questions") == 1
        with db_engine.connect() as connection:
            columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'qa_questions'"
                    )
                ).scalars()
            )
        assert "attempts" not in columns

        command.upgrade(config, "head")

        assert _version(db_engine) == "0022"
        with db_engine.connect() as connection:
            assert connection.scalar(text("SELECT attempts FROM qa_questions")) == 0
        command.check(config)
    finally:
        command.upgrade(config, "head")


def test_qa_failures_database_rejects_negative_attempts(db_engine: Engine) -> None:
    _insert_question(db_engine)

    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(text("UPDATE qa_questions SET attempts = -1"))
