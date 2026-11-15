"""提交檢索索引工作，固定來源結構索引與 embedding 設定。"""

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Job, RetrievalChunkSet, RetrievalIndex, Snapshot
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import SnapshotNotCompletedError, SnapshotNotFoundError
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
    chunk_hash = chunk_profile_hash(CHUNK_MAX_BYTES, structure_id)
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
    session: Session, snapshot_id: int, settings: Settings, *, request_id: str | None = None
) -> RetrievalJobSubmission:
    identity = current_identity(session, snapshot_id, settings)
    params = {
        "snapshot_id": snapshot_id,
        "structure_index_id": identity.structure_id,
        "embedding_profile_hash": identity.profile_hash,
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
