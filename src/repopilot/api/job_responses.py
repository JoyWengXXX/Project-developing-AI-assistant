from typing import Any

from repopilot.api.schemas import (
    JobDiagnosticsResponse,
    JobErrorResponse,
    JobPoisonResponse,
    JobProgressResponse,
    JobResponse,
    JobUnitResponse,
)
from repopilot.db.models import Job
from repopilot.services import job_poison


def to_job_response(job: Job) -> JobResponse:
    """ORM → 回應：不含租約令牌與 work_state；錯誤只含錯誤碼與安全摘要。

    獨立成模組是因為 `routes/jobs.py`（工作查詢）與 `routes/repositories.py`（建立匯入 job）都用它，
    放在任一個路由模組都會造成路由模組互相匯入。

    `diagnostics` 是 `work_state` 的安全摘要（毒藥單位計數、running 時的目前單位種類與位置），
    不含單位識別、路徑、內容或 `work_state` 的其他鍵（例如 `temp_dir`）。
    """
    error = None
    if job.error_code is not None:
        error = JobErrorResponse(
            code=job.error_code, message=job.error_summary or "The job failed."
        )
    # work_state 是 JSONB，理論上可能不是物件（人工改資料）；讀取端點不能因此回 500
    work_state: Any = job.work_state
    summary = job_poison.summarize(work_state if isinstance(work_state, dict) else {})
    # 目前單位只在 running 時有意義；已結束或重新排隊的 job 留在 work_state 裡的是過時指標
    current_unit = (
        JobUnitResponse(
            kind=summary.current_unit_kind,
            index=summary.current_unit_index,
            count=summary.current_unit_count,
        )
        if job.status == "running" and summary.current_unit_kind is not None
        else None
    )
    return JobResponse.model_validate(
        {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "phase": job.phase,
            "repository_id": job.repository_id,
            "snapshot_id": job.snapshot_id,
            "params": job.params,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "next_attempt_at": job.next_attempt_at,
            "cancel_requested": job.cancel_requested_at is not None,
            "cancel_requested_at": job.cancel_requested_at,
            "progress": JobProgressResponse(
                processed_files=job.processed_files,
                total_files=job.total_files,
                indexed_files=job.indexed_files,
                parse_error_count=job.parse_error_count,
                bytes_received=job.bytes_received,
            ),
            "diagnostics": JobDiagnosticsResponse(
                current_unit=current_unit,
                poison=JobPoisonResponse(
                    suspect_crashes=summary.suspect_crashes,
                    skipped_units=summary.skipped_units,
                    total_crashes=summary.total_crashes,
                ),
            ),
            "error": error,
            "result": job.result,
            "request_id": job.request_id,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "phase_started_at": job.phase_started_at,
            "last_heartbeat_at": job.last_heartbeat_at,
            "finished_at": job.finished_at,
            "updated_at": job.updated_at,
            "status_url": f"/api/v1/jobs/{job.id}",
        }
    )
