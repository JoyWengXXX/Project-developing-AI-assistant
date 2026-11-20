"""問答額度帳本（第四階段 4-1b）：原子預留、結算、歸還、重置與用量查詢。

帳本 `qa_usage_ledger` 只追加。一個預留的事件以 `reservation_id` 串起來：`reserve`，之後至多一個
`settle`（供應商回報的實際用量；未知時沿用預留量並標記）或 `release`（取消／失敗：已產生的用量
照實計入，沒用到的預留釋放）。`reset` 不屬於任何預留。

**用量由事件推導，沒有另外的累計欄位**：每個預留的「有效用量」＝結束事件（settle／release）的
數量，沒有結束事件則為預留量。「今日」只計 reserve 事件落在目前每日視窗內的預留，視窗從
「所在時區今天 00:00」與「最近一次 reset 之後」兩者較晚者開始。因此 reset 之前建立的預留即使
之後才結算或歸還，也不會改變今日用量（不可能變成負數），但仍計入累計。

**原子性**：所有寫入都在同一個交易內先取 `pg_advisory_xact_lock`（會等待、交易結束自動釋放），
再讀取今日用量、判斷、寫入事件，所以並發預留被序列化，通過的預留總和不會超過每日上限；
超限時 rollback，帳本沒有任何新事件。注意：結算時實際用量可以大於預留量，今日用量可能因此略超過
上限（用量是已發生的事實，不能拒絕）；下一次預留會看到它。

.NET 對照：像在 `IsolationLevel.ReadCommitted` 交易內先取 `sp_getapplock`（Transaction 範圍）
再做「讀總和、檢查、INSERT」，而不是依賴 Serializable 重試。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, ColumnElement, Numeric, and_, cast, func, select, text
from sqlalchemy.orm import Session, aliased

from repopilot.db.locks import qa_usage_lock_key
from repopilot.db.models import QaUsageLedger
from repopilot.errors import (
    QaReservationFinalizedError,
    QaReservationNotFoundError,
    TokenBudgetExceededError,
)
from repopilot.qa.provider import Usage

_MILLION = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.00000001")
_ZERO_COST = Decimal(0).quantize(_COST_QUANTUM)


@dataclass(frozen=True)
class ModelPrice:
    """單價（美元／百萬 token），輸入與輸出分開；由呼叫端提供，這裡不內建任何價格。"""

    input_usd_per_million: Decimal
    output_usd_per_million: Decimal


@dataclass(frozen=True)
class UsageLimits:
    """額度上限；`None` 表示該項不限制。單次上限以 token 計（估算輸入＋`max_tokens`）。"""

    per_request_tokens: int | None = None
    daily_tokens: int | None = None
    daily_cost_usd: Decimal | None = None


@dataclass(frozen=True)
class Reservation:
    reservation_id: UUID
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class UsageTotals:
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    # 只有 settle 會設定：實際用量（已知）超過該次預留的 token 數
    exceeded_reservation: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class UsageSummary:
    today: UsageTotals
    cumulative: UsageTotals
    window_start: datetime
    window_end: datetime
    last_reset_at: datetime | None
    # 未設定上限（含 limits 為 None）時為 None；已超過時為 0
    remaining_tokens: int | None
    remaining_cost_usd: Decimal | None


def token_cost_usd(input_tokens: int, output_tokens: int, price: ModelPrice) -> Decimal:
    """token 數換算美元，四捨五入到 8 位小數（與帳本欄位一致）。

    `input_tokens` 是總輸入：`input_tokens` ＋ 快取建立 ＋ 快取讀取（`qa.provider.Usage` 已加總）；
    `output_tokens` 含思考 token。快取讀取實際上可能有不同單價，本階段不區分。
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("Token counts must not be negative.")
    cost = (
        input_tokens * price.input_usd_per_million + output_tokens * price.output_usd_per_million
    ) / _MILLION
    return cost.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)


