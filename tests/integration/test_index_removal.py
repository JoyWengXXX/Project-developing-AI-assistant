"""移除含大型結構索引的 repo／快照：分批清除（`purge_index_data`）的行為。

把門檻與批次大小調得很小，讓小資料也走分批路徑；真實 PostgreSQL。
"""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.errors import RepositoryBusyError, SnapshotReferencedError
from repopilot.services import repository_removal, snapshot_indexing, snapshot_removal
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build, count, scalar

FILES = {
    "a/one.py": "class A: ...\nclass B(A):\n    def m(self): ...\n",
    "a/two.py": "import os\ndef f(): ...\n",
    "a/three.py": "class C: ...\n",
}
DERIVED = ("indexed_files", "symbols", "import_references", "symbol_relations")


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


@pytest.fixture
def indexed_snapshot(
    db_engine: Engine, repository_id: int, settings: Settings
) -> tuple[int, Session]:
    snapshot = add_snapshot(db_engine, repository_id)
    add_files(db_engine, snapshot, FILES)
    session = Session(db_engine, expire_on_commit=False)
    build(db_engine, session, snapshot, settings)
    return snapshot, session


@pytest.fixture
def tiny_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot_removal, "PURGE_THRESHOLD_SYMBOLS", 1)
    monkeypatch.setattr(repository_removal, "PURGE_THRESHOLD_SYMBOLS", 1)
    monkeypatch.setattr(snapshot_indexing, "PURGE_BATCH_SYMBOLS", 3)


def _batches(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """記錄 `purge_index_data` 被呼叫的次數（每次記下當時的批次大小）。"""
    calls: list[int] = []
    real = snapshot_indexing.purge_index_data

    def spy(session: Session, ids: list[int], *, batch_size: int | None = None) -> None:
        calls.append(snapshot_indexing.PURGE_BATCH_SYMBOLS)
        real(session, ids, batch_size=batch_size)

    monkeypatch.setattr(snapshot_indexing, "purge_index_data", spy)
    return calls


def test_removing_a_repository_with_a_large_index_purges_it_in_batches(
    db_engine: Engine,
    repository_id: int,
    indexed_snapshot: tuple[int, Session],
    settings: Settings,
    tiny_purge: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = indexed_snapshot
    calls = _batches(monkeypatch)
    assert count(db_engine, "symbols") >= 8  # 批次大小 3：至少三批

    result = repository_removal.remove_repository(session, repository_id, settings)

    assert calls == [3]
    assert result.deleted_snapshots == 1 and result.deleted_files == len(FILES)
    for table in (*DERIVED, "snapshot_indexes", "snapshots", "repositories"):
        assert count(db_engine, table) == 0, table


def test_small_indexes_are_not_purged_and_cascade_as_before(
    db_engine: Engine,
    repository_id: int,
    indexed_snapshot: tuple[int, Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = indexed_snapshot
    calls = _batches(monkeypatch)

    repository_removal.remove_repository(session, repository_id, settings)

    assert calls == []  # 低於門檻：沒有分批
    for table in (*DERIVED, "snapshot_indexes", "repositories"):
        assert count(db_engine, table) == 0, table


def test_a_busy_repository_is_refused_before_anything_is_purged(
    db_engine: Engine,
    repository_id: int,
    indexed_snapshot: tuple[int, Session],
    settings: Settings,
    tiny_purge: None,
) -> None:
    snapshot_id, session = indexed_snapshot
    with db_engine.begin() as connection:  # 有進行中的 job：移除必須在動任何資料之前被擋下
        connection.execute(
            text(
                "INSERT INTO jobs (kind, status, phase, repository_id, dedupe_key, max_attempts) "
                "VALUES ('snapshot_import', 'queued', 'pending', :r, 'k', 3)"
            ),
            {"r": repository_id},
        )
    symbols = count(db_engine, "symbols")

    with pytest.raises(RepositoryBusyError):
        repository_removal.remove_repository(session, repository_id, settings)

    assert count(db_engine, "symbols") == symbols
    status = scalar(
        db_engine, "SELECT status FROM snapshot_indexes WHERE snapshot_id = :s", s=snapshot_id
    )
    assert status == "completed"  # 沒有被標成 failed


def test_a_failure_midway_leaves_only_invisible_failed_indexes_and_a_retry_succeeds(
    db_engine: Engine,
    repository_id: int,
    indexed_snapshot: tuple[int, Session],
    settings: Settings,
    tiny_purge: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id, session = indexed_snapshot
    real_commit = Session.commit
    state = {"commits": 0}

    def flaky_commit(self: Session) -> None:
        state["commits"] += 1
        # remove_repository 先 commit 一次（結束讀取交易），purge 的第 1 次 commit 標記 failed、
        # 第 2 次刪關係與 import，之後每個符號批次一次：在第二個符號批次的 commit 中斷
        if state["commits"] == 5:
            raise RuntimeError("boom")
        real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky_commit)
    with pytest.raises(RuntimeError):
        repository_removal.remove_repository(session, repository_id, settings)
    monkeypatch.setattr(Session, "commit", real_commit)

    assert count(db_engine, "repositories") == 1  # repo 還在，可以重試
    assert (
        scalar(
            db_engine, "SELECT status FROM snapshot_indexes WHERE snapshot_id = :s", s=snapshot_id
        )
        == "failed"
    )
    assert count(db_engine, "symbols") > 0  # 還有殘留，但索引已不可見

    result = repository_removal.remove_repository(session, repository_id, settings)

    assert result.deleted_snapshots == 1
    for table in (*DERIVED, "snapshot_indexes", "repositories"):
        assert count(db_engine, table) == 0, table


def test_removing_a_snapshot_with_a_large_index_purges_it_in_batches(
    db_engine: Engine,
    indexed_snapshot: tuple[int, Session],
    settings: Settings,
    tiny_purge: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id, session = indexed_snapshot
    calls = _batches(monkeypatch)

    result = snapshot_removal.remove_snapshot(session, snapshot_id, settings)

    assert calls == [3]
    assert result.deleted_files == len(FILES)
    for table in (*DERIVED, "snapshot_indexes", "snapshots"):
        assert count(db_engine, table) == 0, table


def test_a_referenced_snapshot_is_refused_before_its_index_is_purged(
    db_engine: Engine,
    repository_id: int,
    indexed_snapshot: tuple[int, Session],
    settings: Settings,
    tiny_purge: None,
) -> None:
    snapshot_id, session = indexed_snapshot
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"),
            {"r": repository_id, "s": snapshot_id},
        )
    symbols = count(db_engine, "symbols")

    with pytest.raises(SnapshotReferencedError):
        snapshot_removal.remove_snapshot(session, snapshot_id, settings)

    assert count(db_engine, "symbols") == symbols
    assert count(db_engine, "snapshots") == 1


def test_purging_only_touches_the_given_snapshots(
    db_engine: Engine,
    repository_id: int,
    indexed_snapshot: tuple[int, Session],
    settings: Settings,
    tiny_purge: None,
) -> None:
    snapshot_id, session = indexed_snapshot
    other = add_snapshot(db_engine, repository_id, index=2)
    add_files(db_engine, other, {"o.py": "class Other: ...\n"})
    build(db_engine, session, other, settings)
    other_symbols = scalar(
        db_engine,
        "SELECT symbol_count FROM snapshot_indexes WHERE snapshot_id = :s",
        s=other,
    )

    snapshot_removal.remove_snapshot(session, snapshot_id, settings)

    assert count(db_engine, "symbols") == other_symbols
    assert count(db_engine, "snapshot_indexes") == 1
