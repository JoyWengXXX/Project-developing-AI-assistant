"""具所有權柵欄的檢索向量儲存。"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, delete, insert, select
from sqlalchemy.orm import Session

from repopilot.db.locks import AdvisoryLock, lock_key
from repopilot.db.models import (
    RetrievalChunk,
    RetrievalChunkSet,
    RetrievalIndex,
    RetrievalVector,
    Snapshot,
)
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import JobLeaseLostError, SnapshotNotCompletedError, SnapshotNotFoundError


@dataclass(frozen=True)
class VectorSegment:
    chunk_id: int
    ordinal: int
    start_offset: int
    end_offset: int
    token_count: int
    embedding: list[float]


@dataclass(frozen=True)
class ClaimedIndex:
    index_id: int
    reused: bool


def retrieval_lock_key(chunk_set_id: int, profile: EmbeddingProfile) -> int:
    return lock_key("retrieval", f"{chunk_set_id}:{profile.hash()}")


def claim_index(
    session: Session,
    *,
    chunk_set_id: int,
    profile: EmbeddingProfile,
    owner_token: UUID,
    lock: AdvisoryLock,
) -> ClaimedIndex:
    """取得或重用索引；呼叫者持有專用鎖，並負責提交交易。"""
    if lock.shared or lock.key != retrieval_lock_key(chunk_set_id, profile):
        raise ValueError("The retrieval index lock does not match.")
    lock.assert_alive()
    chunk_set = session.execute(
        select(RetrievalChunkSet)
        .where(RetrievalChunkSet.id == chunk_set_id)
        .with_for_update(read=True)
    ).scalar_one_or_none()
    if chunk_set is None:
        raise ValueError("The retrieval chunk set does not exist.")
    snapshot = session.execute(
        select(Snapshot).where(Snapshot.id == chunk_set.snapshot_id).with_for_update(read=True)
    ).scalar_one_or_none()
    if snapshot is None:
        raise SnapshotNotFoundError("Snapshot not found.")
    if snapshot.status != "completed":
        raise SnapshotNotCompletedError("The snapshot has not been imported completely.")
    existing = session.execute(
        select(RetrievalIndex)
        .where(
            RetrievalIndex.chunk_set_id == chunk_set_id,
            RetrievalIndex.profile_hash == profile.hash(),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if existing is not None and existing.status == "completed":
        return ClaimedIndex(existing.id, True)
    if existing is None:
        existing = RetrievalIndex(
            snapshot_id=chunk_set.snapshot_id,
            chunk_set_id=chunk_set_id,
            profile_hash=profile.hash(),
            profile=profile.describe(),
            status="building",
            owner_token=owner_token,
        )
        session.add(existing)
        session.flush()
    else:
        session.execute(delete(RetrievalVector).where(RetrievalVector.index_id == existing.id))
        existing.status = "building"
        existing.owner_token = owner_token
        existing.segment_count = 0
        existing.completed_at = None
        existing.profile = profile.describe()
        session.flush()
    return ClaimedIndex(existing.id, False)


def _owned_index(session: Session, index_id: int, owner_token: UUID) -> RetrievalIndex:
    index = session.execute(
        select(RetrievalIndex)
        .where(RetrievalIndex.id == index_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if index is None or index.status != "building" or index.owner_token != owner_token:
        raise JobLeaseLostError("The retrieval index owner has changed.")
    return index


def write_segments(
    session: Session, *, index_id: int, owner_token: UUID, segments: list[VectorSegment]
) -> None:
    """寫入一批受柵欄保護的向量；不在此提交交易。"""
    index = _owned_index(session, index_id, owner_token)
    if len(segments) > 1000:
        raise ValueError("The retrieval vector batch is too large.")
    if not segments:
        return
    chunk_ids = {segment.chunk_id for segment in segments}
    lengths = dict(
        session.execute(
            select(
                RetrievalChunk.id, RetrievalChunk.end_offset - RetrievalChunk.start_offset
            ).where(
                RetrievalChunk.chunk_set_id == index.chunk_set_id, RetrievalChunk.id.in_(chunk_ids)
            )
        ).all()
    )
    rows = []
    for segment in segments:
        length = lengths.get(segment.chunk_id)
        if (
            length is None
            or segment.ordinal < 0
            or segment.start_offset < 0
            or segment.end_offset <= segment.start_offset
            or segment.end_offset > length
            or not 1 <= segment.token_count <= 512
        ):
            raise ValueError("A retrieval vector segment is invalid.")
        if len(segment.embedding) != 768:
            raise ValueError("A retrieval vector has an invalid dimension.")
        try:
            vector = [float(value) for value in segment.embedding]
        except (OverflowError, TypeError, ValueError):
            raise ValueError("A retrieval vector contains an invalid value.") from None
        if any(isinstance(value, bool) for value in segment.embedding) or not all(
            math.isfinite(value) for value in vector
        ):
            raise ValueError("A retrieval vector contains an invalid value.")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or abs(norm - 1.0) > 0.0001:
            raise ValueError("A retrieval vector is not normalized.")
        rows.append(
            {
                "index_id": index_id,
                "chunk_set_id": index.chunk_set_id,
                "chunk_id": segment.chunk_id,
                "ordinal": segment.ordinal,
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "token_count": segment.token_count,
                "embedding": vector,
            }
        )
    session.execute(insert(RetrievalVector), rows)


def fail_index(session: Session, *, index_id: int, owner_token: UUID) -> None:
    """標記建構失敗；後續持鎖接手時會清除未完成的向量。"""
    index = _owned_index(session, index_id, owner_token)
    index.status = "failed"
    index.owner_token = None
    session.flush()


def complete_index(
    session: Session, *, index_id: int, owner_token: UUID, lock: AdvisoryLock
) -> int:
    """確認每個來源片段被連續涵蓋，並在呼叫者交易中發布。"""
    index = _owned_index(session, index_id, owner_token)
    if lock.shared or lock.key != lock_key(
        "retrieval", f"{index.chunk_set_id}:{index.profile_hash}"
    ):
        raise ValueError("The retrieval index lock does not match.")
    lock.assert_alive()
    chunk_set = session.execute(
        select(RetrievalChunkSet)
        .where(RetrievalChunkSet.id == index.chunk_set_id)
        .with_for_update(read=True)
    ).scalar_one()
    rows = session.execute(
        select(
            RetrievalChunk.id,
            RetrievalChunk.end_offset - RetrievalChunk.start_offset,
            RetrievalVector.ordinal,
            RetrievalVector.start_offset,
            RetrievalVector.end_offset,
        )
        .outerjoin(
            RetrievalVector,
            and_(
                RetrievalVector.chunk_id == RetrievalChunk.id,
                RetrievalVector.index_id == index_id,
                RetrievalVector.chunk_set_id == index.chunk_set_id,
            ),
        )
        .where(RetrievalChunk.chunk_set_id == index.chunk_set_id)
        .order_by(RetrievalChunk.id, RetrievalVector.ordinal)
        .execution_options(yield_per=1000)
    )
    current_chunk: int | None = None
    current_length = offset = expected_ordinal = chunk_count = segment_count = 0
    for chunk_id, length, ordinal, start, end in rows:
        if chunk_id != current_chunk:
            if current_chunk is not None and offset != current_length:
                raise ValueError("The retrieval index does not cover every source chunk.")
            current_chunk, current_length = chunk_id, length
            offset = expected_ordinal = 0
            chunk_count += 1
        if ordinal != expected_ordinal or start != offset or end is None or end <= offset:
            raise ValueError("The retrieval index does not cover every source chunk.")
        offset = end
        expected_ordinal += 1
        segment_count += 1
    if (
        current_chunk is not None and offset != current_length
    ) or chunk_count != chunk_set.chunk_count:
        raise ValueError("The retrieval index does not cover every source chunk.")
    index.status = "completed"
    index.owner_token = None
    index.completed_at = datetime.now(UTC)
    index.segment_count = segment_count
    session.flush()
    return segment_count
