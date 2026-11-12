"""參考版本（`repository_references`）：每個 repo 最多一個「參考版本」標記（切片 3b，D41）。

只做標記與持久化，不呼叫 Git、不接 Agent。規則：

- 快照必須**屬於該 repo**且為 `completed`。歸屬檢查與查詢合為一個 SQL 條件
  （`WHERE id = ? AND repository_id = ?`），別的 repo 的快照一律回 404 `snapshot_not_found`，
  與 D30 一致（不洩漏別的 repo 有沒有這個快照）。資料庫另有複合外鍵作為最後防線。
- 與「移除快照」的競爭交給資料庫：寫入參考時，外鍵檢查會對快照列取 `FOR KEY SHARE`，與移除的
  `FOR UPDATE` 互斥——移除進行中 PUT 會等；移除先提交則外鍵失敗，轉成 404 `snapshot_not_found`；
  PUT 先寫入則移除看到參考（未帶 `force` 回 409）。不另外對快照列加鎖：初版曾在 PUT 的檢查語句
  加 `FOR KEY SHARE`，變異檢查移除它後所有測試結果不變（等價的變異，見 D41），所以不保留多餘的鎖。
- `PUT` 是單一 `INSERT ... ON CONFLICT (repository_id) DO UPDATE`（upsert，併發安全）。重複 PUT
  同一個快照不改動任何資料（`created_at` 保持不變）；換成另一個快照才更新 `created_at`。

.NET 對照：像 EF Core 的 upsert（`ExecuteUpdate` 或 `MERGE`），並讓資料庫外鍵擔任最後一道防線。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.db.models import Repository, RepositoryReference, Snapshot
from repopilot.errors import (
    RepositoryNotFoundError,
    SnapshotNotCompletedError,
    SnapshotNotFoundError,
)
from repopilot.services.repositories import get_repository
from repopilot.services.snapshot_queries import SNAPSHOT_COMPLETED

REFERENCE_SNAPSHOT_FOREIGN_KEY = "fk_repository_references_snapshot_id_snapshots"
REFERENCE_REPOSITORY_FOREIGN_KEY = "fk_repository_references_repository_id_repositories"


@dataclass(frozen=True)
class ReferenceView:
    """一個參考版本與它的 repo、快照摘要（唯讀；快照狀態是「現在」的狀態）。"""

    id: int
    repository_id: int
    repository_name: str
    source_type: Literal["local", "github"]
    github_owner: str | None
    github_name: str | None
    snapshot_id: int
    commit_sha: str
    snapshot_status: str
    file_count: int
    skipped_count: int
    total_bytes: int
    created_at: datetime


def list_references(session: Session) -> list[ReferenceView]:
    """所有參考版本（依 repo id 排序）；單一 JOIN 查詢，不呼叫 Git。"""
    views = _load_views(session, None)
    session.rollback()
    return views


def set_reference(session: Session, repository_id: int, snapshot_id: int) -> ReferenceView:
    """把 `snapshot_id` 設為 repo 的參考版本（upsert）；回傳設定後的參考。"""
    get_repository(session, repository_id)  # 不存在 → 404 repository_not_found
    status = session.scalar(
        select(Snapshot.status).where(
            Snapshot.id == snapshot_id, Snapshot.repository_id == repository_id
        )
    )
    if status is None:
        session.rollback()
        raise SnapshotNotFoundError("Snapshot not found.")
    if status != SNAPSHOT_COMPLETED:
        session.rollback()
        raise SnapshotNotCompletedError("Snapshot import is not completed.")

    statement = pg_insert(RepositoryReference).values(
        repository_id=repository_id, snapshot_id=snapshot_id
    )
    statement = statement.on_conflict_do_update(
        index_elements=[RepositoryReference.repository_id],
        set_={
            "snapshot_id": statement.excluded.snapshot_id,
            # 同一個快照重複設定：保持原本的 created_at（冪等）
            "created_at": case(
                (
                    RepositoryReference.snapshot_id == statement.excluded.snapshot_id,
                    RepositoryReference.created_at,
                ),
                else_=statement.excluded.created_at,
            ),
        },
    )
    try:
        session.execute(statement)
        session.commit()
    except IntegrityError as exc:
        # 上面的 FOR KEY SHARE 已擋掉競爭；這裡是最後防線（約束名稱判斷，同 D20 的慣例）
        session.rollback()
        message = str(exc.orig)
        if REFERENCE_REPOSITORY_FOREIGN_KEY in message:
            raise RepositoryNotFoundError("Repository not found.") from None
        if REFERENCE_SNAPSHOT_FOREIGN_KEY in message:
            raise SnapshotNotFoundError("Snapshot not found.") from None
        raise
    views = _load_views(session, repository_id)
    session.rollback()
    return views[0]


def clear_reference(session: Session, repository_id: int) -> None:
    """取消 repo 的參考版本（冪等：本來就沒有參考也成功）；repo 不存在回 404。"""
    get_repository(session, repository_id)
    session.execute(
        delete(RepositoryReference).where(RepositoryReference.repository_id == repository_id)
    )
    session.commit()


def _load_views(session: Session, repository_id: int | None) -> list[ReferenceView]:
    query = (
        select(
            RepositoryReference.id,
            RepositoryReference.repository_id,
            Repository.name,
            Repository.source_type,
            Repository.github_owner,
            Repository.github_name,
            RepositoryReference.snapshot_id,
            Snapshot.commit_sha,
            Snapshot.status,
            Snapshot.file_count,
            Snapshot.skipped_count,
            Snapshot.total_bytes,
            RepositoryReference.created_at,
        )
        .join(Repository, Repository.id == RepositoryReference.repository_id)
        .join(Snapshot, Snapshot.id == RepositoryReference.snapshot_id)
        .order_by(RepositoryReference.repository_id)
    )
    if repository_id is not None:
        query = query.where(RepositoryReference.repository_id == repository_id)
    return [
        ReferenceView(
            id=row[0],
            repository_id=row[1],
            repository_name=row[2],
            source_type="github" if row[3] == "github" else "local",
            github_owner=row[4],
            github_name=row[5],
            snapshot_id=row[6],
            commit_sha=row[7],
            snapshot_status=row[8],
            file_count=row[9],
            skipped_count=row[10],
            total_bytes=row[11],
            created_at=row[12],
        )
        for row in session.execute(query).all()
    ]
