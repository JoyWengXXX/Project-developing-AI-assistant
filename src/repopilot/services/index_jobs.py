"""提交結構索引 job（第二階段切片 6，規格 4.4）。

`POST /api/v1/snapshots/{snapshot_id}/index-jobs`：為已 `completed` 的快照排入索引建構。索引只讀
資料庫裡的快照內容，所以 API 端**不碰 Git、也不需要路徑映射**。

冪等規則（`dedupe_key` ＝ `index:{snapshot_id}:{index_profile_hash}`），順序即優先序：

| 目前狀態 | 結果 |
| --- | --- |
| 快照不存在 | 404 `snapshot_not_found` |
| 快照尚未 `completed` | 409 `snapshot_not_completed` |
| 有 `queued`／`running` 的同鍵 job | 回既有 job（202） |
| 目前設定的索引已 `completed` | 不建立 job，200＋`reused=true` |
| 最近的 job 是 `failed`／`cancelled` | 重新排入**同一個 job**（202） |
| 其他 | 建立新 job（202） |

匯入 job 的索引階段使用另一個 `dedupe_key`（`import:…`）；兩種 job 同時想建立同一份索引時，
由 worker 端的索引鎖（`db.locks.index_lock_key`）序列化。
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job, Snapshot
from repopilot.errors import SnapshotNotCompletedError, SnapshotNotFoundError
from repopilot.indexing.profile import IndexProfile
from repopilot.services import jobs as job_service
from repopilot.services.import_jobs import (
    find_active_job,
    index_is_completed,
    latest_job,
    latest_succeeded_job,
)
from repopilot.services.repositories import get_repository, repository_key

_MAX_SUBMIT_ATTEMPTS = 5


@dataclass(frozen=True)
class IndexJobSubmission:
    job: Job | None
    snapshot_id: int
    repository_id: int
    reused: bool  # True＝目前設定的索引已 completed，沒有新的工作（API 回 200）


def index_dedupe_key(snapshot_id: int, index_profile_hash: str) -> str:
    return f"index:{snapshot_id}:{index_profile_hash}"


def submit_index_job(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    request_id: str | None = None,
) -> IndexJobSubmission:
    snapshot = session.get(Snapshot, snapshot_id, populate_existing=True)
    if snapshot is None:
        session.rollback()
        raise SnapshotNotFoundError("Snapshot not found.")
    if snapshot.status != "completed":
        session.rollback()
        raise SnapshotNotCompletedError("The snapshot has not been imported completely.")
    repository = get_repository(session, snapshot.repository_id)
    repository_id = repository.id
    key_for_repo = repository_key(repository)
    session.rollback()

    index_hash = IndexProfile.from_settings(settings).hash()
    dedupe_key = index_dedupe_key(snapshot_id, index_hash)
    params = {"snapshot_id": snapshot_id}

    for _ in range(_MAX_SUBMIT_ATTEMPTS):
        active = find_active_job(session, dedupe_key)
        if active is not None:
            return IndexJobSubmission(active, snapshot_id, repository_id, reused=False)

        if index_is_completed(session, snapshot_id, index_hash):
            return IndexJobSubmission(
                latest_succeeded_job(session, snapshot_id), snapshot_id, repository_id, True
            )

        latest = latest_job(session, dedupe_key)
        if latest is not None and latest.status in ("failed", "cancelled"):
            resubmitted = job_service.resubmit_job(
                session,
                latest.id,
                settings,
                request_id=request_id,
                params=params,
                index_profile_hash=index_hash,
            )
            if resubmitted is not None:
                return IndexJobSubmission(resubmitted, snapshot_id, repository_id, reused=False)
            continue  # 狀態在這期間變了：重新判斷

        created = job_service.create_job(
            session,
            kind="snapshot_index",
            dedupe_key=dedupe_key,
            settings=settings,
            repository_id=repository_id,
            snapshot_id=snapshot_id,
            repo_key=key_for_repo,
            params=params,
            index_profile_hash=index_hash,
            request_id=request_id,
        )
        return IndexJobSubmission(created.job, snapshot_id, repository_id, reused=False)
    raise RuntimeError("Could not submit the index job.")
