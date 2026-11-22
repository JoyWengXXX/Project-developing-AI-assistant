from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.orm import Session

from repopilot.api.project_schemas import (
    ProjectConfig,
    ProjectCreateWithLlmRequest,
    ProjectCreateWithLlmResponse,
    ProjectInitRequest,
    ProjectInitResponse,
    ProjectLlmStatusResponse,
    ProjectPlanReferenceResponse,
    ProjectPlanRequest,
    ProjectPlanResponse,
    ProjectPlanUsageResponse,
    ProjectRecommendationResponse,
    ProjectTemplateResponse,
)
from repopilot.api.routes.repositories import SettingsDep
from repopilot.db.models import ProjectPlan
from repopilot.db.session import get_session
from repopilot.services import project_init, project_llm
from repopilot.services import project_planning as planning

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])
SessionDep = Annotated[Session, Depends(get_session)]
PlanIdPath = Annotated[int, Path(ge=1, le=2**63 - 1)]


@router.post("/init", response_model=ProjectInitResponse)
def init_project(body: ProjectInitRequest, settings: SettingsDep) -> ProjectInitResponse:
    """驗證本機初始專案的路徑並偵測語言、框架與依賴；唯讀，不寫資料庫也不改動專案。"""
    managed_dir = settings.managed_repos_dir
    root = project_init.validate_project_path(
        body.project_path,
        settings.repository_roots,
        reserved_roots=[managed_dir] if managed_dir is not None else [],
    )
    detection = project_init.detect_project(root)
    result = detection.classification
    return ProjectInitResponse(
        project_config=ProjectConfig(
            language=result.language,
            framework=result.framework,
            orm=result.orm,
            test_framework=result.test_framework,
            database=result.database,
            dependencies=result.dependencies,
            root_path=detection.root_path,
            detected_at=detection.detected_at,
        ),
        confidence=result.confidence,
        manifests=result.manifests,
        language_files=detection.language_files,
        directories=detection.directories,
    )


@router.post("/create-with-llm", response_model=ProjectCreateWithLlmResponse)
def create_with_llm(
    body: ProjectCreateWithLlmRequest, session: SessionDep, settings: SettingsDep
) -> ProjectCreateWithLlmResponse:
    """無初始骨架時的固定問答；LLM 只提供推薦（失敗時略過），不寫檔、不執行初始化。"""
    turn = project_llm.converse(
        session,
        settings,
        reference_snapshot_id=body.reference_snapshot_id,
        conversation=[
            project_llm.Message(message.role, message.content) for message in body.conversation
        ],
    )
    config = turn.config
    template = turn.template
    recommendation = turn.recommendation
    return ProjectCreateWithLlmResponse(
        done=turn.done,
        step=turn.step,
        question=turn.question,
        options=turn.options,
        collected=turn.collected,
        recommendation=None
        if recommendation is None
        else ProjectRecommendationResponse(
            value=recommendation.value, reason=recommendation.reason, source=recommendation.source
        ),
        llm=ProjectLlmStatusResponse(
            state=turn.llm.state,
            code=turn.llm.code,
            model=turn.llm.model,
            input_tokens=turn.llm.input_tokens,
            output_tokens=turn.llm.output_tokens,
        ),
        project_config=None
        if config is None
        else ProjectConfig(
            language=config.language,
            framework=config.framework,
            orm=config.orm,
            test_framework=config.test_framework,
            database=config.database,
            dependencies=config.dependencies,
        ),
        template=None
        if template is None
        else ProjectTemplateResponse(
            name=template.name, template_path=template.template_path, files=template.files
        ),
    )


def _to_plan_response(row: ProjectPlan) -> ProjectPlanResponse:
    return ProjectPlanResponse(
        id=row.id,
        status=row.status,
        locked=row.status == "confirmed",
        created_at=row.created_at,
        confirmed_at=row.confirmed_at,
        target_project_config=ProjectConfig.model_validate(row.target_project_config),
        project_requirements=row.project_requirements,
        reference=ProjectPlanReferenceResponse(
            snapshot_id=row.reference_snapshot_id,
            repository_name=row.reference_repository_name,
            commit_sha=row.reference_commit_sha,
            available=row.reference_snapshot_id is not None,
        ),
        plan=row.plan,
        plan_markdown=row.plan_markdown,
        usage=ProjectPlanUsageResponse(
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=row.cost_usd,
            usage_known=row.usage_known,
        ),
    )


@router.post("/plan", response_model=ProjectPlanResponse, status_code=status.HTTP_201_CREATED)
def create_plan(
    body: ProjectPlanRequest, session: SessionDep, settings: SettingsDep
) -> ProjectPlanResponse:
    """以參考快照規劃新專案：一次模型呼叫（同步，可能要數十秒），驗證後保存為草稿計畫。

    不寫任何檔案。沒有設定 key／模型、參考 repo 未同意資料外送、額度不足時，在呼叫模型之前就拒絕。
    """
    row = planning.create_plan(
        session,
        settings,
        config=body.target_project_config.model_dump(mode="json"),
        snapshot_id=body.reference_snapshot_id,
        requirements=body.project_requirements,
    )
    return _to_plan_response(row)


@router.get("/plans/{plan_id}", response_model=ProjectPlanResponse)
def get_plan(plan_id: PlanIdPath, session: SessionDep) -> ProjectPlanResponse:
    return _to_plan_response(planning.get_plan(session, plan_id))


@router.post("/plans/{plan_id}/confirm", response_model=ProjectPlanResponse)
def confirm_plan(plan_id: PlanIdPath, session: SessionDep) -> ProjectPlanResponse:
    """確認並鎖定計畫；冪等。要修改需求請重新規劃（建立新的草稿計畫）。"""
    return _to_plan_response(planning.confirm_plan(session, plan_id))
