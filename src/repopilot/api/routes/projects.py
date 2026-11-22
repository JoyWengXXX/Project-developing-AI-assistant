from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from repopilot.api.project_schemas import (
    ProjectConfig,
    ProjectCreateWithLlmRequest,
    ProjectCreateWithLlmResponse,
    ProjectInitRequest,
    ProjectInitResponse,
    ProjectLlmStatusResponse,
    ProjectRecommendationResponse,
    ProjectTemplateResponse,
)
from repopilot.api.routes.repositories import SettingsDep
from repopilot.db.session import get_session
from repopilot.services import project_init, project_llm

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])
SessionDep = Annotated[Session, Depends(get_session)]


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
