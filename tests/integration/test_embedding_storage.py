"""向量索引在 PostgreSQL 中保留片段歸屬及狀態約束。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from repopilot.db.locks import try_advisory_lock
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.errors import DatabaseUnavailableError, JobLeaseLostError
from repopilot.services.retrieval_build import load_source_chunks, vectorize_chunk
from repopilot.services.retrieval_chunks import build_chunk_set
from repopilot.services.retrieval_vectors import (
    VectorSegment,
    claim_index,
    complete_index,
    fail_index,
    retrieval_lock_key,
    write_segments,
)
from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files


@pytest.fixture
def chunk_set(db_engine: Engine, clean_repositories: None) -> tuple[int, int, int]:
    with db_engine.begin() as connection:
        repository_id = connection.scalar(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('vector-test', '/vector-test') RETURNING id"
            )
        )
    assert isinstance(repository_id, int)
    snapshot_id = add_snapshot(db_engine, repository_id)
    add_files(db_engine, snapshot_id, {"test.txt": "example source text"})
    with Session(db_engine) as session, session.begin():
        result = build_chunk_set(session, snapshot_id, max_bytes=24)
        chunk_id = session.scalar(
            text("SELECT id FROM retrieval_chunks WHERE chunk_set_id = :id LIMIT 1"),
            {"id": result.chunk_set_id},
        )
        assert isinstance(chunk_id, int)
    return snapshot_id, result.chunk_set_id, chunk_id


def insert_index(connection: Connection, snapshot: int, chunk_set_id: int, profile: str) -> int:
    value = connection.scalar(
        text(
            "INSERT INTO retrieval_indexes "
            "(snapshot_id, chunk_set_id, profile_hash, profile, status, owner_token) "
            "VALUES (:snapshot, :chunk_set, :profile, '{}', 'building', :owner) RETURNING id"
        ),
        {"snapshot": snapshot, "chunk_set": chunk_set_id, "profile": profile, "owner": uuid4()},
    )
    assert isinstance(value, int)
    return value


def insert_segment(connection: Connection, index_id: int, chunk_set_id: int, chunk_id: int) -> None:
    connection.execute(
        text(
            "INSERT INTO retrieval_vectors "
            "(index_id, chunk_set_id, chunk_id, ordinal, start_offset, end_offset, "
            "token_count, embedding) "
            "VALUES (:index, :chunk_set, :chunk, 0, 0, 1, 3, CAST(:vector AS vector))"
        ),
        {
            "index": index_id,
            "chunk_set": chunk_set_id,
            "chunk": chunk_id,
            "vector": "[" + ",".join(["1"] + ["0"] * 767) + "]",
        },
    )


def test_vector_round_trips_with_source_and_unit_distance(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, set_id, chunk_id = chunk_set
    with db_engine.begin() as connection:
        index_id = insert_index(connection, snapshot, set_id, "a" * 64)
        insert_segment(connection, index_id, set_id, chunk_id)
        row = connection.execute(
            text(
                "SELECT chunk_id, vector_dims(embedding), "
                "embedding <-> CAST(:vector AS vector) AS distance "
                "FROM retrieval_vectors WHERE index_id = :index"
            ),
            {"index": index_id, "vector": "[" + ",".join(["1"] + ["0"] * 767) + "]"},
        ).one()
    assert row == (chunk_id, 768, 0)


def test_index_rejects_chunk_set_from_another_snapshot(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, set_id, _ = chunk_set
    with db_engine.connect() as connection:
        repository_id = connection.scalar(
            text("SELECT repository_id FROM snapshots WHERE id = :id"), {"id": snapshot}
        )
    assert isinstance(repository_id, int)
    other_snapshot = add_snapshot(db_engine, repository_id, index=2)
    with db_engine.begin() as connection, pytest.raises(IntegrityError):
        insert_index(connection, other_snapshot, set_id, "a" * 64)


def test_vector_rejects_a_chunk_from_another_chunk_set(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, _, chunk_id = chunk_set
    with Session(db_engine) as session, session.begin():
        other = build_chunk_set(session, snapshot, max_bytes=32)
    with db_engine.begin() as connection:
        index_id = insert_index(connection, snapshot, other.chunk_set_id, "a" * 64)
    with db_engine.begin() as connection, pytest.raises(IntegrityError):
        insert_segment(connection, index_id, other.chunk_set_id, chunk_id)


def test_index_profile_is_unique_within_chunk_set(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, set_id, _ = chunk_set
    with db_engine.begin() as connection:
        insert_index(connection, snapshot, set_id, "a" * 64)
    with db_engine.begin() as connection, pytest.raises(IntegrityError):
        insert_index(connection, snapshot, set_id, "a" * 64)


def test_index_state_rejects_completed_without_completion_time(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, set_id, _ = chunk_set
    with db_engine.begin() as connection:
        index_id = insert_index(connection, snapshot, set_id, "a" * 64)
    with db_engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(
                "UPDATE retrieval_indexes SET status = 'completed', owner_token = NULL "
                "WHERE id = :id"
            ),
            {"id": index_id},
        )


def test_vector_dimension_is_enforced_by_database(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, set_id, chunk_id = chunk_set
    with db_engine.begin() as connection:
        index_id = insert_index(connection, snapshot, set_id, "a" * 64)
    with db_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text(
                "INSERT INTO retrieval_vectors "
                "(index_id, chunk_set_id, chunk_id, ordinal, start_offset, end_offset, "
                "token_count, embedding) "
                "VALUES (:index, :set, :chunk, 0, 0, 1, 3, '[1,0]'::vector)"
            ),
            {"index": index_id, "set": set_id, "chunk": chunk_id},
        )


def test_deleting_snapshot_cascades_its_indexes_and_vectors(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, set_id, chunk_id = chunk_set
    with db_engine.begin() as connection:
        index_id = insert_index(connection, snapshot, set_id, "a" * 64)
        insert_segment(connection, index_id, set_id, chunk_id)
        connection.execute(text("DELETE FROM snapshots WHERE id = :id"), {"id": snapshot})
        assert (
            connection.scalar(
                text("SELECT count(*) FROM retrieval_indexes WHERE id = :id"), {"id": index_id}
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM retrieval_vectors WHERE index_id = :id"),
                {"id": index_id},
            )
            == 0
        )


def test_migration_refuses_to_delete_existing_vector_indexes(
    db_engine: Engine, test_database_url: str, chunk_set: tuple[int, int, int]
) -> None:
    snapshot, set_id, chunk_id = chunk_set
    with db_engine.begin() as connection:
        index_id = insert_index(connection, snapshot, set_id, "a" * 64)
        insert_segment(connection, index_id, set_id, chunk_id)
    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        command.downgrade(alembic_config(test_database_url), "0015")
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM retrieval_vectors")) == 1


def test_complete_index_requires_full_coverage_and_reuses_published_index(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    vector = [1.0] + [0.0] * 767
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
            assert not index.reused
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[VectorSegment(chunk_id, 0, 0, 4, 3, vector)],
            )
        with Session(db_engine) as session, session.begin(), pytest.raises(ValueError):
            complete_index(session, index_id=index.index_id, owner_token=owner, lock=lock)
        with Session(db_engine) as session, session.begin():
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[VectorSegment(chunk_id, 1, 4, len("example source text"), 3, vector)],
            )
            count = complete_index(session, index_id=index.index_id, owner_token=owner, lock=lock)
            assert count == 2
        with Session(db_engine) as session, session.begin():
            reused = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=uuid4(), lock=lock
            )
            assert reused.index_id == index.index_id and reused.reused


def test_stale_owner_cannot_write_or_complete(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    old_owner, new_owner = uuid4(), uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            first = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=old_owner, lock=lock
            )
        with Session(db_engine) as session, session.begin():
            second = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=new_owner, lock=lock
            )
            assert second.index_id == first.index_id
        with Session(db_engine) as session, pytest.raises(JobLeaseLostError):
            write_segments(
                session,
                index_id=first.index_id,
                owner_token=old_owner,
                segments=[VectorSegment(chunk_id, 0, 0, 1, 1, [1.0] + [0.0] * 767)],
            )
        with Session(db_engine) as session, pytest.raises(JobLeaseLostError):
            complete_index(session, index_id=first.index_id, owner_token=old_owner, lock=lock)


@pytest.mark.parametrize(
    "vector", [[0.0] * 768, [float("nan")] * 768, [1.0] * 767, [2.0] + [0.0] * 767]
)
def test_invalid_vectors_are_rejected(
    db_engine: Engine, chunk_set: tuple[int, int, int], vector: list[float]
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
        with Session(db_engine) as session, pytest.raises(ValueError):
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[VectorSegment(chunk_id, 0, 0, 1, 1, vector)],
            )
        with Session(db_engine) as session:
            assert session.scalar(text("SELECT count(*) FROM retrieval_vectors")) == 0


def test_publication_rolls_back_with_caller_transaction(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[
                    VectorSegment(
                        chunk_id, 0, 0, len("example source text"), 3, [1.0] + [0.0] * 767
                    )
                ],
            )
        with Session(db_engine) as session:
            complete_index(session, index_id=index.index_id, owner_token=owner, lock=lock)
            session.rollback()
        with Session(db_engine) as session:
            status = session.scalar(
                text("SELECT status FROM retrieval_indexes WHERE id = :id"), {"id": index.index_id}
            )
            assert status == "building"


def test_retry_clears_failed_index_and_fences_old_owner(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[VectorSegment(chunk_id, 0, 0, 4, 3, [1.0] + [0.0] * 767)],
            )
            fail_index(session, index_id=index.index_id, owner_token=owner)
        with Session(db_engine) as session, session.begin():
            retry = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=uuid4(), lock=lock
            )
            assert retry.index_id == index.index_id and not retry.reused
            assert session.scalar(text("SELECT count(*) FROM retrieval_vectors")) == 0
        with Session(db_engine) as session, pytest.raises(JobLeaseLostError):
            fail_index(session, index_id=index.index_id, owner_token=owner)


@pytest.mark.parametrize("second_ordinal,second_start", [(1, 5), (1, 3), (2, 4)])
def test_publication_rejects_gaps_overlaps_and_discontinuous_ordinals(
    db_engine: Engine,
    chunk_set: tuple[int, int, int],
    second_ordinal: int,
    second_start: int,
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    vector = [1.0] + [0.0] * 767
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[
                    VectorSegment(chunk_id, 0, 0, 4, 3, vector),
                    VectorSegment(
                        chunk_id,
                        second_ordinal,
                        second_start,
                        len("example source text"),
                        3,
                        vector,
                    ),
                ],
            )
        with Session(db_engine) as session, pytest.raises(ValueError):
            complete_index(session, index_id=index.index_id, owner_token=owner, lock=lock)


def test_publication_requires_live_exclusive_lock(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    key = retrieval_lock_key(set_id, profile)
    with try_advisory_lock(db_engine, key) as lock:
        assert lock is not None
        with try_advisory_lock(db_engine, key) as competing_lock:
            assert competing_lock is None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
            write_segments(
                session,
                index_id=index.index_id,
                owner_token=owner,
                segments=[
                    VectorSegment(
                        chunk_id, 0, 0, len("example source text"), 3, [1.0] + [0.0] * 767
                    )
                ],
            )
    with Session(db_engine) as session, pytest.raises(DatabaseUnavailableError):
        complete_index(session, index_id=index.index_id, owner_token=owner, lock=lock)


def test_source_chunks_are_reassembled_from_the_completed_snapshot(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    _, set_id, chunk_id = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
        with Session(db_engine) as session:
            chunks = load_source_chunks(session, index_id=index.index_id, owner_token=owner)
            assert (
                load_source_chunks(
                    session, index_id=index.index_id, owner_token=owner, after_chunk_id=chunk_id
                )
                == []
            )
        with Session(db_engine) as session, pytest.raises(JobLeaseLostError):
            load_source_chunks(session, index_id=index.index_id, owner_token=uuid4())
        assert len(chunks) == 1
        assert chunks[0].chunk_id == chunk_id
        assert chunks[0].content == "example source text"
        checks: list[int] = []
        segments = list(
            vectorize_chunk(
                chunks[0],
                count_tokens=lambda value: len(value) * 60,
                embed=lambda value: [1.0] + [0.0] * 767,
                checkpoint=lambda: checks.append(1),
            )
        )
        assert len(segments) > 1
        assert (
            "".join(
                chunks[0].content[segment.start_offset : segment.end_offset] for segment in segments
            )
            == chunks[0].content
        )
        assert all(segment.token_count <= 512 for segment in segments)
        assert len(checks) >= len(segments)
        with Session(db_engine) as session, session.begin():
            write_segments(session, index_id=index.index_id, owner_token=owner, segments=segments)
            assert complete_index(
                session, index_id=index.index_id, owner_token=owner, lock=lock
            ) == len(segments)


def test_changed_source_is_rejected_before_embedding(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot_id, set_id, _ = chunk_set
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
        with db_engine.begin() as connection:
            connection.execute(
                text("UPDATE snapshot_files SET content = :content WHERE snapshot_id = :id"),
                {"content": "changed source text", "id": snapshot_id},
            )
        with Session(db_engine) as session, pytest.raises(ValueError):
            load_source_chunks(session, index_id=index.index_id, owner_token=owner)


def test_source_pagination_preserves_unicode_offsets(
    db_engine: Engine, chunk_set: tuple[int, int, int]
) -> None:
    snapshot_id, _, _ = chunk_set
    content = "A測é😀Z"
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE snapshot_files SET content = :content WHERE snapshot_id = :id"),
            {"content": content, "id": snapshot_id},
        )
    with Session(db_engine) as session, session.begin():
        set_id = build_chunk_set(session, snapshot_id, max_bytes=4).chunk_set_id
    profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
    owner = uuid4()
    with try_advisory_lock(db_engine, retrieval_lock_key(set_id, profile)) as lock:
        assert lock is not None
        with Session(db_engine) as session, session.begin():
            index = claim_index(
                session, chunk_set_id=set_id, profile=profile, owner_token=owner, lock=lock
            )
        collected = []
        cursor = 0
        while True:
            with Session(db_engine) as session:
                page = load_source_chunks(
                    session, index_id=index.index_id, owner_token=owner, after_chunk_id=cursor
                )
            if not page:
                break
            collected.extend(page)
            cursor = page[-1].chunk_id
        assert len(collected) > 1
        assert "".join(item.content for item in collected) == content
