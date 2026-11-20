"""快照片段集合；只有呼叫端交易提交後才可見。"""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from repopilot.db.models import (
    IndexedFile,
    RetrievalChunk,
    RetrievalChunkSet,
    Snapshot,
    SnapshotFile,
    SnapshotIndex,
    Symbol,
)
from repopilot.errors import (
    SnapshotIndexNotReadyError,
    SnapshotNotCompletedError,
    SnapshotNotFoundError,
)
from repopilot.indexing.retrieval_chunking import chunk_text

_ALGORITHM = "retrieval-chunks-v1"
_FILE_BATCH_SIZE = 100
_FILE_BATCH_MAX_BYTES = 8 * 1024 * 1024
_INSERT_BATCH_SIZE = 500


def chunk_profile_hash(
    max_bytes: int, structure_index_id: int | None, excluded_kinds: tuple[str, ...] | None = None
) -> str:
    excluded_key = ""
    if excluded_kinds:
        # 排序以確保一致性
        excluded_key = ":" + ",".join(sorted(set(excluded_kinds)))
    full_key = f"{_ALGORITHM}:{max_bytes}:{structure_index_id}{excluded_key}"
    return sha256(full_key.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ChunkSetSummary:
    chunk_set_id: int
    reused: bool
    chunk_count: int


def build_chunk_set(
    session: Session,
    snapshot_id: int,
    *,
    max_bytes: int,
    structure_index_id: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> ChunkSetSummary:
    """建立完整片段集合；失敗回滾至 savepoint，由呼叫者決定是否提交外層交易。"""
    with session.begin_nested():
        return _build_chunk_set(
            session,
            snapshot_id,
            max_bytes=max_bytes,
            structure_index_id=structure_index_id,
            checkpoint=checkpoint,
        )


def _build_chunk_set(
    session: Session,
    snapshot_id: int,
    *,
    max_bytes: int,
    structure_index_id: int | None,
    checkpoint: Callable[[], None] | None,
) -> ChunkSetSummary:
    if not 4 <= max_bytes <= 2_147_483_647:
        raise ValueError("max_bytes is outside the supported range.")
    snapshot = session.execute(
        select(Snapshot).where(Snapshot.id == snapshot_id).with_for_update(read=True)
    ).scalar_one_or_none()
    if snapshot is None:
        raise SnapshotNotFoundError("Snapshot not found.")
    if snapshot.status != "completed":
        raise SnapshotNotCompletedError("The snapshot has not been imported completely.")

    structure_profile_hash = None
    if structure_index_id is not None:
        structure_index = session.execute(
            select(SnapshotIndex)
            .where(SnapshotIndex.id == structure_index_id, SnapshotIndex.snapshot_id == snapshot_id)
            .with_for_update(read=True)
        ).scalar_one_or_none()
        if structure_index is None or structure_index.status != "completed":
            raise SnapshotIndexNotReadyError(
                "The requested structure index is not ready.",
                index_status=structure_index.status if structure_index is not None else "none",
            )
        structure_profile_hash = structure_index.index_profile_hash

    profile_hash = chunk_profile_hash(max_bytes, structure_index_id)
    chunk_set_id = session.scalar(
        pg_insert(RetrievalChunkSet)
        .values(
            snapshot_id=snapshot_id,
            profile_hash=profile_hash,
            max_bytes=max_bytes,
            structure_index_id=structure_index_id,
            structure_profile_hash=structure_profile_hash,
            chunk_count=0,
        )
        .on_conflict_do_nothing(index_elements=["snapshot_id", "profile_hash"])
        .returning(RetrievalChunkSet.id)
    )
    if chunk_set_id is None:
        existing = session.execute(
            select(RetrievalChunkSet).where(
                RetrievalChunkSet.snapshot_id == snapshot_id,
                RetrievalChunkSet.profile_hash == profile_hash,
            )
        ).scalar_one()
        return ChunkSetSummary(existing.id, True, existing.chunk_count)

    pending: list[dict[str, Any]] = []
    count = 0
    last_file_id = 0
    while True:
        if checkpoint is not None:
            checkpoint()
        candidates = session.execute(
            select(SnapshotFile.id, SnapshotFile.byte_size)
            .where(
                SnapshotFile.snapshot_id == snapshot_id,
                SnapshotFile.status == "imported",
                SnapshotFile.id > last_file_id,
            )
            .order_by(SnapshotFile.id)
            .limit(_FILE_BATCH_SIZE)
        ).all()
        if not candidates:
            break
        file_ids: list[int] = []
        batch_bytes = 0
        for candidate in candidates:
            if candidate.byte_size is None:
                if not file_ids:
                    file_ids.append(candidate.id)
                break
            if file_ids and batch_bytes + candidate.byte_size > _FILE_BATCH_MAX_BYTES:
                break
            file_ids.append(candidate.id)
            batch_bytes += candidate.byte_size
        last_file_id = file_ids[-1]
        files = session.execute(
            select(SnapshotFile.id, SnapshotFile.content)
            .where(SnapshotFile.snapshot_id == snapshot_id, SnapshotFile.id.in_(file_ids))
            .order_by(SnapshotFile.id)
        ).all()
        boundaries: dict[int, list[int]] = {}
        if structure_index_id is not None:
            symbol_rows = session.execute(
                select(
                    IndexedFile.snapshot_file_id,
                    func.coalesce(Symbol.leading_start_line, Symbol.start_line),
                )
                .join(Symbol, Symbol.indexed_file_id == IndexedFile.id)
                .where(
                    IndexedFile.snapshot_index_id == structure_index_id,
                    IndexedFile.snapshot_file_id.in_(file_ids),
                    IndexedFile.parse_status != "failed",
                    Symbol.kind.not_in(("module", "namespace")),
                )
            )
            for file_id, start_line in symbol_rows:
                boundaries.setdefault(file_id, []).append(start_line)
        for file in files:
            if checkpoint is not None:
                checkpoint()
            if file.content is None:
                raise RuntimeError("An imported snapshot file has no content.")
            for ordinal, chunk in enumerate(
                chunk_text(
                    file.content,
                    max_bytes=max_bytes,
                    structure_lines=boundaries.get(file.id),
                )
            ):
                pending.append(
                    {
                        "snapshot_id": snapshot_id,
                        "chunk_set_id": chunk_set_id,
                        "snapshot_file_id": file.id,
                        "ordinal": ordinal,
                        "start_offset": chunk.start,
                        "end_offset": chunk.end,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "split_kind": chunk.split_kind,
                        "content_hash": sha256(
                            file.content[chunk.start : chunk.end].encode("utf-8")
                        ).hexdigest(),
                    }
                )
                count += 1
                if len(pending) >= _INSERT_BATCH_SIZE:
                    session.execute(insert(RetrievalChunk), pending)
                    pending.clear()
    if pending:
        session.execute(insert(RetrievalChunk), pending)
    session.execute(
        update(RetrievalChunkSet)
        .where(RetrievalChunkSet.id == chunk_set_id)
        .values(chunk_count=count)
    )
    return ChunkSetSummary(chunk_set_id, False, count)
