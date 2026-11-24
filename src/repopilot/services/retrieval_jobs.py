"""提交檢索索引工作，固定來源結構索引與 embedding 設定。"""

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job, RetrievalChunkSet, RetrievalIndex, Snapshot
from repopilot.embedding.devices import DEFAULT_DEVICE, EmbeddingDevice, ensure_available
from repopilot.embedding.local_runtime import EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import (
    EmbeddingDeviceUnavailableError,
    EmbeddingNotConfiguredError,
    SnapshotNotCompletedError,
    SnapshotNotFoundError,
)
from repopilot.indexing.file_kinds import RETRIEVAL_EXCLUDED_KINDS
from repopilot.services import jobs
from repopilot.services.import_jobs import find_active_job, latest_job
from repopilot.services.index_queries import get_current_index
from repopilot.services.repositories import get_repository, repository_key
from repopilot.services.retrieval_chunks import chunk_profile_hash

CHUNK_MAX_BYTES = 32768


@dataclass(frozen=True)
class RetrievalJobSubmission:
    job: Job | None
    snapshot_id: int
    repository_id: int
    reused: bool


@dataclass(frozen=True)
class RetrievalIdentity:
    snapshot_id: int
    repository_id: int
    repo_key: str
    profile_hash: str
    chunk_hash: str
    structure_id: int | None
    dedupe_key: str


def current_identity(session: Session, snapshot_id: int, settings: Settings) -> RetrievalIdentity:
    snapshot = session.get(Snapshot, snapshot_id, populate_existing=True)
    if snapshot is None:
        raise SnapshotNotFoundError("Snapshot not found.")
    if snapshot.status != "completed":
        raise SnapshotNotCompletedError("The snapshot has not been imported completely.")
    repository = get_repository(session, snapshot.repository_id)
    repository_id = repository.id
    repo_key = repository_key(repository)
    profile_hash = EmbeddingProfile.from_settings(settings).hash()
    structure = get_current_index(session, snapshot_id, settings)
    structure_id = (
        structure.id if structure is not None and structure.status == "completed" else None
    )
    chunk_hash = chunk_profile_hash(CHUNK_MAX_BYTES, structure_id, RETRIEVAL_EXCLUDED_KINDS)
    key_hash = sha256(f"{chunk_hash}:{profile_hash}".encode("ascii")).hexdigest()
    return RetrievalIdentity(
        snapshot_id,
        repository_id,
        repo_key,
        profile_hash,
        chunk_hash,
        structure_id,
        f"retrieval:{snapshot_id}:{key_hash}",
    )


def current_index(
    session: Session, identity: RetrievalIdentity
) -> tuple[RetrievalIndex, RetrievalChunkSet] | None:
    row = session.execute(
        select(RetrievalIndex, RetrievalChunkSet)
        .join(RetrievalChunkSet, RetrievalIndex.chunk_set_id == RetrievalChunkSet.id)
        .where(
            RetrievalChunkSet.snapshot_id == identity.snapshot_id,
            RetrievalChunkSet.profile_hash == identity.chunk_hash,
            RetrievalIndex.profile_hash == identity.profile_hash,
        )
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None


def submit_retrieval_job(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    request_id: str | None = None,
    device: EmbeddingDevice = DEFAULT_DEVICE,
) -> RetrievalJobSubmission:
    """提交檢索索引工作。`device` 只決定這個工作使用哪個 runtime，不影響去重與設定識別：
    已有進行中的工作時回傳它（其裝置不變）；失敗或取消後重新提交會以新的裝置續跑已保存的片段。"""
    ensure_available(settings, device)
    identity = current_identity(session, snapshot_id, settings)
    params = {
        "snapshot_id": snapshot_id,
        "structure_index_id": identity.structure_id,
        "embedding_profile_hash": identity.profile_hash,
        "device": device,
    }
    session.rollback()

    for _ in range(5):
        active = find_active_job(session, identity.dedupe_key)
        if active is not None:
            return RetrievalJobSubmission(active, snapshot_id, identity.repository_id, False)
        current = current_index(session, identity)
        if current is not None and current[0].status == "completed":
            return RetrievalJobSubmission(None, snapshot_id, identity.repository_id, True)
        latest = latest_job(session, identity.dedupe_key)
        if latest is not None and latest.status in ("failed", "cancelled"):
            resubmitted = jobs.resubmit_job(
                session, latest.id, settings, request_id=request_id, params=params
            )
            if resubmitted is not None:
                return RetrievalJobSubmission(
                    resubmitted, snapshot_id, identity.repository_id, False
                )
            continue
        created = jobs.create_job(
            session,
            kind="snapshot_retrieval",
            dedupe_key=identity.dedupe_key,
            settings=settings,
            repository_id=identity.repository_id,
            snapshot_id=snapshot_id,
            repo_key=identity.repo_key,
            params=params,
            request_id=request_id,
        )
        return RetrievalJobSubmission(created.job, snapshot_id, identity.repository_id, False)
    raise RuntimeError("Could not submit the retrieval job.")


def submit_with_gpu_preference(
    session: Session, snapshot_id: int, settings: Settings, *, request_id: str | None = None
) -> tuple[RetrievalJobSubmission, bool]:
    """GPU 優先送出；送出時 GPU 不可用才改用 CPU。回傳 (結果, 是否發生改用 CPU)。

    設定參考版本（#25）與結構索引完成後補送（#26）共用。工作開始後的失敗不在這裡處理。
    """
    try:
        return submit_retrieval_job(
            session, snapshot_id, settings, request_id=request_id, device="gpu"
        ), False
    except (EmbeddingDeviceUnavailableError, EmbeddingNotConfiguredError, EmbeddingRuntimeError):
        session.rollback()
        return submit_retrieval_job(
            session, snapshot_id, settings, request_id=request_id, device="cpu"
        ), True
