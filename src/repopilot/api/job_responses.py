from repopilot.api.schemas import JobErrorResponse, JobProgressResponse, JobResponse
from repopilot.db.models import Job


def to_job_response(job: Job) -> JobResponse:
    """ORM → 回應：不含租約令牌與 work_state；錯誤只含錯誤碼與安全摘要。

    獨立成模組是因為 `routes/jobs.py`（工作查詢）與 `routes/repositories.py`（建立匯入 job）都用它，
    放在任一個路由模組都會造成路由模組互相匯入。
    """
    error = None
    if job.error_code is not None:
        error = JobErrorResponse(
            code=job.error_code, message=job.error_summary or "The job failed."
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
            "progress": JobProgressResponse(
                processed_files=job.processed_files,
                total_files=job.total_files,
                indexed_files=job.indexed_files,
                parse_error_count=job.parse_error_count,
                bytes_received=job.bytes_received,
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