def day_window(now: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    """`now` 所在的每日視窗 `[start, end)`：`tz` 時區的當地午夜到下一個午夜，回傳 UTC 時間。"""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware.")
    local_day: date = now.astimezone(tz).date()
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def reserve(
    session: Session,
    *,
    estimated_input_tokens: int,
    max_tokens: int,
    price: ModelPrice,
    limits: UsageLimits,
    now: datetime,
    tz: tzinfo,
    reservation_id: UUID | None = None,
) -> Reservation:
    """預留 `estimated_input_tokens + max_tokens`；超限拋 `TokenBudgetExceededError` 且不寫事件。

    `reservation_id` 可由呼叫端先決定並先記錄（問答先把它寫進追蹤記錄再預留，崩潰後仍找得到）。
    """
    if estimated_input_tokens < 0 or max_tokens < 0:
        raise ValueError("Token counts must not be negative.")
    requested = estimated_input_tokens + max_tokens
    if limits.per_request_tokens is not None and requested > limits.per_request_tokens:
        raise TokenBudgetExceededError(
            "per_request_tokens", limit=limits.per_request_tokens, requested=requested
        )
    cost = token_cost_usd(estimated_input_tokens, max_tokens, price)
    window_start, window_end = day_window(now, tz)
    try:
        _lock(session)
        today = _today_totals(session, window_start, window_end)
        if limits.daily_tokens is not None and today.total_tokens + requested > limits.daily_tokens:
            raise TokenBudgetExceededError(
                "daily_tokens",
                limit=limits.daily_tokens,
                requested=requested,
                used=today.total_tokens,
            )
        if limits.daily_cost_usd is not None and today.cost_usd + cost > limits.daily_cost_usd:
            raise TokenBudgetExceededError(
                "daily_cost_usd",
                limit=str(limits.daily_cost_usd),
                requested=str(cost),
                used=str(today.cost_usd),
            )
        reservation_id = reservation_id or uuid4()
        _append(session, "reserve", reservation_id, estimated_input_tokens, max_tokens, cost, now)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return Reservation(reservation_id, estimated_input_tokens, max_tokens, cost)


def settle(
    session: Session,
    reservation_id: UUID,
    usage: Usage | None,
    *,
    price: ModelPrice,
    now: datetime,
    commit: bool = True,
) -> UsageTotals:
    """以實際用量結算（取代預留量，差額自動歸還）；`usage` 為 None 或未知時沿用預留量並標記。

    回傳這個預留結算後的有效用量。`commit=False` 時只 flush，由呼叫端在同一交易內寫完自己的資料後
    提交（問答把結算與答案保存放在同一交易）；帳本鎖會持有到該交易結束。
    """
    try:
        _lock(session)
        reserved = _open_reservation(session, reservation_id)
        if usage is None or not usage.known:
            totals = UsageTotals(reserved.input_tokens, reserved.output_tokens, reserved.cost_usd)
            unknown = True
        else:
            cost = token_cost_usd(usage.input_tokens, usage.output_tokens, price)
            exceeded = (
                usage.input_tokens + usage.output_tokens
                > reserved.input_tokens + reserved.output_tokens
            )
            totals = UsageTotals(usage.input_tokens, usage.output_tokens, cost, exceeded)
            unknown = False
        _append(
            session,
            "settle",
            reservation_id,
            totals.input_tokens,
            totals.output_tokens,
            totals.cost_usd,
            now,
            usage_unknown=unknown,
        )
        if commit:
            session.commit()
    except BaseException:
        session.rollback()
        raise
    return totals


def release(
    session: Session,
    reservation_id: UUID,
    used_so_far: Usage | None,
    *,
    price: ModelPrice,
    now: datetime,
    commit: bool = True,
) -> UsageTotals:
    """取消或失敗：已產生的用量照實計入，沒用到的預留釋放。

    `used_so_far` 為 None 或未知時視為沒有用量（全數釋放）；呼叫端若無法確定供應商有沒有計費，
    想保守計入就改用 `settle(..., None)` 保留預留量。`commit=False` 的語意同 `settle`。
    """
    input_tokens = output_tokens = 0
    if used_so_far is not None and used_so_far.known:
        input_tokens, output_tokens = used_so_far.input_tokens, used_so_far.output_tokens
    cost = token_cost_usd(input_tokens, output_tokens, price)
    try:
        _lock(session)
        _open_reservation(session, reservation_id)
        _append(session, "release", reservation_id, input_tokens, output_tokens, cost, now)
        if commit:
            session.commit()
    except BaseException:
        session.rollback()
        raise
    return UsageTotals(input_tokens, output_tokens, cost)


def reset(session: Session, *, now: datetime) -> None:
    """手動重置：追加一個 reset 事件；今日用量歸零，歷史事件與累計不變。"""
    try:
        _lock(session)
        _append(session, "reset", None, 0, 0, _ZERO_COST, now)
        session.commit()
    except BaseException:
        session.rollback()
        raise


def get_usage(
    session: Session, limits: UsageLimits | None, *, now: datetime, tz: tzinfo
) -> UsageSummary:
    """今日與累計用量；剩餘額度只在 `limits` 有對應上限時計算，否則為 None。"""
    window_start, window_end = day_window(now, tz)
    today = _today_totals(session, window_start, window_end)
    cumulative = _totals(session)
    last_reset_at = session.scalar(
        select(func.max(QaUsageLedger.created_at)).where(QaUsageLedger.event_type == "reset")
    )
    session.rollback()
    remaining_tokens = remaining_cost = None
    if limits is not None and limits.daily_tokens is not None:
        remaining_tokens = max(0, limits.daily_tokens - today.total_tokens)
    if limits is not None and limits.daily_cost_usd is not None:
        remaining_cost = max(_ZERO_COST, limits.daily_cost_usd - today.cost_usd)
    return UsageSummary(
        today=today,
        cumulative=cumulative,
        window_start=window_start,
        window_end=window_end,
        last_reset_at=last_reset_at,
        remaining_tokens=remaining_tokens,
        remaining_cost_usd=remaining_cost,
    )


def _lock(session: Session) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"), {"key": qa_usage_lock_key()}
    )


