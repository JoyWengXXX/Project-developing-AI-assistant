from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from repopilot.api.routes.qa import to_usage_response
from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    QaConsentItem,
    QaConsentListResponse,
    QaConsentResponse,
    QaEstimateBasisResponse,
    QaLimitsResponse,
    QaModelOptionResponse,
    QaModelPriceResponse,
    QaProviderKeySubmit,
    QaSettingsResponse,
    QaSettingsUpdate,
    RepositoryIdPath,
)
from repopilot.config import Settings
from repopilot.db.session import get_session
from repopilot.qa import usage_ledger
from repopilot.qa.pricing import (
    ESTIMATE_INPUT_TOKENS,
    ESTIMATE_OUTPUT_TOKENS,
    MODEL_TIERS,
    PRICE_NOTES,
    estimated_cost_per_question_usd,
)
from repopilot.services import qa_consent as consent_service
from repopilot.services import qa_settings as settings_service

router = APIRouter(prefix="/api/v1", tags=["qa"])
SessionDep = Annotated[Session, Depends(get_session)]


def _settings_response(session: Session, settings: Settings) -> QaSettingsResponse:
    view = settings_service.get_view(session, settings)
    summary = usage_ledger.get_usage(
        session,
        view.limits,
        now=datetime.now(UTC),
        tz=ZoneInfo(settings.qa_usage_timezone),
    )
    return QaSettingsResponse(
        provider="anthropic",
        model=view.model,
        provider_key_configured=view.provider_key_configured,
        provider_key_warning=view.provider_key_warning,
        can_ask=view.can_ask,
        limits=QaLimitsResponse(
            per_request_tokens=view.limits.per_request_tokens,
            daily_tokens=view.limits.daily_tokens,
            daily_cost_usd=view.limits.daily_cost_usd,
        ),
        price_table_as_of=view.price_table_as_of,
        estimate_basis=QaEstimateBasisResponse(
            input_tokens=ESTIMATE_INPUT_TOKENS, output_tokens=ESTIMATE_OUTPUT_TOKENS
        ),
        models=[
            QaModelOptionResponse(
                id=model_id,
                tier=tier,
                price=QaModelPriceResponse(
                    input_usd_per_million=view.prices[model_id].input_usd_per_million,
                    output_usd_per_million=view.prices[model_id].output_usd_per_million,
                ),
                estimated_cost_per_question_usd=estimated_cost_per_question_usd(
                    view.prices[model_id]
                ),
                price_note=PRICE_NOTES.get(model_id),
            )
            for model_id, tier in MODEL_TIERS.items()
        ],
        usage=to_usage_response(summary),
        updated_at=view.updated_at,
    )


@router.get("/qa/settings", response_model=QaSettingsResponse)
def get_qa_settings(session: SessionDep, settings: SettingsDep) -> QaSettingsResponse:
    """問答設定；永不回傳 API key（只有 `provider_key_configured`）。"""
    return _settings_response(session, settings)


@router.put("/qa/settings", response_model=QaSettingsResponse)
def update_qa_settings(
    body: QaSettingsUpdate, session: SessionDep, settings: SettingsDep
) -> QaSettingsResponse:
    """部分更新模型、額度與單價表（沒送的欄位不變）；回傳更新後的完整設定。"""
    settings_service.update_settings(
        session,
        body.model_dump(exclude_unset=True),
        now=datetime.now(UTC),
        tz=ZoneInfo(settings.qa_usage_timezone),
    )
    return _settings_response(session, settings)


@router.put("/qa/provider-key", response_model=QaSettingsResponse)
def put_provider_key(
    body: QaProviderKeySubmit, session: SessionDep, settings: SettingsDep
) -> QaSettingsResponse:
    """只寫：加密保存供應商 API key（覆蓋舊的）；請求 body 不會被記錄，回應不含 key。"""
    settings_service.save_provider_key(
        session, body.api_key.get_secret_value(), settings=settings, now=datetime.now(UTC)
    )
    return _settings_response(session, settings)


@router.delete("/qa/provider-key", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider_key(session: SessionDep) -> Response:
    """清除保存的 API key（冪等）。"""
    settings_service.clear_provider_key(session, now=datetime.now(UTC))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _consent_response(repository_id: int, consented_at: datetime | None) -> QaConsentResponse:
    return QaConsentResponse(
        repository_id=repository_id,
        consented=consented_at is not None,
        consented_at=consented_at,
    )


@router.get("/qa/consents", response_model=QaConsentListResponse)
def list_qa_consents(session: SessionDep) -> QaConsentListResponse:
    """所有已同意資料外送的 repo（沒出現的 repo 就是未同意）。"""
    return QaConsentListResponse(
        items=[
            QaConsentItem(repository_id=row.repository_id, consented_at=row.consented_at)
            for row in consent_service.list_consents(session)
        ]
    )


@router.get("/repositories/{repository_id}/qa-consent", response_model=QaConsentResponse)
def get_qa_consent(repository_id: RepositoryIdPath, session: SessionDep) -> QaConsentResponse:
    return _consent_response(
        repository_id, consent_service.get_consented_at(session, repository_id)
    )


@router.put("/repositories/{repository_id}/qa-consent", response_model=QaConsentResponse)
def grant_qa_consent(repository_id: RepositoryIdPath, session: SessionDep) -> QaConsentResponse:
    """同意把此 repo 的片段送往問答供應商；冪等，重複同意保留第一次的時間。"""
    consented_at = consent_service.grant(session, repository_id, now=datetime.now(UTC))
    return _consent_response(repository_id, consented_at)


@router.delete("/repositories/{repository_id}/qa-consent", response_model=QaConsentResponse)
def revoke_qa_consent(repository_id: RepositoryIdPath, session: SessionDep) -> QaConsentResponse:
    """撤回同意（冪等）；回到未同意。"""
    consent_service.revoke(session, repository_id)
    return _consent_response(repository_id, None)
