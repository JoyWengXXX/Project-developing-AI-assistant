"""快照片段到 embedding 的建構工具。"""

import time
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
    copy_matching_vectors,
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
    # 本次執行以「相同內容的既有向量」複製完成的片段數（跨快照、跨 repo；見 copy_matching_vectors）
    reused_chunks: int = 0


@dataclass(frozen=True)
class BuildProgress:
    """建構中的進度快照。字元數是片段原文的字元數（片段區間以字元計），供估計剩餘時間。"""

    chunks_done: int
    chunks_total: int
    vectors: int
    chars_done: int
    chars_total: int
    run_seconds: float
    # 估計速率只看本次「實際呼叫模型」處理的字元：續跑前已涵蓋與本次重用的字元都算在 base_chars
    base_chars: int = 0
    reused_chunks: int = 0


class SliceExhausted(Exception):  # noqa: N818 - 控制流程訊號，不是錯誤
    """本次執行的時間切片用完：已完成的片段都已寫入，工作應放回佇列接著做。

    只在完成至少一個新片段後、且還有片段未完成時丟出，所以每一段都有進展，不會無限排隊。
    """


PROGRESS_INTERVAL_SECONDS = 2.0


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
    on_progress: Callable[[BuildProgress], None] | None = None,
    progress_interval_seconds: float = PROGRESS_INTERVAL_SECONDS,
    time_slice_seconds: float | None = None,
) -> RetrievalSummary:
    """建構索引並委由呼叫者在同一交易發布索引與工作狀態。

    `on_progress` 在開始時、之後最多每 `progress_interval_seconds` 秒一次（每個片段結束時判斷），
    以及最後一個片段結束時呼叫；呼叫時沒有未提交的寫入，回呼可以提交自己的交易。
    `time_slice_seconds` 不為 None 時，本次執行超過這段時間（在片段結束時判斷）且還有片段未完成，
    就回報最新進度並丟出 `SliceExhausted`；索引維持 `building`，下一次接手時續跑。
    """
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
    covered = claimed.covered_chunk_ids
    count = claimed.covered_vectors
    cursor = 0
    chunks_done = len(covered)
    chars_done = claimed.covered_chars
    embedded_chars = reused_chunks = 0
    started = last_report = time.monotonic()
    try:
        chunks_total, chars_total = session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(RetrievalChunk.end_offset - RetrievalChunk.start_offset), 0),
            ).where(RetrievalChunk.chunk_set_id == chunk_set_id)
        ).one()
        if covered:
            # 已完整涵蓋的前綴不必再載入原文；前綴之後零星已涵蓋的片段在下面略過
            for chunk_id in session.scalars(
                select(RetrievalChunk.id)
                .where(RetrievalChunk.chunk_set_id == chunk_set_id)
                .order_by(RetrievalChunk.id)
            ):
                if chunk_id not in covered:
                    break
                cursor = chunk_id
        session.rollback()

        def report(*, force: bool) -> None:
            nonlocal last_report
            if on_progress is None:
                return
            now = time.monotonic()
            if not force and now - last_report < progress_interval_seconds:
                return
            last_report = now
            on_progress(
                BuildProgress(
                    chunks_done=chunks_done,
                    chunks_total=chunks_total,
                    vectors=count,
                    chars_done=chars_done,
                    chars_total=int(chars_total),
                    run_seconds=now - started,
                    base_chars=chars_done - embedded_chars,
                    reused_chunks=reused_chunks,
                )
            )

        def flush() -> None:
            if not pending:
                return
            checkpoint()
            write_segments(
                session, index_id=claimed.index_id, owner_token=owner_token, segments=pending
            )
            session.commit()
            pending.clear()

        report(force=True)
        while True:
            checkpoint()
            chunks = load_source_chunks(
                session, index_id=claimed.index_id, owner_token=owner_token, after_chunk_id=cursor
            )
            session.rollback()
            if not chunks:
                break
            for chunk in chunks:
                cursor = chunk.chunk_id
                if chunk.chunk_id in covered:
                    continue
                copied = copy_matching_vectors(
                    session,
                    index_id=claimed.index_id,
                    owner_token=owner_token,
                    chunk_id=chunk.chunk_id,
                    profile=profile,
                )
                if copied:
                    session.commit()
                    count += copied
                    reused_chunks += 1
                else:
                    # 查詢（含索引列鎖）沒有結果：結束交易，不讓鎖跨過耗時的模型呼叫
                    session.rollback()
                    for vector in vectorize_chunk(
                        chunk, count_tokens=count_tokens, embed=embed, checkpoint=checkpoint
                    ):
                        pending.append(vector)
                        count += 1
                        if len(pending) == 64:
                            flush()
                    # 每個片段結束就寫入並提交：中斷時，已完成的片段才能在續跑時保留
                    flush()
                    embedded_chars += len(chunk.content)
                chunks_done += 1
                chars_done += len(chunk.content)
                report(force=chunks_done == chunks_total)
                if (
                    time_slice_seconds is not None
                    and chunks_done < chunks_total
                    and time.monotonic() - started >= time_slice_seconds
                ):
                    report(force=True)
                    raise SliceExhausted
        flush()
        checkpoint()
        lock.assert_alive()
        summary = RetrievalSummary(claimed.index_id, False, count, reused_chunks)
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
    except (JobLeaseLostError, SliceExhausted):
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
