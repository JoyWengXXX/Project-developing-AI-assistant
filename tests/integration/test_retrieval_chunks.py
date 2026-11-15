"""片段集合以隔離 PostgreSQL 驗證原子性與快照歸屬。"""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.errors import SnapshotIndexNotReadyError, SnapshotNotCompletedError
from repopilot.indexing.profile import IndexProfile
from repopilot.indexing.retrieval_chunking import TextChunk, chunk_text
from repopilot.services import retrieval_chunks, snapshot_removal
from tests.git_helpers import init_repo
from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build


@pytest.fixture
def snapshot_id(db_engine: Engine, clean_repositories: None) -> int:
    with db_engine.begin() as connection:
        repository_id = connection.execute(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('retrieval', '/retrieval') RETURNING id"
            )
        ).scalar_one()
    snapshot = add_snapshot(db_engine, repository_id)
    add_files(
        db_engine,
        snapshot,
        {"hello.txt": "第一行\n" + "🙂" * 20 + "\nlast", "empty.txt": ""},
    )
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO snapshot_files (snapshot_id, path, status, skip_reason) "
                "VALUES (:snapshot, '.env', 'skipped', 'excluded')"
            ),
            {"snapshot": snapshot},
        )
    return snapshot


def test_persists_exact_source_chunks_and_ignores_skipped_files(
    db_engine: Engine, snapshot_id: int
) -> None:
    with Session(db_engine) as session, session.begin():
        result = retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT f.path, f.content, c.start_offset, c.end_offset, "
                "c.start_line, c.end_line, c.content_hash "
                "FROM retrieval_chunks c JOIN snapshot_files f ON f.id = c.snapshot_file_id "
                "WHERE c.chunk_set_id = :id ORDER BY f.path, c.ordinal"
            ),
            {"id": result.chunk_set_id},
        ).all()
    assert result.chunk_count == len(rows) and not result.reused
    assert {row.path for row in rows} == {"hello.txt"}
    content = rows[0].content
    assert "".join(content[row.start_offset : row.end_offset] for row in rows) == content
    assert all(row.start_line <= row.end_line for row in rows)
    assert all(
        row.content_hash == sha256(content[row.start_offset : row.end_offset].encode()).hexdigest()
        for row in rows
    )


def test_reuses_same_profile_and_distinguishes_different_sizes(
    db_engine: Engine, snapshot_id: int
) -> None:
    with Session(db_engine) as session, session.begin():
        first = retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)
    with Session(db_engine) as session, session.begin():
        reused = retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)
        other = retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=32)
    assert reused.reused and reused.chunk_set_id == first.chunk_set_id
    assert other.chunk_set_id != first.chunk_set_id and not other.reused


def test_noncompleted_snapshot_is_rejected(db_engine: Engine, snapshot_id: int) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE snapshots SET status = 'failed' WHERE id = :id"), {"id": snapshot_id}
        )
    with Session(db_engine) as session, pytest.raises(SnapshotNotCompletedError):
        retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)


def test_concurrent_builds_publish_only_one_chunk_set(db_engine: Engine, snapshot_id: int) -> None:
    barrier = Barrier(2)

    def build() -> retrieval_chunks.ChunkSetSummary:
        with Session(db_engine) as session, session.begin():
            barrier.wait(timeout=10)
            return retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: build(), range(2)))
    assert len({result.chunk_set_id for result in results}) == 1
    assert sorted(result.reused for result in results) == [False, True]


def test_failure_rolls_back_all_chunks(
    db_engine: Engine, snapshot_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE snapshot_files SET content = :content, byte_size = 2400 "
                "WHERE snapshot_id = :snapshot AND path = 'hello.txt'"
            ),
            {"content": "x" * 2400, "snapshot": snapshot_id},
        )
    calls = 0

    def fail_second(
        content: str, *, max_bytes: int, structure_lines: list[int] | None = None
    ) -> list[TextChunk]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("controlled failure")
        return chunk_text(content, max_bytes=max_bytes, structure_lines=structure_lines)

    monkeypatch.setattr(retrieval_chunks, "chunk_text", fail_second)
    with Session(db_engine) as session, session.begin(), pytest.raises(RuntimeError):
        retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=4)
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM retrieval_chunk_sets")) == 0
        assert connection.scalar(text("SELECT count(*) FROM retrieval_chunks")) == 0


