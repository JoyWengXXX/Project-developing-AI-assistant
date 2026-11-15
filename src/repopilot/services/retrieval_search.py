"""在指定快照的相容已完成索引中搜尋片段。"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, case, func, literal, select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Repository, RetrievalChunk, RetrievalIndex, Snapshot, SnapshotFile
from repopilot.errors import (
    RetrievalIndexNotReadyError,
    RetrievalSnapshotNotCompletedError,
    RetrievalSnapshotNotFoundError,
)
from repopilot.search_query import keyword_terms
from repopilot.services import retrieval_jobs
from repopilot.services.search import _set_statement_timeout, _timeout_as_domain_error


@dataclass(frozen=True)
class KeywordMatch:
    chunk_id: int
    repository_id: int
    snapshot_id: int
    commit_sha: str
    path: str
    start_line: int
    end_line: int
    content: str
    split_kind: str
    support_level: Literal["structure", "text"]
    keyword_score: int


def keyword_search(
    session: Session, settings: Settings, *, snapshot_ids: list[int], query: str, limit: int
) -> list[KeywordMatch]:
    terms = keyword_terms(query)
    with _timeout_as_domain_error():
        _set_statement_timeout(session, settings.search_statement_timeout_seconds)
        snapshots = {
            snapshot.id: snapshot
            for snapshot in session.scalars(
                select(Snapshot)
                .where(Snapshot.id.in_(snapshot_ids))
                .order_by(Snapshot.id)
                .with_for_update(read=True)
            )
        }
        index_ids = []
        for snapshot_id in sorted(snapshot_ids):
            snapshot = snapshots.get(snapshot_id)
            if snapshot is None:
                raise RetrievalSnapshotNotFoundError(snapshot_id)
            if snapshot.status != "completed":
                raise RetrievalSnapshotNotCompletedError(snapshot_id)
            identity = retrieval_jobs.current_identity(session, snapshot_id, settings)
            current = retrieval_jobs.current_index(session, identity)
            if current is None or current[0].status != "completed":
                raise RetrievalIndexNotReadyError(
                    snapshot_id, "none" if current is None else current[0].status
                )
            index_ids.append(current[0].id)

        content = func.substr(
            SnapshotFile.content,
            RetrievalChunk.start_offset + 1,
            RetrievalChunk.end_offset - RetrievalChunk.start_offset,
        )
        score: ColumnElement[int] = literal(0)
        for term in terms:
            score = score + case((func.strpos(func.lower(content), term) > 0, 1), else_=0)
        rows = session.execute(
            select(
                RetrievalChunk.id,
                Repository.id,
                Snapshot.id,
                Snapshot.commit_sha,
                SnapshotFile.path,
                RetrievalChunk.start_line,
                RetrievalChunk.end_line,
                content,
                RetrievalChunk.split_kind,
                score.label("score"),
            )
            .join(Snapshot, Snapshot.repository_id == Repository.id)
            .join(RetrievalIndex, RetrievalIndex.snapshot_id == Snapshot.id)
            .join(RetrievalChunk, RetrievalChunk.chunk_set_id == RetrievalIndex.chunk_set_id)
            .join(SnapshotFile, SnapshotFile.id == RetrievalChunk.snapshot_file_id)
            .where(
                RetrievalIndex.id.in_(index_ids),
                RetrievalIndex.status == "completed",
                SnapshotFile.status == "imported",
                score > 0,
            )
            .order_by(
                score.desc(),
                Repository.id,
                Snapshot.id,
                SnapshotFile.path.collate("C"),
                RetrievalChunk.start_offset,
                RetrievalChunk.id,
            )
            .limit(limit)
        ).all()
    return [
        KeywordMatch(
            chunk_id=row[0],
            repository_id=row[1],
            snapshot_id=row[2],
            commit_sha=row[3],
            path=row[4],
            start_line=row[5],
            end_line=row[6],
            content=row[7],
            split_kind=row[8],
            support_level="structure" if row[8] == "structure" else "text",
            keyword_score=row[9],
        )
        for row in rows
    ]
