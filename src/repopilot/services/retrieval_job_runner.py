"""背景工作中建構檢索索引，使用租約與鎖保護發布。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import LockBusyError, acquire_advisory_lock, repo_lock_key
from repopilot.db.models import Job, Snapshot
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_client import SocketEmbeddingClient
from repopilot.errors import (
    EmbeddingNotConfiguredError,
    ImportCancelledError,
    ProfileMismatchError,
    SnapshotNotCompletedError,
    SnapshotNotFoundError,
)
from repopilot.services import jobs
from repopilot.services.repositories import get_repository, repository_key
from repopilot.services.retrieval_build import RetrievalSummary, build_retrieval_index
from repopilot.services.retrieval_chunks import build_chunk_set
from repopilot.services.retrieval_jobs import CHUNK_MAX_BYTES
from repopilot.services.retrieval_vectors import retrieval_lock_key


def run_retrieval(
    session: Session,
    job: Job,
    token: UUID,
    engine: Engine,
    settings: Settings,
    heartbeat_checkpoint: Callable[[], None],
    finished: dict[str, str],
) -> None:
    job_id = job.id
    snapshot_id = job.snapshot_id
    if snapshot_id is None:
        raise SnapshotNotFoundError("The job has no snapshot.")
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.repository_id != job.repository_id:
        raise SnapshotNotFoundError("Snapshot not found.")
    if snapshot.status != "completed":
        raise SnapshotNotCompletedError("The snapshot has not been imported completely.")
    repository = get_repository(session, snapshot.repository_id)
    repo_key = repository_key(repository)
    profile = EmbeddingProfile.from_settings(settings)
    if job.params.get("embedding_profile_hash") != profile.hash():
        raise ProfileMismatchError("The worker's embedding settings differ from the API's.")
    structure_id = job.params.get("structure_index_id")
    if structure_id is not None and (type(structure_id) is not int or structure_id < 1):
        raise ProfileMismatchError("The job has an invalid structure index.")
    if settings.embedding_socket_path is None:
        raise EmbeddingNotConfiguredError("The isolated embedding socket is not configured.")
    session.commit()

    def checkpoint() -> None:
        heartbeat_checkpoint()
        with Session(engine) as probe:
            if jobs.read_cancel_requested(probe, job_id, token):
                raise ImportCancelledError("The job was cancelled.")

    checkpoint()
    repo_lock = acquire_advisory_lock(engine, repo_lock_key(repo_key), shared=True)
    if repo_lock is None:
        raise LockBusyError
    try:
        client = SocketEmbeddingClient(socket_path=settings.embedding_socket_path, profile=profile)
        client.verify_identity()
        jobs.update_progress(session, job_id, token, phase="embedding")
        checkpoint()
        chunks = build_chunk_set(
            session,
            snapshot_id,
            max_bytes=CHUNK_MAX_BYTES,
            structure_index_id=structure_id,
            checkpoint=checkpoint,
        )
        repo_lock.assert_alive()
        checkpoint()
        session.commit()
        index_lock = acquire_advisory_lock(engine, retrieval_lock_key(chunks.chunk_set_id, profile))
        if index_lock is None:
            raise LockBusyError
        try:

            def result(summary: RetrievalSummary) -> dict[str, Any]:
                return {
                    "snapshot_id": snapshot_id,
                    "index_id": summary.index_id,
                    "segment_count": summary.segment_count,
                    "index_reused": summary.reused,
                }

            def finalize(summary: RetrievalSummary, publish: Callable[[Session], None]) -> bool:
                repo_lock.assert_alive()
                done = jobs.complete_job(
                    session,
                    job_id,
                    token,
                    snapshot_id=snapshot_id,
                    result=result(summary),
                    before_publish=publish,
                )
                finished["status"] = done.status
                return done.status == "succeeded"

            summary = build_retrieval_index(
                session,
                chunk_set_id=chunks.chunk_set_id,
                owner_token=token,
                profile=profile,
                lock=index_lock,
                count_tokens=client.count_tokens,
                embed=client.embed,
                checkpoint=checkpoint,
                finalize=finalize,
            )
            if "status" not in finished:
                checkpoint()
                repo_lock.assert_alive()
                index_lock.assert_alive()
                done = jobs.complete_job(
                    session, job_id, token, snapshot_id=snapshot_id, result=result(summary)
                )
                finished["status"] = done.status
                if done.status == "cancelled":
                    raise ImportCancelledError("The job was cancelled.")
        finally:
            index_lock.release()
    finally:
        repo_lock.release()