def test_uses_completed_structure_index_for_matching_file(
    db_engine: Engine, snapshot_id: int, settings: Settings
) -> None:
    add_files(
        db_engine,
        snapshot_id,
        {"module.py": "# header\n\ndef first():\n    pass\n\ndef second():\n    pass\n"},
    )
    with Session(db_engine, expire_on_commit=False) as session:
        index = build(db_engine, session, snapshot_id, settings)
    with Session(db_engine) as session, session.begin():
        result = retrieval_chunks.build_chunk_set(
            session, snapshot_id, max_bytes=24, structure_index_id=index.index_id
        )
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT f.path, c.split_kind FROM retrieval_chunks c "
                "JOIN snapshot_files f ON f.id = c.snapshot_file_id "
                "WHERE c.chunk_set_id = :id"
            ),
            {"id": result.chunk_set_id},
        ).all()
        provenance = connection.execute(
            text(
                "SELECT structure_index_id, structure_profile_hash FROM retrieval_chunk_sets "
                "WHERE id = :id"
            ),
            {"id": result.chunk_set_id},
        ).one()
    assert any(row.path == "module.py" and row.split_kind == "structure" for row in rows)
    assert all(row.split_kind != "structure" for row in rows if row.path == "hello.txt")
    assert provenance == (index.index_id, IndexProfile.from_settings(settings).hash())


def test_missing_structure_index_is_not_silently_used_as_text(
    db_engine: Engine, snapshot_id: int
) -> None:
    with Session(db_engine) as session, pytest.raises(SnapshotIndexNotReadyError) as error:
        retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24, structure_index_id=999)
    assert error.value.index_status == "none"


def test_real_git_snapshot_chunks_committed_content_only(
    db_engine: Engine, client: TestClient, roots: Path
) -> None:
    repo = init_repo(
        roots / "chunk-repo",
        files={"src/main.py": "def main():\n    return 'committed'\n", ".env": "excluded"},
    )
    (repo / "src" / "main.py").write_text("uncommitted", encoding="utf-8")
    registered = client.post("/api/v1/repositories", json={"path": str(repo)})
    assert registered.status_code == 201
    response = client.post(f"/api/v1/repositories/{registered.json()['id']}/imports")
    assert response.status_code == 201
    snapshot_id = response.json()["id"]
    with Session(db_engine) as session, session.begin():
        result = retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)
    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT f.path, f.content, c.start_offset, c.end_offset FROM retrieval_chunks c "
                "JOIN snapshot_files f ON f.id = c.snapshot_file_id "
                "WHERE c.chunk_set_id = :id ORDER BY c.ordinal"
            ),
            {"id": result.chunk_set_id},
        ).all()
    assert {row.path for row in rows} == {"src/main.py"}
    assert "".join(row.content[row.start_offset : row.end_offset] for row in rows) == (
        "def main():\n    return 'committed'\n"
    )


def test_database_rejects_chunk_from_another_snapshot(db_engine: Engine, snapshot_id: int) -> None:
    with Session(db_engine) as session, session.begin():
        result = retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)
    with db_engine.connect() as connection:
        repository_id = connection.scalar(
            text("SELECT repository_id FROM snapshots WHERE id = :id"), {"id": snapshot_id}
        )
    other = add_snapshot(db_engine, repository_id, index=2)
    add_files(db_engine, other, {"other.txt": "not the same snapshot"})
    with pytest.raises(IntegrityError), db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO retrieval_chunks "
                "(snapshot_id, chunk_set_id, snapshot_file_id, ordinal, start_offset, "
                "end_offset, start_line, end_line, split_kind, content_hash) "
                "SELECT :snapshot, :chunk_set, id, 0, 0, 1, 1, 1, 'text', :hash "
                "FROM snapshot_files WHERE snapshot_id = :other"
            ),
            {
                "snapshot": snapshot_id,
                "chunk_set": result.chunk_set_id,
                "other": other,
                "hash": "0" * 64,
            },
        )


def test_snapshot_removal_deletes_derived_chunk_data(
    db_engine: Engine, snapshot_id: int, settings: Settings
) -> None:
    with Session(db_engine) as session, session.begin():
        retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)
    with Session(db_engine) as session:
        snapshot_removal.remove_snapshot(session, snapshot_id, settings)
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM retrieval_chunk_sets")) == 0
        assert connection.scalar(text("SELECT count(*) FROM retrieval_chunks")) == 0


def test_migration_refuses_to_delete_existing_chunk_sets(
    db_engine: Engine, snapshot_id: int, test_database_url: str
) -> None:
    with Session(db_engine) as session, session.begin():
        result = retrieval_chunks.build_chunk_set(session, snapshot_id, max_bytes=24)
    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        command.downgrade(alembic_config(test_database_url), "0013")
    with db_engine.connect() as connection:
        head = ScriptDirectory.from_config(alembic_config(test_database_url)).get_current_head()
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert (
            connection.scalar(
                text("SELECT count(*) FROM retrieval_chunks WHERE chunk_set_id = :id"),
                {"id": result.chunk_set_id},
            )
            == result.chunk_count
        )
