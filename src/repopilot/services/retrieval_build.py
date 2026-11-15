"""快照片段到 embedding 的建構工具。"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from repopilot.db.locks import AdvisoryLock
from repopilot.db.models import RetrievalChunk, RetrievalIndex, Snapshot, SnapshotFile
from repopilot.embedding.chunking import split_for_embedding
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import ImportCancelledError, JobLeaseLostError, SnapshotNotCompletedError
from repopilot.services.retrieval_vectors import (
    VectorSegment,
    claim_index,
    complete_index,
    fail_index,
    write_segments,
)


@dataclass(frozen=True)
class SourceChunk:
    chunk_id: int
    content: str


@dataclass(frozen=True)
class RetrievalSummary:
    index_id: int
    reused: bool
    segment_count: int


def build_retrieval_index(
    session: Session,
    *,
    chunk_set_id: int,
    owner_token: UUID,
    profile: EmbeddingProfile,
    lock: AdvisoryLock,
    count_tokens: Callable[[str], int],
    embed: Callable[[str], list[float]],
    checkpoint: Callable[[], None],
    finalize: Callable[[RetrievalSummary, Callable[[Session], None]], bool],
) -> RetrievalSummary:
    """建構索引並委由呼叫者在同一交易發布索引與工作狀態。"""
    checkpoint()
    claimed = claim_index(
        session, chunk_set_id=chunk_set_id, profile=profile, owner_token=owner_token, lock=lock
    )
    index = session.get(RetrievalIndex, claimed.index_id)
    if index is None:
        raise JobLeaseLostError("The retrieval index no longer exists.")
    previous_count = index.segment_count
    session.commit()
    if claimed.reused:
        return RetrievalSummary(claimed.index_id, True, previous_count)

    pending: list[VectorSegment] = []
    count = 0
    cursor = 0
    try:
        while True:
            checkpoint()
            chunks = load_source_chunks(
                session, index_id=claimed.index_id, owner_token=owner_token, after_chunk_id=cursor
            )
            session.rollback()
            if not chunks:
                break
            for chunk in chunks:
                for vector in vectorize_chunk(
                    chunk, count_tokens=count_tokens, embed=embed, checkpoint=checkpoint
                ):
                    pending.append(vector)
                    count += 1
                    if len(pending) == 64:
                        checkpoint()
                        write_segments(
                            session,
                            index_id=claimed.index_id,
                            owner_token=owner_token,
                            segments=pending,
                        )
                        session.commit()
                        pending.clear()
                cursor = chunk.chunk_id
        if pending:
            checkpoint()
            write_segments(
                session, index_id=claimed.index_id, owner_token=owner_token, segments=pending
            )
            session.commit()
        checkpoint()
        lock.assert_alive()
        summary = RetrievalSummary(claimed.index_id, False, count)
        published = False

        def publish(transaction: Session) -> None:
            nonlocal published
            actual = complete_index(
                transaction, index_id=claimed.index_id, owner_token=owner_token, lock=lock
            )
            if actual != count:
                raise ValueError("The retrieval vector count changed during publication.")
            published = True

        if not finalize(summary, publish):
            raise ImportCancelledError("The retrieval index build was cancelled.")
        if not published:
            raise RuntimeError("The retrieval index finalizer did not publish the index.")
        return summary
    except JobLeaseLostError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        fail_index(session, index_id=claimed.index_id, owner_token=owner_token)
        session.commit()
        raise


def load_source_chunks(
    session: Session,
    *,
    index_id: int,
    owner_token: UUID,
    after_chunk_id: int = 0,
    limit: int = 1,
) -> list[SourceChunk]:
    """讀取已匯入的片段文字並核對雜湊；使用 keyset 分頁。"""
    if after_chunk_id < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid source chunk pagination.")
    row = session.execute(
        select(RetrievalIndex, Snapshot.status)
        .join(Snapshot, Snapshot.id == RetrievalIndex.snapshot_id)
        .where(RetrievalIndex.id == index_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None or row[0].status != "building" or row[0].owner_token != owner_token:
        raise JobLeaseLostError("The retrieval index owner has changed.")
    if row[1] != "completed":
        raise SnapshotNotCompletedError("The snapshot has not been imported completely.")
    index = row[0]
    rows = session.execute(
        select(
            RetrievalChunk.id,
            RetrievalChunk.content_hash,
            RetrievalChunk.end_offset - RetrievalChunk.start_offset,
            SnapshotFile.status,
            func.substr(
                SnapshotFile.content,
                RetrievalChunk.start_offset + 1,
                RetrievalChunk.end_offset - RetrievalChunk.start_offset,
            ),
        )
        .join(
            SnapshotFile,
            and_(
                SnapshotFile.id == RetrievalChunk.snapshot_file_id,
                SnapshotFile.snapshot_id == RetrievalChunk.snapshot_id,
            ),
        )
        .where(
            RetrievalChunk.chunk_set_id == index.chunk_set_id,
            RetrievalChunk.snapshot_id == index.snapshot_id,
            RetrievalChunk.id > after_chunk_id,
        )
        .order_by(RetrievalChunk.id)
        .limit(limit)
    )
    chunks = []
    for chunk_id, expected_hash, length, status, content in rows:
        if (
            status != "imported"
            or not isinstance(content, str)
            or len(content) != length
            or sha256(content.encode("utf-8")).hexdigest() != expected_hash
        ):
            raise ValueError("A retrieval chunk no longer matches its snapshot source.")
        chunks.append(SourceChunk(chunk_id=chunk_id, content=content))
    return chunks


def vectorize_chunk(
    chunk: SourceChunk,
    *,
    count_tokens: Callable[[str], int],
    embed: Callable[[str], list[float]],
    checkpoint: Callable[[], None],
) -> Iterator[VectorSegment]:
    """使用實際 tokenizer 切分；每次模型呼叫前檢查取消或租約。"""

    def checked_count(value: str) -> int:
        checkpoint()
        return count_tokens(value)

    pieces = split_for_embedding(
        chunk.content, count_tokens=checked_count, max_tokens=512, max_bytes=32768
    )
    for ordinal, piece in enumerate(pieces):
        checkpoint()
        embedding = embed(chunk.content[piece.start : piece.end])
        yield VectorSegment(
            chunk_id=chunk.chunk_id,
            ordinal=ordinal,
            start_offset=piece.start,
            end_offset=piece.end,
            token_count=piece.token_count,
            embedding=embedding,
        )
