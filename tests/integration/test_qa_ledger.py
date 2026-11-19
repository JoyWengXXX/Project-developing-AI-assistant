"""問答額度帳本（4-1b）：真實 PostgreSQL 上的預留、結算、歸還、重置、並發與 migration 0019。"""

import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from repopilot.errors import (
    QaReservationFinalizedError,
    QaReservationNotFoundError,
    TokenBudgetExceededError,
)
from repopilot.qa import usage_ledger
from repopilot.qa.provider import Usage
from repopilot.qa.usage_ledger import ModelPrice, Reservation, UsageLimits
from tests.integration.conftest import alembic_config

TZ = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 11, 19, 3, 0, tzinfo=UTC)  # 台北 11:00
PRICE = ModelPrice(input_usd_per_million=Decimal("1"), output_usd_per_million=Decimal("5"))
NO_LIMITS = UsageLimits()


@pytest.fixture
def ledger_sessions(db_engine: Engine) -> Iterator[sessionmaker[Session]]:
    # 帳本只追加，downgrade 會拒絕有資料的帳本：前後都清空，避免影響其他 migration 測試
    def truncate() -> None:
        with db_engine.begin() as connection:
            connection.execute(text("TRUNCATE qa_usage_ledger RESTART IDENTITY"))

    truncate()
    yield sessionmaker(bind=db_engine, expire_on_commit=False)
    truncate()


def _reserve(
    factory: sessionmaker[Session],
    *,
    input_tokens: int = 600,
    max_tokens: int = 400,
    limits: UsageLimits = NO_LIMITS,
    now: datetime = NOW,
) -> Reservation:
    with factory() as session:
        return usage_ledger.reserve(
            session,
            estimated_input_tokens=input_tokens,
            max_tokens=max_tokens,
            price=PRICE,
            limits=limits,
            now=now,
            tz=TZ,
        )


def _settle(
    factory: sessionmaker[Session],
    reservation_id: UUID,
    usage: Usage | None,
    now: datetime = NOW,
) -> usage_ledger.UsageTotals:
    with factory() as session:
        return usage_ledger.settle(session, reservation_id, usage, price=PRICE, now=now)


def _release(
    factory: sessionmaker[Session],
    reservation_id: UUID,
    used: Usage | None,
    now: datetime = NOW,
) -> usage_ledger.UsageTotals:
    with factory() as session:
        return usage_ledger.release(session, reservation_id, used, price=PRICE, now=now)


def _reset(factory: sessionmaker[Session], now: datetime = NOW) -> None:
    with factory() as session:
        usage_ledger.reset(session, now=now)


def _usage(
    factory: sessionmaker[Session], limits: UsageLimits | None = None, now: datetime = NOW
) -> usage_ledger.UsageSummary:
    with factory() as session:
        return usage_ledger.get_usage(session, limits, now=now, tz=TZ)


def _events(db_engine: Engine) -> list[dict[str, Any]]:
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT event_type, reservation_id, input_tokens, output_tokens, cost_usd, "
                "usage_unknown FROM qa_usage_ledger ORDER BY id"
            )
        ).mappings()
        return [dict(row) for row in rows]


# --- AC-1：單次上限 ---


