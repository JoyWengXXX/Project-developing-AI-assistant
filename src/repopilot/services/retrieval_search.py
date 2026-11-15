"""在指定快照的相容已完成索引中搜尋片段。"""

from dataclasses import dataclass, replace
from typing import Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import ColumnElement, Float, case, func, literal, select, type_coerce
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import (
    Repository,
    RetrievalChunk,
    RetrievalIndex,
    RetrievalVector,
    Snapshot,
    SnapshotFile,
)
from repopilot.embedding.gateway_client import embed_query
from repopilot.embedding.profile import EmbeddingProfile
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
    keyword_score: int | None
    start_offset: int
    semantic_score: float | None = None
    hybrid_score: float | None = None


_CANDIDATE_LIMIT = 100
_RRF_K = 60


def _index_ids(session: Session, settings: Settings, snapshot_ids: list[int]) -> list[int]:
    snapshots = {
        snapshot.id: snapshot
        for snapshot in session.scalars(
            select(Snapshot)
            .where(Snapshot.id.in_(snapshot_ids))
            .order_by(Snapshot.id)
            .with_for_update(read=True)
        )
    }
    result = []
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
        result.append(current[0].id)
    return result


def keyword_search(
    session: Session, settings: Settings, *, snapshot_ids: list[int], query: str, limit: int
) -> list[KeywordMatch]:
    terms = keyword_terms(query)
    with _timeout_as_domain_error():
        _set_statement_timeout(session, settings.search_statement_timeout_seconds)
        index_ids = _index_ids(session, settings, snapshot_ids)
        return _keyword_candidates(session, index_ids, terms, limit)


def _keyword_candidates(
    session: Session, index_ids: list[int], terms: list[str], limit: int
) -> list[KeywordMatch]:
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
            RetrievalChunk.start_offset,
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
            start_offset=row[10],
        )
        for row in rows
    ]


def _semantic_candidates(
    session: Session, index_ids: list[int], vector: list[float], limit: int
) -> list[KeywordMatch]:
    distance = type_coerce(
        RetrievalVector.embedding.op("<=>")(literal(vector, type_=Vector(768))), Float
    )
    scores = (
        select(RetrievalVector.chunk_id, func.min(distance).label("distance"))
        .where(RetrievalVector.index_id.in_(index_ids))
        .group_by(RetrievalVector.chunk_id)
        .subquery()
    )
    content = func.substr(
        SnapshotFile.content,
        RetrievalChunk.start_offset + 1,
        RetrievalChunk.end_offset - RetrievalChunk.start_offset,
    )
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
            scores.c.distance,
            RetrievalChunk.start_offset,
        )
        .join(Snapshot, Snapshot.repository_id == Repository.id)
        .join(RetrievalChunk, RetrievalChunk.snapshot_id == Snapshot.id)
        .join(scores, scores.c.chunk_id == RetrievalChunk.id)
        .join(SnapshotFile, SnapshotFile.id == RetrievalChunk.snapshot_file_id)
        .where(SnapshotFile.status == "imported")
        .order_by(
            scores.c.distance,
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
            keyword_score=None,
            semantic_score=1.0 - row[9],
            start_offset=row[10],
        )
        for row in rows
    ]


def search(
    session: Session,
    settings: Settings,
    *,
    snapshot_ids: list[int],
    query: str,
    mode: Literal["keyword", "semantic", "hybrid"],
    limit: int,
) -> list[KeywordMatch]:
    if mode == "keyword":
        return keyword_search(
            session, settings, snapshot_ids=snapshot_ids, query=query, limit=limit
        )
    with _timeout_as_domain_error():
        _set_statement_timeout(session, settings.search_statement_timeout_seconds)
        index_ids = _index_ids(session, settings, snapshot_ids)
        vector = embed_query(settings, EmbeddingProfile.from_settings(settings), query)
        semantic = _semantic_candidates(
            session, index_ids, vector, limit if mode == "semantic" else _CANDIDATE_LIMIT
        )
        if mode == "semantic":
            return semantic
        keyword = _keyword_candidates(session, index_ids, keyword_terms(query), _CANDIDATE_LIMIT)
    by_id = {item.chunk_id: item for item in semantic}
    by_id.update({item.chunk_id: item for item in keyword})
    scores: dict[int, float] = {}
    semantic_scores = {item.chunk_id: item.semantic_score for item in semantic}
    for candidates in (keyword, semantic):
        for rank, item in enumerate(candidates, 1):
            scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
    combined = [
        replace(item, semantic_score=semantic_scores.get(chunk_id), hybrid_score=scores[chunk_id])
        for chunk_id, item in by_id.items()
    ]
    return sorted(
        combined,
        key=lambda item: (
            -scores[item.chunk_id],
            item.repository_id,
            item.snapshot_id,
            item.path.encode("utf-8"),
            item.start_offset,
            item.chunk_id,
        ),
    )[:limit]
