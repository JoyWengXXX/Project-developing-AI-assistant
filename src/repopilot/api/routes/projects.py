from fastapi import APIRouter

from repopilot.api.project_schemas import ProjectConfig, ProjectInitRequest, ProjectInitResponse
from repopilot.api.routes.repositories import SettingsDep
from repopilot.services import project_init

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


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
    return ProjectInitResponse(
        project_config=ProjectConfig(
            language=detection.language,
            framework=detection.framework,
            orm=detection.orm,
            test_framework=detection.test_framework,
            database=detection.database,
            dependencies=detection.dependencies,
            root_path=detection.root_path,
            detected_at=detection.detected_at,
        ),
        confidence=detection.confidence,
        manifests=detection.manifests,
        language_files=detection.language_files,
        directories=detection.directories,
    )