def lock_and_check_open(session: Session, reservation_id: UUID) -> bool:
    """取得帳本鎖（持有到交易結束）並回報預留是否仍開著；不提交、不回滾。"""
    _lock(session)
    try:
        _open_reservation(session, reservation_id)
    except (QaReservationNotFoundError, QaReservationFinalizedError):
        return False
    return True


def _append(
    session: Session,
    event_type: str,
    reservation_id: UUID | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    now: datetime,
    *,
    usage_unknown: bool = False,
) -> None:
    session.add(
        QaUsageLedger(
            reservation_id=reservation_id,
            event_type=event_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            usage_unknown=usage_unknown,
            created_at=now,
        )
    )
    session.flush()


def _open_reservation(session: Session, reservation_id: UUID) -> QaUsageLedger:
    """取得預留事件；不存在或已結束就拋錯（呼叫端須已持有帳本鎖）。"""
    events = session.scalars(
        select(QaUsageLedger).where(
            QaUsageLedger.reservation_id == reservation_id,
            QaUsageLedger.event_type.in_(("reserve", "settle", "release")),
        )
    ).all()
    reserved = next((event for event in events if event.event_type == "reserve"), None)
    if reserved is None:
        raise QaReservationNotFoundError("Reservation not found.")
    if len(events) > 1:
        raise QaReservationFinalizedError("Reservation was already settled or released.")
    return reserved


def _today_totals(session: Session, window_start: datetime, window_end: datetime) -> UsageTotals:
    last_reset_id = (
        select(func.coalesce(func.max(QaUsageLedger.id), 0))
        .where(QaUsageLedger.event_type == "reset")
        .scalar_subquery()
    )
    return _totals(
        session,
        QaUsageLedger.created_at >= window_start,
        QaUsageLedger.created_at < window_end,
        QaUsageLedger.id > last_reset_id,
    )


def _totals(session: Session, *window: ColumnElement[bool]) -> UsageTotals:
    """對 reserve 事件（可加視窗條件）加總各預留的有效用量。"""
    final = aliased(QaUsageLedger)
    row = session.execute(
        select(
            cast(
                func.coalesce(
                    func.sum(func.coalesce(final.input_tokens, QaUsageLedger.input_tokens)), 0
                ),
                BigInteger,
            ),
            cast(
                func.coalesce(
                    func.sum(func.coalesce(final.output_tokens, QaUsageLedger.output_tokens)), 0
                ),
                BigInteger,
            ),
            cast(
                func.coalesce(func.sum(func.coalesce(final.cost_usd, QaUsageLedger.cost_usd)), 0),
                Numeric(18, 8),
            ),
        )
        .select_from(QaUsageLedger)
        .outerjoin(
            final,
            and_(
                final.reservation_id == QaUsageLedger.reservation_id,
                final.event_type.in_(("settle", "release")),
            ),
        )
        .where(QaUsageLedger.event_type == "reserve", *window)
    ).one()
    return UsageTotals(int(row[0]), int(row[1]), Decimal(row[2]))