def test_qa_ledger_request_above_the_per_request_limit_is_rejected_without_any_event(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    limits = UsageLimits(per_request_tokens=1_000)

    with pytest.raises(TokenBudgetExceededError) as exc_info:
        _reserve(ledger_sessions, input_tokens=600, max_tokens=401, limits=limits)

    assert exc_info.value.code == "token_budget_exceeded"
    assert exc_info.value.reason == "per_request_tokens"
    assert (exc_info.value.limit, exc_info.value.requested) == (1_000, 1_001)
    assert _events(db_engine) == []


def test_qa_ledger_request_exactly_at_the_per_request_limit_is_accepted(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    _reserve(ledger_sessions, input_tokens=600, max_tokens=400, limits=UsageLimits(1_000))

    assert [event["event_type"] for event in _events(db_engine)] == ["reserve"]


def test_qa_ledger_daily_token_limit_rejects_and_keeps_existing_events_only(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    limits = UsageLimits(daily_tokens=1_500)
    _reserve(ledger_sessions, limits=limits)

    with pytest.raises(TokenBudgetExceededError) as exc_info:
        _reserve(ledger_sessions, input_tokens=300, max_tokens=201, limits=limits)

    assert exc_info.value.reason == "daily_tokens"
    assert exc_info.value.used == 1_000
    assert len(_events(db_engine)) == 1


def test_qa_ledger_daily_cost_limit_rejects_and_keeps_existing_events_only(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    # 600 輸入 × $1/M ＋ 400 輸出 × $5/M ＝ $0.0026
    limits = UsageLimits(daily_cost_usd=Decimal("0.005"))
    first = _reserve(ledger_sessions, limits=limits)
    assert first.cost_usd == Decimal("0.0026")

    with pytest.raises(TokenBudgetExceededError) as exc_info:
        _reserve(ledger_sessions, limits=limits)

    assert exc_info.value.reason == "daily_cost_usd"
    assert len(_events(db_engine)) == 1


# --- AC-2：並發預留 ---


def test_qa_ledger_concurrent_reservations_never_exceed_the_daily_limit(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    attempts, per_reservation, daily_limit = 12, 100, 500
    limits = UsageLimits(daily_tokens=daily_limit)
    barrier = threading.Barrier(attempts)
    outcomes: list[str] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            _reserve(ledger_sessions, input_tokens=60, max_tokens=40, limits=limits)
            result = "accepted"
        except TokenBudgetExceededError:
            result = "rejected"
        with guard:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == attempts
    assert outcomes.count("accepted") == daily_limit // per_reservation
    assert outcomes.count("rejected") == attempts - daily_limit // per_reservation
    events = _events(db_engine)
    assert len(events) == daily_limit // per_reservation
    assert _usage(ledger_sessions).today.total_tokens == daily_limit


def test_qa_ledger_concurrent_reservations_respect_the_daily_cost_limit(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    # 每筆 $0.0026；上限 $0.0105 → 最多 4 筆
    limits = UsageLimits(daily_cost_usd=Decimal("0.0105"))
    barrier = threading.Barrier(10)
    accepted: list[Reservation] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            reservation = _reserve(ledger_sessions, limits=limits)
        except TokenBudgetExceededError:
            return
        with guard:
            accepted.append(reservation)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(accepted) == 4
    assert _usage(ledger_sessions).today.cost_usd == Decimal("0.0104")


# --- AC-3：結算 ---


def test_qa_ledger_settle_uses_actual_usage_and_gives_back_the_difference(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    reservation = _reserve(ledger_sessions, input_tokens=600, max_tokens=400)
    assert _usage(ledger_sessions).today.total_tokens == 1_000

    settled = _settle(ledger_sessions, reservation.reservation_id, Usage(300, 100))

    today = _usage(ledger_sessions).today
    assert (today.input_tokens, today.output_tokens, today.total_tokens) == (300, 100, 400)
    assert today.cost_usd == Decimal("0.0008")  # 300 × $1/M ＋ 100 × $5/M
    assert settled.cost_usd == today.cost_usd
    events = _events(db_engine)
    assert [event["event_type"] for event in events] == ["reserve", "settle"]
    assert events[1]["usage_unknown"] is False
    assert events[1]["reservation_id"] == reservation.reservation_id


def test_qa_ledger_settled_difference_becomes_available_to_the_next_reservation(
    ledger_sessions: sessionmaker[Session],
) -> None:
    limits = UsageLimits(daily_tokens=1_000)
    first = _reserve(ledger_sessions, limits=limits)
    with pytest.raises(TokenBudgetExceededError):
        _reserve(ledger_sessions, input_tokens=300, max_tokens=200, limits=limits)

    _settle(ledger_sessions, first.reservation_id, Usage(100, 50))

    _reserve(ledger_sessions, input_tokens=300, max_tokens=200, limits=limits)


def test_qa_ledger_settle_with_more_actual_usage_than_reserved_counts_the_actual(
    ledger_sessions: sessionmaker[Session],
) -> None:
    reservation = _reserve(ledger_sessions, input_tokens=100, max_tokens=100)

    _settle(ledger_sessions, reservation.reservation_id, Usage(400, 300))

    assert _usage(ledger_sessions).today.total_tokens == 700


@pytest.mark.parametrize("unknown", [None, Usage.unknown()], ids=["none", "usage_unknown"])
def test_qa_ledger_settle_with_unknown_usage_keeps_the_reserved_amount_and_flags_it(
    db_engine: Engine, ledger_sessions: sessionmaker[Session], unknown: Usage | None
) -> None:
    reservation = _reserve(ledger_sessions, input_tokens=600, max_tokens=400)

    _settle(ledger_sessions, reservation.reservation_id, unknown)

    today = _usage(ledger_sessions).today
    assert (today.input_tokens, today.output_tokens) == (600, 400)
    assert today.cost_usd == reservation.cost_usd
    settle_event = _events(db_engine)[1]
    assert settle_event["event_type"] == "settle"
    assert settle_event["usage_unknown"] is True
    assert (settle_event["input_tokens"], settle_event["output_tokens"]) == (600, 400)


# --- AC-4：取消與失敗的歸還 ---


def test_qa_ledger_release_counts_used_tokens_and_frees_the_unused_reservation(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    reservation = _reserve(ledger_sessions, input_tokens=600, max_tokens=400)

    released = _release(ledger_sessions, reservation.reservation_id, Usage(600, 50))

    today = _usage(ledger_sessions).today
    assert (today.input_tokens, today.output_tokens) == (600, 50)
    assert released.cost_usd == Decimal("0.00085")
    assert [event["event_type"] for event in _events(db_engine)] == ["reserve", "release"]


@pytest.mark.parametrize("used", [None, Usage.unknown()], ids=["none", "usage_unknown"])
def test_qa_ledger_release_without_used_tokens_frees_the_whole_reservation(
    ledger_sessions: sessionmaker[Session], used: Usage | None
) -> None:
    reservation = _reserve(ledger_sessions)

    _release(ledger_sessions, reservation.reservation_id, used)

    today = _usage(ledger_sessions).today
    assert (today.total_tokens, today.cost_usd) == (0, Decimal(0))


def test_qa_ledger_released_reservation_makes_room_for_new_requests(
    ledger_sessions: sessionmaker[Session],
) -> None:
    limits = UsageLimits(daily_tokens=1_000)
    first = _reserve(ledger_sessions, limits=limits)

    _release(ledger_sessions, first.reservation_id, Usage(100, 0))

    _reserve(ledger_sessions, input_tokens=450, max_tokens=450, limits=limits)


def test_qa_ledger_a_reservation_can_only_be_finished_once(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    settled = _reserve(ledger_sessions)
    _settle(ledger_sessions, settled.reservation_id, Usage(10, 10))
    released = _reserve(ledger_sessions)
    _release(ledger_sessions, released.reservation_id, Usage(1, 1))
    before = _events(db_engine)

    actions: list[Callable[[], object]] = [
        lambda: _settle(ledger_sessions, settled.reservation_id, Usage(1, 1)),
        lambda: _release(ledger_sessions, settled.reservation_id, None),
        lambda: _settle(ledger_sessions, released.reservation_id, None),
        lambda: _release(ledger_sessions, released.reservation_id, Usage(5, 5)),
    ]
    for action in actions:
        with pytest.raises(QaReservationFinalizedError):
            action()

    assert _events(db_engine) == before


def test_qa_ledger_finishing_an_unknown_reservation_is_rejected(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    with pytest.raises(QaReservationNotFoundError):
        _settle(ledger_sessions, uuid4(), Usage(1, 1))
    with pytest.raises(QaReservationNotFoundError):
        _release(ledger_sessions, uuid4(), None)

    assert _events(db_engine) == []


def test_qa_ledger_concurrent_settles_of_one_reservation_write_exactly_one_event(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    reservation = _reserve(ledger_sessions)
    barrier = threading.Barrier(6)
    results: list[str] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            _settle(ledger_sessions, reservation.reservation_id, Usage(10, 10))
            outcome = "settled"
        except QaReservationFinalizedError:
            outcome = "finalized"
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(results) == ["finalized"] * 5 + ["settled"]
    assert [event["event_type"] for event in _events(db_engine)] == ["reserve", "settle"]


# --- AC-5：重置 ---


def test_qa_ledger_reset_zeroes_today_but_keeps_history_and_cumulative(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    reservation = _reserve(ledger_sessions)
    _settle(ledger_sessions, reservation.reservation_id, Usage(300, 100))
    before = _events(db_engine)

    _reset(ledger_sessions, now=NOW + timedelta(minutes=1))

    summary = _usage(ledger_sessions, now=NOW + timedelta(minutes=2))
    assert (summary.today.total_tokens, summary.today.cost_usd) == (0, Decimal(0))
    assert summary.cumulative.total_tokens == 400
    assert summary.cumulative.cost_usd == Decimal("0.0008")
    assert summary.last_reset_at == NOW + timedelta(minutes=1)
    events = _events(db_engine)
    assert events[: len(before)] == before
    assert [event["event_type"] for event in events] == ["reserve", "settle", "reset"]
    assert events[2]["reservation_id"] is None


def test_qa_ledger_reset_at_the_same_instant_still_orders_after_earlier_reservations(
    ledger_sessions: sessionmaker[Session],
) -> None:
    _reserve(ledger_sessions, now=NOW)

    _reset(ledger_sessions, now=NOW)

    assert _usage(ledger_sessions, now=NOW).today.total_tokens == 0


def test_qa_ledger_usage_after_a_reset_counts_again_and_frees_the_daily_limit(
    ledger_sessions: sessionmaker[Session],
) -> None:
    limits = UsageLimits(daily_tokens=1_000)
    _reserve(ledger_sessions, limits=limits)
    with pytest.raises(TokenBudgetExceededError):
        _reserve(ledger_sessions, input_tokens=1, max_tokens=0, limits=limits)

    _reset(ledger_sessions, now=NOW + timedelta(seconds=1))
    _reserve(ledger_sessions, now=NOW + timedelta(seconds=2), limits=limits)

    summary = _usage(ledger_sessions, now=NOW + timedelta(seconds=3))
    assert summary.today.total_tokens == 1_000
    assert summary.cumulative.total_tokens == 2_000


@pytest.mark.parametrize("finish", ["settle_known", "settle_unknown", "release"])
def test_qa_ledger_finishing_a_reservation_made_before_a_reset_never_makes_usage_negative(
    ledger_sessions: sessionmaker[Session], finish: str
) -> None:
    reservation = _reserve(ledger_sessions, input_tokens=600, max_tokens=400)
    _reset(ledger_sessions, now=NOW + timedelta(seconds=1))

    later = NOW + timedelta(seconds=2)
    if finish == "settle_known":
        _settle(ledger_sessions, reservation.reservation_id, Usage(100, 10), later)
    elif finish == "settle_unknown":
        _settle(ledger_sessions, reservation.reservation_id, None, later)
    else:
        _release(ledger_sessions, reservation.reservation_id, None, later)

    summary = _usage(ledger_sessions, now=NOW + timedelta(seconds=3))
    assert summary.today.total_tokens == 0
    assert summary.today.cost_usd == 0
    assert summary.cumulative.total_tokens >= 0


# --- 每日週期與剩餘額度 ---


def test_qa_ledger_yesterdays_usage_does_not_count_against_todays_limit(
    ledger_sessions: sessionmaker[Session],
) -> None:
    limits = UsageLimits(daily_tokens=1_000)
    _reserve(ledger_sessions, limits=limits, now=NOW)

    tomorrow = NOW + timedelta(days=1)
    _reserve(ledger_sessions, limits=limits, now=tomorrow)

    summary = _usage(ledger_sessions, now=tomorrow)
    assert summary.today.total_tokens == 1_000
    assert summary.cumulative.total_tokens == 2_000


def test_qa_ledger_day_rolls_over_at_local_midnight_not_utc_midnight(
    ledger_sessions: sessionmaker[Session],
) -> None:
    just_before = datetime(2026, 11, 19, 15, 59, 59, tzinfo=UTC)  # 台北 23:59:59
    midnight = datetime(2026, 11, 19, 16, 0, 0, tzinfo=UTC)  # 台北隔天 00:00
    _reserve(ledger_sessions, now=just_before)

    assert _usage(ledger_sessions, now=just_before).today.total_tokens == 1_000
    assert _usage(ledger_sessions, now=midnight).today.total_tokens == 0


def test_qa_ledger_usage_reports_remaining_quota_only_for_configured_limits(
    ledger_sessions: sessionmaker[Session],
) -> None:
    reservation = _reserve(ledger_sessions)
    _settle(ledger_sessions, reservation.reservation_id, Usage(300, 100))

    without = _usage(ledger_sessions, None)
    unset = _usage(ledger_sessions, UsageLimits(per_request_tokens=10))
    tokens_only = _usage(ledger_sessions, UsageLimits(daily_tokens=1_000))
    both = _usage(ledger_sessions, UsageLimits(daily_tokens=1_000, daily_cost_usd=Decimal("1")))

    assert (without.remaining_tokens, without.remaining_cost_usd) == (None, None)
    assert (unset.remaining_tokens, unset.remaining_cost_usd) == (None, None)
    assert (tokens_only.remaining_tokens, tokens_only.remaining_cost_usd) == (600, None)
    assert (both.remaining_tokens, both.remaining_cost_usd) == (600, Decimal("0.9992"))


def test_qa_ledger_remaining_quota_is_never_negative(
    ledger_sessions: sessionmaker[Session],
) -> None:
    reservation = _reserve(ledger_sessions, input_tokens=100, max_tokens=100)
    _settle(ledger_sessions, reservation.reservation_id, Usage(900, 900))

    summary = _usage(
        ledger_sessions, UsageLimits(daily_tokens=1_000, daily_cost_usd=Decimal("0.001"))
    )

    assert (summary.remaining_tokens, summary.remaining_cost_usd) == (0, Decimal(0))


# --- 資料庫約束 ---


@pytest.mark.parametrize(
    ("event_type", "has_reservation", "input_tokens", "cost", "unknown", "constraint"),
    [
        ("bogus", True, 0, "0", False, "ck_qa_usage_ledger_event_type_valid"),
        ("reset", True, 0, "0", False, "ck_qa_usage_ledger_reservation_matches"),
        ("reserve", False, 0, "0", False, "ck_qa_usage_ledger_reservation_matches"),
        ("reserve", True, -1, "0", False, "ck_qa_usage_ledger_amounts_nonneg"),
        ("reserve", True, 0, "-0.1", False, "ck_qa_usage_ledger_amounts_nonneg"),
        ("reset", False, 1, "0", False, "ck_qa_usage_ledger_reset_amounts_zero"),
        ("reserve", True, 0, "0", True, "ck_qa_usage_ledger_usage_unknown_only"),
        ("release", True, 0, "0", True, "ck_qa_usage_ledger_usage_unknown_only"),
    ],
)
def test_qa_ledger_rejects_invalid_events_at_the_database(
    db_engine: Engine,
    ledger_sessions: sessionmaker[Session],
    event_type: str,
    has_reservation: bool,
    input_tokens: int,
    cost: str,
    unknown: bool,
    constraint: str,
) -> None:
    with pytest.raises(IntegrityError, match=constraint), db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO qa_usage_ledger (event_type, reservation_id, input_tokens, "
                "output_tokens, cost_usd, usage_unknown, created_at) VALUES (:type, "
                "CASE WHEN :has THEN gen_random_uuid() END, :input, 0, CAST(:cost AS numeric), "
                ":unknown, now())"
            ),
            {
                "type": event_type,
                "has": has_reservation,
                "input": input_tokens,
                "cost": cost,
                "unknown": unknown,
            },
        )


def test_qa_ledger_database_allows_one_reserve_and_one_final_event_per_reservation(
    db_engine: Engine, ledger_sessions: sessionmaker[Session]
) -> None:
    reservation_id = uuid4()
    insert = text(
        "INSERT INTO qa_usage_ledger (event_type, reservation_id, input_tokens, output_tokens, "
        "cost_usd, created_at) VALUES (:t, :r, 0, 0, 0, now())"
    )
    with db_engine.begin() as connection:
        connection.execute(insert, {"t": "reserve", "r": reservation_id})
        connection.execute(insert, {"t": "settle", "r": reservation_id})

    with (
        pytest.raises(IntegrityError, match="uq_qa_usage_ledger_reservation_id_reserve"),
        db_engine.begin() as connection,
    ):
        connection.execute(insert, {"t": "reserve", "r": reservation_id})
    with (
        pytest.raises(IntegrityError, match="uq_qa_usage_ledger_reservation_id_final"),
        db_engine.begin() as connection,
    ):
        connection.execute(insert, {"t": "release", "r": reservation_id})


# --- API ---


def test_qa_ledger_usage_endpoint_is_empty_with_null_remaining_quota(
    client: TestClient, ledger_sessions: sessionmaker[Session]
) -> None:
    response = client.get("/api/v1/qa/usage")

    assert response.status_code == 200
    body = response.json()
    zero = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert {k: body["today"][k] for k in zero} == zero
    assert Decimal(body["today"]["cost_usd"]) == 0
    assert body["cumulative"]["total_tokens"] == 0
    assert body["remaining_tokens"] is None
    assert body["remaining_cost_usd"] is None
    assert body["last_reset_at"] is None


def test_qa_ledger_usage_endpoint_reports_settled_usage_and_reset_keeps_cumulative(
    db_engine: Engine, client: TestClient, ledger_sessions: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    reservation = _reserve(ledger_sessions, now=now)
    _settle(ledger_sessions, reservation.reservation_id, Usage(300, 100), now)

    usage = client.get("/api/v1/qa/usage").json()
    reset = client.post("/api/v1/qa/usage/reset")
    after = client.get("/api/v1/qa/usage").json()

    assert (usage["today"]["total_tokens"], usage["cumulative"]["total_tokens"]) == (400, 400)
    assert Decimal(usage["today"]["cost_usd"]) == Decimal("0.0008")
    assert reset.status_code == 200
    assert reset.json()["today"]["total_tokens"] == 0
    assert after["today"]["total_tokens"] == 0
    assert after["cumulative"]["total_tokens"] == 400
    assert after["last_reset_at"] is not None
    assert [event["event_type"] for event in _events(db_engine)] == ["reserve", "settle", "reset"]


# --- AC-6：migration 0019 ---


def _tables(db_engine: Engine) -> set[str]:
    with db_engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _current_version(db_engine: Engine) -> str | None:
    with db_engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
    return None if version is None else str(version)


def test_qa_ledger_models_and_migrations_have_no_drift(
    db_engine: Engine, test_database_url: str
) -> None:
    assert "qa_usage_ledger" in _tables(db_engine)

    command.check(alembic_config(test_database_url))


def test_qa_ledger_migration_downgrade_refuses_while_events_exist(
    db_engine: Engine, test_database_url: str, ledger_sessions: sessionmaker[Session]
) -> None:
    config = alembic_config(test_database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    reservation = _reserve(ledger_sessions)
    _settle(ledger_sessions, reservation.reservation_id, Usage(1, 1))
    before = _events(db_engine)

    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        command.downgrade(config, "0018")

    assert _current_version(db_engine) == head
    assert "qa_usage_ledger" in _tables(db_engine)
    assert _events(db_engine) == before


def test_qa_ledger_migration_downgrades_when_empty_and_upgrades_again(
    db_engine: Engine, test_database_url: str, ledger_sessions: sessionmaker[Session]
) -> None:
    config = alembic_config(test_database_url)
    try:
        command.downgrade(config, "0018")

        assert _current_version(db_engine) == "0018"
        assert "qa_usage_ledger" not in _tables(db_engine)

        command.upgrade(config, "head")

        assert "qa_usage_ledger" in _tables(db_engine)
        assert _events(db_engine) == []
    finally:
        command.upgrade(config, "head")
