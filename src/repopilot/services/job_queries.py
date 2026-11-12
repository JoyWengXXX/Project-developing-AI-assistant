"""背景工作的唯讀查詢（列表與單筆）；不連 Redis，一律從 PostgreSQL 讀取。

契約比照 D30：固定排序（`id DESC`，新的在前）、`total` 為符合條件的總數、`limit`／`offset` 由 API 層
驗證範圍。與寫入的 `services/jobs.py` 分開（同 `snapshot_queries` 與 `snapshots` 的分法）。
"""

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from repopilot.db.models import Job
from repopilot.errors import JobNotFoundError
from repopilot.services import job_rules


def get_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id, populate_existing=True)
    if job is None:
        raise JobNotFoundError("Job not found.")
    return job


def list_jobs(
    session: Session,
    *,
    status: str | None = None,
    kind: str | None = None,
    repository_id: int | None = None,
    snapshot_id: int | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Job], int]:
    """分頁列出工作（`id DESC`）。`status="active"` 等於 queued＋running。"""
    conditions: list[ColumnElement[bool]] = []
    if status == "active":
        conditions.append(Job.status.in_(job_rules.ACTIVE_STATUSES))
    elif status is not None:
        conditions.append(Job.status == status)
    if kind is not None:
        conditions.append(Job.kind == kind)
    if repository_id is not None:
        conditions.append(Job.repository_id == repository_id)
    if snapshot_id is not None:
        conditions.append(Job.snapshot_id == snapshot_id)
    total = session.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    items = list(
        session.scalars(
            select(Job).where(*conditions).order_by(Job.id.desc()).limit(limit).offset(offset)
        )
    )
    return items, total
