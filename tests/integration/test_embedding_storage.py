"""向量索引在 PostgreSQL 中保留片段歸屬及狀態約束。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from repopilot.services.retrieval_chunks import build_chunk_set
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
