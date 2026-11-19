"""具所有權柵欄的檢索向量儲存。"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, delete, insert, literal, select, text
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
    # 續跑：接手未完成的索引時，保留下來的（向量區間完整涵蓋原文的）片段；新建索引時為空
    covered_chunk_ids: frozenset[int] = frozenset()
    covered_chars: int = 0
    covered_vectors: int = 0


def retrieval_lock_key(chunk_set_id: int, profile: EmbeddingProfile) -> int:
    return lock_key("retrieval", f"{chunk_set_id}:{profile.hash()}")


_CLEAR_BATCH = 1000

# 每個有向量的片段：向量的序號從 0 連續、區間從 0 開始首尾相接。涵蓋到原文結尾才算完整
# （規則與 complete_index 的發布檢查相同）。
_COVERAGE_SQL = text(
    """
    WITH v AS (
        SELECT chunk_id, ordinal, start_offset, end_offset,
               row_number() OVER w - 1 AS expected_ordinal,
               coalesce(lag(end_offset) OVER w, 0) AS expected_start
        FROM retrieval_vectors
        WHERE index_id = :index_id
        WINDOW w AS (PARTITION BY chunk_id ORDER BY ordinal)
    )
    SELECT v.chunk_id,
           c.end_offset - c.start_offset AS length,
           bool_and(v.ordinal = v.expected_ordinal AND v.start_offset = v.expected_start)
               AS contiguous,
           max(v.end_offset) AS covered_to,
           count(*) AS vectors
    FROM v
    JOIN retrieval_chunks c ON c.id = v.chunk_id
    GROUP BY v.chunk_id, c.end_offset, c.start_offset
    """
)


def _keep_covered_chunks(session: Session, *, index_id: int) -> tuple[frozenset[int], int, int]:
    """續跑前整理既有向量：保留完整涵蓋的片段，清除不完整片段的向量。

    回傳 (保留的片段 ID, 其原文字元數, 向量數)。呼叫者持有索引列鎖與專用鎖，所以舊擁有者
    的寫入不會在計算與清除之間插入（它們會被擁有者柵欄擋下）。
    """
    covered: set[int] = set()
    partial: list[int] = []
    chars = vectors = 0
    for chunk_id, length, contiguous, covered_to, count in session.execute(
        _COVERAGE_SQL, {"index_id": index_id}
    ):
        if contiguous and covered_to == length:
            covered.add(chunk_id)
            chars += length
            vectors += count
        else:
            partial.append(chunk_id)
    for start in range(0, len(partial), _CLEAR_BATCH):
        session.execute(
            delete(RetrievalVector).where(
                RetrievalVector.index_id == index_id,
                RetrievalVector.chunk_id.in_(partial[start : start + _CLEAR_BATCH]),
            )
        )
    return frozenset(covered), chars, vectors


def claim_index(
    session: Session,
    *,
    chunk_set_id: int,
    profile: EmbeddingProfile,
    owner_token: UUID,
    lock: AdvisoryLock,
) -> ClaimedIndex:
    """取得、重用或接手索引；呼叫者持有專用鎖，並負責提交交易。

    接手未完成（`failed`，或擁有者已消失的 `building`）的同設定索引時續跑：保留完整涵蓋
    的片段向量，只清除不完整片段的向量；設定（`profile_hash`）不同者是不同的索引，不會混用。
    """
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
    covered: frozenset[int] = frozenset()
    covered_chars = covered_vectors = 0
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
        covered, covered_chars, covered_vectors = _keep_covered_chunks(
            session, index_id=existing.id
        )
        existing.status = "building"
        existing.owner_token = owner_token
        existing.segment_count = covered_vectors
        existing.completed_at = None
        existing.profile = profile.describe()
        session.flush()
    return ClaimedIndex(existing.id, False, covered, covered_chars, covered_vectors)


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


def copy_matching_vectors(
    session: Session,
    *,
    index_id: int,
    owner_token: UUID,
    chunk_id: int,
    profile: EmbeddingProfile,
) -> int:
    """重用相同內容的向量：從任一「同 embedding 設定、已完成」的索引複製同雜湊片段的向量。

    向量只由片段文字與 embedding 設定（模型、執行環境、切分演算法與長度上限皆在
    `profile_hash` 內）決定，與片段所屬的快照或 repo 無關，所以可跨快照與 repo 複製；複製的是
    資料列（不是參照），來源快照之後被移除也不影響本索引。來源索引已通過發布時的涵蓋檢查。
    回傳複製的向量數，找不到可用來源時為 0（呼叫者改為呼叫模型）。不提交交易。
    """
    index = _owned_index(session, index_id, owner_token)
    target = session.execute(
        select(
            RetrievalChunk.content_hash, RetrievalChunk.end_offset - RetrievalChunk.start_offset
        ).where(RetrievalChunk.id == chunk_id, RetrievalChunk.chunk_set_id == index.chunk_set_id)
    ).one_or_none()
    if target is None:
        raise ValueError("The retrieval chunk does not belong to this index.")
    content_hash, length = target
    source = session.execute(
        select(RetrievalChunk.id, RetrievalIndex.id)
        .join(RetrievalIndex, RetrievalIndex.chunk_set_id == RetrievalChunk.chunk_set_id)
        .where(
            RetrievalChunk.content_hash == content_hash,
            RetrievalChunk.end_offset - RetrievalChunk.start_offset == length,
            RetrievalIndex.profile_hash == profile.hash(),
            RetrievalIndex.status == "completed",
        )
        .order_by(RetrievalChunk.id)
        .limit(1)
    ).one_or_none()
    if source is None:
        return 0
    source_chunk_id, source_index_id = source
    result = session.execute(
        insert(RetrievalVector)
        .from_select(
            [
                "index_id",
                "chunk_set_id",
                "chunk_id",
                "ordinal",
                "start_offset",
                "end_offset",
                "token_count",
                "embedding",
            ],
            select(
                literal(index_id),
                literal(index.chunk_set_id),
                literal(chunk_id),
                RetrievalVector.ordinal,
                RetrievalVector.start_offset,
                RetrievalVector.end_offset,
                RetrievalVector.token_count,
                RetrievalVector.embedding,
            )
            .where(
                RetrievalVector.index_id == source_index_id,
                RetrievalVector.chunk_id == source_chunk_id,
            )
            .order_by(RetrievalVector.ordinal),
        )
        .returning(RetrievalVector.id)
    )
    copied = len(result.all())
    if copied < 1:
        raise ValueError("The reusable retrieval vectors disappeared.")
    return copied


def fail_index(session: Session, *, index_id: int, owner_token: UUID) -> None:
    """標記建構失敗；之後同設定的重試會接手並續跑（保留完整涵蓋的片段向量）。"""
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
