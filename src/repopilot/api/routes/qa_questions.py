from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from repopilot.api.job_responses import to_job_response
from repopilot.api.routes.repositories import SettingsDep
from repopilot.api.schemas import (
    DEFAULT_PAGE_LIMIT,
    JobErrorResponse,
    LimitQuery,
    OffsetQuery,
    QaCitationResponse,
    QaClaimedCitationResponse,
    QaContextChunkResponse,
    QaCostResponse,
    QaEstimateResponse,
    QaQuestionCreate,
    QaQuestionIdPath,
    QaQuestionListItem,
    QaQuestionListResponse,
    QaQuestionResponse,
    QaQuestionsDeletedResponse,
    QaQuestionStatus,
    QaQuestionSubmitResponse,
    QaRejectedStatementResponse,
    QaRejectionReasonResponse,
    QaStatementResponse,
    QaUsageDetailResponse,
)
from repopilot.db.models import QaQuestion
from repopilot.db.session import get_session
from repopilot.qa.question_rules import DEFAULT_MAX_TOKENS
from repopilot.request_context import request_id_var
from repopilot.services import qa_questions as question_service

router = APIRouter(prefix="/api/v1/qa/questions", tags=["qa"])
SessionDep = Annotated[Session, Depends(get_session)]


def _list_item(question: QaQuestion) -> dict[str, object]:
    return {
        "id": question.id,
        "status": question.status,
        "answer_status": question.answer_status,
        "question": question.question,
        "snapshot_ids": list(question.snapshot_ids),
        "provider": question.provider,
        "model": question.model,
        "error_code": question.error_code,
        "attempts": question.attempts,
        "usage": QaUsageDetailResponse(
            input_tokens=question.input_tokens,
            output_tokens=question.output_tokens,
            usage_unknown=question.usage_unknown,
        ),
        "cost": QaCostResponse(
            estimated_usd=question.estimated_cost_usd,
            estimated_input_tokens=question.estimated_input_tokens,
            actual_usd=question.cost_usd,
        ),
        "job_id": question.job_id,
        "created_at": question.created_at,
        "finished_at": question.finished_at,
    }


def to_question_response(detail: question_service.QuestionDetail) -> QaQuestionResponse:
    question = detail.question
    statements = []
    rejected = []
    for item in detail.statements:
        statement = item.statement
        if statement.status == "valid":
            statements.append(
                QaStatementResponse.model_validate(
                    {
                        "position": statement.position,
                        "kind": statement.kind,
                        "text": statement.body,
                        "citations": [
                            QaCitationResponse(
                                chunk_code=citation.claimed_chunk,
                                repository_id=_required(citation.resolved_repository_id),
                                repository=_required(citation.resolved_repo_name),
                                snapshot_id=_required(citation.resolved_snapshot_id),
                                commit_sha=_required(citation.resolved_commit_sha),
                                path=_required(citation.resolved_path),
                                start_line=_required(citation.resolved_start_line),
                                end_line=_required(citation.resolved_end_line),
                            )
                            for citation in item.citations
                        ],
                    }
                )
            )
        else:
            rejected.append(
                QaRejectedStatementResponse.model_validate(
                    {
                        "position": statement.position,
                        "kind": statement.kind,
                        "text": statement.body,
                        "reasons": [
                            QaRejectionReasonResponse(
                                reason=str(reason["reason"]),
                                citation_index=reason.get("citation_index"),
                            )
                            for reason in statement.rejection_reasons
                        ],
                        "claimed_citations": [
                            QaClaimedCitationResponse(
                                chunk=citation.claimed_chunk,
                                start_line=citation.claimed_start_line,
                                end_line=citation.claimed_end_line,
                                valid=citation.valid,
                                reasons=list(citation.reasons),
                            )
                            for citation in item.citations
                        ],
                    }
                )
            )
    error = None
    if question.error_code is not None:
        error = JobErrorResponse(
            code=question.error_code, message=question.error_summary or "The question failed."
        )
    return QaQuestionResponse.model_validate(
        {
            **_list_item(question),
            "prompt_version": question.prompt_version,
            "max_tokens": question.max_tokens,
            "error": error,
            "provider_request_id": question.provider_request_id,
            "duration_ms": question.duration_ms,
            "started_at": question.started_at,
            "statements": statements,
            "rejected_statements": rejected,
            "context_chunks": [
                QaContextChunkResponse.model_validate(
                    {
                        "rank": chunk.rank,
                        "code": chunk.code,
                        "skipped_reason": chunk.skip_reason,
                        "repository_id": chunk.repository_id,
                        "repository": chunk.repo_name,
                        "snapshot_id": chunk.snapshot_id,
                        "commit_sha": chunk.commit_sha,
                        "path": chunk.path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "chunk_id": chunk.chunk_id,
                        "truncated": chunk.truncated,
                    }
                )
                for chunk in detail.chunks
            ],
        }
    )


