from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import QaUsageResponse, QaUsageTotalsResponse
from repopilot.db.session import get_session
from repopilot.qa import usage_ledger

router = APIRouter(prefix="/api/v1/qa", tags=["qa"])
SessionDep = Annotated[Session, Depends(get_session)]


def _to_response(summary: usage_ledger.UsageSummary) -> QaUsageResponse:
    def totals(value: usage_ledger.UsageTotals) -> QaUsageTotalsResponse:
        return QaUsageTotalsResponse(
            input_tokens=value.input_tokens,
            output_tokens=value.output_tokens,
            total_tokens=value.total_tokens,
            cost_usd=value.cost_usd,
        )

    return QaUsageResponse(
        today=totals(summary.today),
        cumulative=totals(summary.cumulative),
        window_start=summary.window_start,
        window_end=summary.window_end,
        last_reset_at=summary.last_reset_at,
        remaining_tokens=summary.remaining_tokens,
        remaining_cost_usd=summary.remaining_cost_usd,
    )


# 額度上限由 4-1c 的 qa_settings 提供；在那之前沒有上限，剩餘額度一律為 null
@router.get("/usage", response_model=QaUsageResponse)
def get_usage(session: SessionDep, settings: SettingsDep) -> QaUsageResponse:
    """今日（所在時區的當地日）與累計的 token 與費用。"""
    summary = usage_ledger.get_usage(
        session, None, now=datetime.now(UTC), tz=ZoneInfo(settings.qa_usage_timezone)
    )
    return _to_response(summary)


@router.post("/usage/reset", response_model=QaUsageResponse)
def reset_usage(session: SessionDep, settings: SettingsDep) -> QaUsageResponse:
    """手動重置：今日用量歸零；帳本保留所有歷史事件，累計不變。回傳重置後的用量。"""
    now = datetime.now(UTC)
    usage_ledger.reset(session, now=now)
    summary = usage_ledger.get_usage(
        session, None, now=now, tz=ZoneInfo(settings.qa_usage_timezone)
    )
    return _to_response(summary)
