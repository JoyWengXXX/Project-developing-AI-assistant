"""結構索引的建構服務（切片 6）：真實 PostgreSQL，直接呼叫 `snapshot_indexing.build_index`。

快照與檔案以 SQL 直接寫入（不經 Git 匯入），所以內容可以精確控制。每個測試自己取索引鎖
（`index_lock_key`），與 worker 的作法相同。
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, insert, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import AdvisoryLock, acquire_advisory_lock, index_lock_key
from repopilot.db.models import SnapshotFile
from repopilot.errors import (
    ImportCancelledError,
    JobLeaseLostError,
    SnapshotNotCompletedError,
)
from repopilot.indexing.profile import IndexProfile
from repopilot.indexing.types import FileExtraction
from repopilot.services import job_poison, snapshot_indexing
from tests.integration.snapshot_helpers import add_snapshot

PY = {
    "pkg/models.py": (
        "import os\n"
        "from . import util\n"
        "class Base: ...\n"
        "class User(Base):\n"
        "    @property\n"
        "    def name(self): ...\n"
    ),
    "pkg/util.py": "def helper(): ...\nasync def run(): ...\n",
    "pkg/broken.py": "def oops(:\n",
}
NON_INDEXED = {
    "README.md": "# readme\n",
    "web/app.html": "<p>Example</p>\n",
    "src/App.cshtml": "@{ }\n",
}


@pytest.fixture
def repository_id(db_engine: Engine, clean_repositories: None) -> int:
    with db_engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO repositories (name, canonical_local_path) "
                    "VALUES ('r', '/r') RETURNING id"
                )
            ).scalar_one()
        )


def add_files(engine: Engine, snapshot_id: int, files: dict[str, str]) -> None:
    rows = [
        {
            "snapshot_id": snapshot_id,
            "path": path,
            "content": content,
            "byte_size": len(content.encode()),
            "status": "imported",
        }
        for path, content in files.items()
    ]
    with Session(engine) as session:
        session.execute(insert(SnapshotFile), rows)
        session.commit()


@pytest.fixture
def snapshot_id(db_engine: Engine, repository_id: int) -> int:
    snapshot = add_snapshot(db_engine, repository_id)
    add_files(db_engine, snapshot, {**PY, **NON_INDEXED})
    return snapshot


@pytest.fixture
def session(db_engine: Engine) -> Iterator[Session]:
    with Session(db_engine, expire_on_commit=False) as db_session:
        yield db_session


@contextmanager
def index_lock(engine: Engine, snapshot_id: int, settings: Settings) -> Iterator[AdvisoryLock]:
    key = index_lock_key(snapshot_id, IndexProfile.from_settings(settings).hash())
    lock = acquire_advisory_lock(engine, key)
    assert lock is not None
    try:
        yield lock
    finally:
        lock.release()


def build(
    engine: Engine,
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    token: uuid.UUID | None = None,
    **kwargs: Any,
) -> snapshot_indexing.IndexSummary:
    with index_lock(engine, snapshot_id, settings) as lock:
        return snapshot_indexing.build_index(
            session, snapshot_id, settings, owner_token=token or uuid.uuid4(), lock=lock, **kwargs
        )


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar()


def index_row(engine: Engine, snapshot_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM snapshot_indexes WHERE snapshot_id = :s"), {"s": snapshot_id}
            )
            .one()
            ._mapping
        )


def count(engine: Engine, table: str) -> int:
    return int(scalar(engine, f"SELECT count(*) FROM {table}"))  # noqa: S608 - 固定表名


def test_python_files_are_indexed_and_unsupported_files_are_not(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    summary = build(db_engine, session, snapshot_id, settings)

    assert not summary.reused
    assert summary.file_count == 3  # README.md、.html、.cshtml 沒有對應列
    assert summary.parse_error_count == 1  # broken.py
    assert count(db_engine, "indexed_files") == 3
    row = index_row(db_engine, snapshot_id)
    assert row["status"] == "completed" and row["owner_token"] is None
    assert (row["file_count"], row["symbol_count"], row["parse_error_count"]) == (
        3,
        summary.symbol_count,
        1,
    )
    assert row["analyzers"]["python"]["enabled"] is True
    csharp = row["analyzers"]["csharp"]
    assert csharp["enabled"] is True and csharp["include_fields"] is False
    assert csharp["abi_version"] == 15 and csharp["grammar"] and csharp["tree_sitter"]
    with db_engine.connect() as connection:
        broken = connection.execute(
            text(
                "SELECT i.parse_status, i.error_kind, i.first_error_line, "
                "(SELECT count(*) FROM symbols s WHERE s.indexed_file_id = i.id) "
                "FROM indexed_files i JOIN snapshot_files f ON f.id = i.snapshot_file_id "
                "WHERE f.path = 'pkg/broken.py'"
            )
        ).one()
    assert tuple(broken) == ("failed", "syntax_error", 1, 0)


def test_parent_child_relations_and_references_are_persisted_with_real_ids(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    build(db_engine, session, snapshot_id, settings)

    with db_engine.connect() as connection:
        symbols = {
            row[0]: row
            for row in connection.execute(
                text(
                    "SELECT s.qualified_name, s.id, s.parent_symbol_id, s.kind, "
                    "s.leading_start_line, s.is_async FROM symbols s"
                )
            )
        }
        relation = connection.execute(
            text(
                "SELECT r.target_name, r.resolution, r.target_symbol_id, r.source_symbol_id "
                "FROM symbol_relations r WHERE r.relation_type = 'base_class'"
            )
        ).one()
        imports = connection.execute(
            text("SELECT kind, target, imported_name, relative_level FROM import_references")
        ).all()
    assert symbols["pkg.models.User.name"][2] == symbols["pkg.models.User"][1]
    assert symbols["pkg.models.User"][2] == symbols["pkg.models"][1]
    assert symbols["pkg.models.User.name"][3] == "method"
    assert symbols["pkg.models.User.name"][4] == 5  # 裝飾器行
    assert symbols["pkg.util.run"][5] is True
    assert tuple(relation) == (
        "Base",
        "resolved_same_file",
        symbols["pkg.models.Base"][1],
        symbols["pkg.models.User"][1],
    )
    assert sorted(tuple(i) for i in imports) == [
        ("from_import", "", "util", 1),
        ("import", "os", None, None),
    ]


def test_batch_size_does_not_change_the_result(
    db_engine: Engine, session: Session, snapshot_id: int, test_database_url: str
) -> None:
    one_by_one = Settings(
        database_url=SecretStr(test_database_url), index_batch_max_files=1, index_batch_max_bytes=1
    )
    batches: list[tuple[int, int]] = []
    summary = build(
        db_engine, session, snapshot_id, one_by_one, on_batch=lambda i, e: batches.append((i, e))
    )

    assert [b[0] for b in batches] == [1, 2, 3]
    assert summary.file_count == 3
    # 與預設批次大小的結果相同
    baseline_symbols = summary.symbol_count
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM snapshot_indexes"))
    again = build(
        db_engine, session, snapshot_id, Settings(database_url=SecretStr(test_database_url))
    )
    assert again.symbol_count == baseline_symbols
    assert len(batches) == 3


def test_a_completed_index_is_reused_without_doing_any_work(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    first = build(db_engine, session, snapshot_id, settings)
    called: list[object] = []

    def finalize(summary: snapshot_indexing.IndexSummary, complete: Any) -> bool:
        called.append(summary)
        return True

    second = build(db_engine, session, snapshot_id, settings, finalize=finalize)

    assert second.reused and second.index_id == first.index_id
    assert (second.file_count, second.symbol_count) == (first.file_count, first.symbol_count)
    assert called == []  # 沒有呼叫 finalize：呼叫端自己完成 job
    assert count(db_engine, "snapshot_indexes") == 1


def test_snapshots_that_are_not_completed_cannot_be_indexed(
    db_engine: Engine, session: Session, repository_id: int, settings: Settings
) -> None:
    importing = add_snapshot(db_engine, repository_id, status="importing", index=2)

    with pytest.raises(SnapshotNotCompletedError):
        build(db_engine, session, importing, settings)

    assert count(db_engine, "snapshot_indexes") == 0


def test_a_failing_build_marks_the_index_failed_and_removes_the_partial_data(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    def explode(indexed: int, errors: int) -> None:
        raise RuntimeError("boom: /secret/path")

    with pytest.raises(RuntimeError):
        build(db_engine, session, snapshot_id, settings, on_batch=explode)

    row = index_row(db_engine, snapshot_id)
    assert row["status"] == "failed" and row["owner_token"] is None
    assert row["error_summary"] == "Indexing failed (RuntimeError)."  # 固定摘要，不含訊息
    for table in ("indexed_files", "symbols", "import_references", "symbol_relations"):
        assert count(db_engine, table) == 0, table


def test_a_failed_index_is_taken_over_and_rebuilt_from_scratch(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    with pytest.raises(RuntimeError):
        build(db_engine, session, snapshot_id, settings, on_batch=_raise_runtime)
    failed_id = index_row(db_engine, snapshot_id)["id"]

    summary = build(db_engine, session, snapshot_id, settings)

    assert summary.index_id == failed_id  # 同一列重新建構（唯一約束：每個設定只有一列）
    assert index_row(db_engine, snapshot_id)["status"] == "completed"
    assert count(db_engine, "indexed_files") == 3


def _raise_runtime(indexed: int, errors: int) -> None:
    raise RuntimeError("x")


def test_a_stale_building_index_is_taken_over_by_the_next_lock_holder(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    """上一個建構者被殺：留下 building 與部分資料；新的持鎖者接手，舊資料被丟掉。"""
    dead_token = uuid.uuid4()
    with pytest.raises(KeyboardInterrupt):
        build(
            db_engine,
            session,
            snapshot_id,
            settings,
            token=dead_token,
            on_batch=_interrupt,
        )
    # KeyboardInterrupt 不是 Exception：模擬行程被殺，索引仍是 building
    assert index_row(db_engine, snapshot_id)["status"] == "building"
    assert count(db_engine, "indexed_files") > 0

    summary = build(db_engine, session, snapshot_id, settings)

    assert not summary.reused
    row = index_row(db_engine, snapshot_id)
    assert row["status"] == "completed" and row["owner_token"] is None
    assert count(db_engine, "indexed_files") == 3  # 沒有舊的殘留與重複


def _interrupt(indexed: int, errors: int) -> None:
    raise KeyboardInterrupt


def test_the_old_owner_cannot_write_after_a_takeover(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    """資料面柵欄：擁有者令牌被換掉後，舊寫入者的批次寫入與完成都被拒絕，且不留下任何資料。"""
    old_token, new_token = uuid.uuid4(), uuid.uuid4()
    rows: list[tuple[int, str, FileExtraction]] = []

    with pytest.raises(KeyboardInterrupt):
        build(db_engine, session, snapshot_id, settings, token=old_token, on_batch=_interrupt)
    index_id = index_row(db_engine, snapshot_id)["id"]
    with db_engine.begin() as connection:  # 新的擁有者接手（等同 _claim_index）
        connection.execute(
            text("UPDATE snapshot_indexes SET owner_token = :t WHERE id = :i"),
            {"t": new_token, "i": index_id},
        )
        connection.execute(
            text("DELETE FROM indexed_files WHERE snapshot_index_id = :i"), {"i": index_id}
        )

    with pytest.raises(JobLeaseLostError):
        snapshot_indexing._write_batch(session, index_id, old_token, rows)
    with pytest.raises(JobLeaseLostError):
        snapshot_indexing._complete_index(
            session,
            index_id,
            old_token,
            snapshot_indexing.IndexSummary(index_id, False, 0, 0, 0, 0, 0),
        )
    session.rollback()

    assert count(db_engine, "indexed_files") == 0
    assert index_row(db_engine, snapshot_id)["status"] == "building"


def test_cancelling_at_the_publish_boundary_leaves_a_failed_invisible_index(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    with pytest.raises(ImportCancelledError):
        build(db_engine, session, snapshot_id, settings, finalize=lambda summary, complete: False)

    row = index_row(db_engine, snapshot_id)
    assert row["status"] == "failed"
    assert row["error_summary"] == "The job was cancelled."
    assert count(db_engine, "indexed_files") == 0


def test_finalize_runs_the_completion_in_the_callers_transaction(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    seen: list[str] = []

    def finalize(summary: snapshot_indexing.IndexSummary, complete: Any) -> bool:
        complete(session)
        # 完成寫入尚未提交：另一條連線仍看到 building
        seen.append(index_row(db_engine, snapshot_id)["status"])
        session.commit()
        return True

    build(db_engine, session, snapshot_id, settings, finalize=finalize)

    assert seen == ["building"]
    assert index_row(db_engine, snapshot_id)["status"] == "completed"


def test_poison_files_are_skipped_and_recorded_without_stopping_the_rest(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    broken_id = scalar(
        db_engine,
        "SELECT id FROM snapshot_files WHERE snapshot_id = :s AND path = 'pkg/util.py'",
        s=snapshot_id,
    )
    state: dict[str, Any] = {
        "poison": {
            "suspect": {
                "unit": str(broken_id),
                "kind": "file",
                "crash_kind": "parser_crash",
                "crashes": job_poison.SKIP_THRESHOLD,
            },
            "skipped": [],
            "total_crashes": 2,
        }
    }
    persisted: list[dict[str, Any]] = []

    def persist(patch: dict[str, Any]) -> dict[str, Any]:
        persisted.append(patch)
        state.update(patch)
        return state

    summary = build(
        db_engine, session, snapshot_id, settings, tracker=job_poison.PoisonTracker(state, persist)
    )

    assert summary.skipped_file_count == 1
    assert summary.file_count == 3
    with db_engine.connect() as connection:
        skipped = connection.execute(
            text(
                "SELECT i.parse_status, i.error_kind, i.error_count, "
                "(SELECT count(*) FROM symbols s WHERE s.indexed_file_id = i.id) "
                "FROM indexed_files i JOIN snapshot_files f ON f.id = i.snapshot_file_id "
                "WHERE f.path = 'pkg/util.py'"
            )
        ).one()
        others = connection.execute(
            text(
                "SELECT count(*) FROM indexed_files i JOIN snapshot_files f ON f.id = "
                "i.snapshot_file_id WHERE f.path = 'pkg/models.py' AND i.parse_status = 'parsed'"
            )
        ).scalar_one()
    assert tuple(skipped) == ("failed", "parser_crash", 1, 0)
    assert others == 1  # 其他檔案照常索引
    assert job_poison.summarize(state).skipped_units == 1


def test_the_extractor_never_sees_files_outside_the_snapshot_or_non_imported_files(
    db_engine: Engine, session: Session, repository_id: int, snapshot_id: int, settings: Settings
) -> None:
    other = add_snapshot(db_engine, repository_id, index=2)
    add_files(db_engine, other, {"other/zzz.py": "class Other: ...\n"})
    with db_engine.begin() as connection:  # 被略過的 .py（二進位、排除…）沒有內容
        connection.execute(
            text(
                "INSERT INTO snapshot_files (snapshot_id, path, status, skip_reason) "
                "VALUES (:s, 'skipped.py', 'skipped', 'excluded')"
            ),
            {"s": snapshot_id},
        )

    build(db_engine, session, snapshot_id, settings)

    with db_engine.connect() as connection:
        paths = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT f.path FROM indexed_files i JOIN snapshot_files f "
                    "ON f.id = i.snapshot_file_id"
                )
            )
        }
    assert paths == set(PY)


def test_other_profiles_indexes_are_removed_only_after_the_new_one_completes(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO snapshot_indexes (snapshot_id, index_profile_hash, status) "
                "VALUES (:s, 'old-profile', 'completed'), (:s, 'building-profile', 'failed')"
            ),
            {"s": snapshot_id},
        )
        connection.execute(
            text(
                "INSERT INTO snapshot_indexes (snapshot_id, index_profile_hash, status, "
                "owner_token) VALUES (:s, 'active-profile', 'building', gen_random_uuid())"
            ),
            {"s": snapshot_id},
        )
    build(db_engine, session, snapshot_id, settings)
    current = IndexProfile.from_settings(settings).hash()

    removed = snapshot_indexing.delete_outdated_indexes(session, snapshot_id, current)

    assert removed == 2  # 舊設定的 completed 與 failed；別人正在 building 的不動
    with db_engine.connect() as connection:
        remaining = {
            row[0]
            for row in connection.execute(text("SELECT index_profile_hash FROM snapshot_indexes"))
        }
    assert remaining == {current, "active-profile"}


def test_the_index_lock_is_required_and_must_match_the_snapshot(
    db_engine: Engine, session: Session, snapshot_id: int, settings: Settings
) -> None:
    lock = acquire_advisory_lock(db_engine, index_lock_key(snapshot_id + 1, "other"))
    assert lock is not None
    try:
        with pytest.raises(ValueError, match="index lock"):
            snapshot_indexing.build_index(
                session, snapshot_id, settings, owner_token=uuid.uuid4(), lock=lock
            )
    finally:
        lock.release()
    assert count(db_engine, "snapshot_indexes") == 0