def _required[T](value: T | None) -> T:
    # 資料庫在「已驗證陳述」上保證解析欄位存在；這裡只是讓型別檢查器與執行期一致
    if value is None:
        raise ValueError("A verified citation is missing its resolved position.")
    return value


@router.post(
    "",
    response_model=QaQuestionSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "已建立問答與背景工作；以 `GET status_url` 輪詢"},
        403: {
            "description": "有 repo 未同意資料外送（qa_egress_not_consented，details 指出 repo）"
        },
        404: {"description": "快照不存在（snapshot_not_found）"},
        409: {
            "description": "快照未完成、檢索索引未就緒、未選模型或額度不足"
            "（snapshot_not_completed／retrieval_index_not_ready／qa_model_not_selected／"
            "token_budget_exceeded）"
        },
        422: {"description": "輸入驗證或選取範圍太大（validation_error／search_scope_too_large）"},
        503: {"description": "沒有可用的供應商 API key（qa_provider_not_configured）"},
    },
)
def submit_question(
    body: QaQuestionCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> QaQuestionSubmitResponse:
    """提問：在任何外部呼叫前驗證所有前置條件，建立問答與背景工作（同一交易）。"""
    submitted = question_service.submit_question(
        session,
        settings,
        snapshot_ids=body.snapshot_ids,
        question=body.question,
        max_tokens=body.max_tokens or DEFAULT_MAX_TOKENS,
        request_id=request_id_var.get(),
    )
    status_url = f"/api/v1/qa/questions/{submitted.question.id}"
    response.headers["Location"] = status_url
    estimate = submitted.estimate
    return QaQuestionSubmitResponse(
        id=submitted.question.id,
        status="queued",
        model=submitted.question.model,
        job=to_job_response(submitted.job),
        estimate=QaEstimateResponse(
            input_tokens_upper_bound=estimate.input_tokens_upper_bound,
            max_output_tokens=estimate.max_output_tokens,
            total_tokens_upper_bound=estimate.total_tokens_upper_bound,
            cost_usd=estimate.cost_usd,
        ),
        status_url=status_url,
    )


@router.get("", response_model=QaQuestionListResponse)
def list_questions(
    session: SessionDep,
    status: QaQuestionStatus | None = None,
    limit: LimitQuery = DEFAULT_PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> QaQuestionListResponse:
    """歷史問答（新的在前）；`status` 可篩選。列表不含陳述與片段，詳情用 `GET /{id}`。"""
    items, total = question_service.list_questions(
        session, status=status, limit=limit, offset=offset
    )
    return QaQuestionListResponse(
        items=[QaQuestionListItem.model_validate(_list_item(item)) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete("", response_model=QaQuestionsDeletedResponse)
def delete_all_questions(session: SessionDep) -> QaQuestionsDeletedResponse:
    """刪除所有已結束的問答（含陳述、引用與片段記錄）；排隊或執行中的保留並回報數量。"""
    deleted, skipped = question_service.delete_all_questions(session)
    return QaQuestionsDeletedResponse(deleted=deleted, skipped_active=skipped)


@router.get(
    "/{question_id}",
    response_model=QaQuestionResponse,
    responses={404: {"description": "問答不存在（qa_question_not_found）"}},
)
def get_question(question_id: QaQuestionIdPath, session: SessionDep) -> QaQuestionResponse:
    """完整追蹤：狀態、驗證後的答案與引用、被拒絕的陳述與原因、送出的片段清單、用量、費用與錯誤。"""
    return to_question_response(question_service.get_question(session, question_id))


@router.delete(
    "/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "問答不存在（qa_question_not_found）"},
        409: {"description": "問答排隊或執行中（qa_question_active）"},
    },
)
def delete_question(question_id: QaQuestionIdPath, session: SessionDep) -> Response:
    question_service.delete_question(session, question_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
