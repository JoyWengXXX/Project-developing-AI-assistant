"""`import_snapshot` 的第二階段參數（規格 5.4）：`takeover`、`path`、`on_progress`、`finalize`。

第一階段的接手測試（`test_snapshots.py`、`test_import_failures.py`、`test_import_concurrency.py`）原封不動；
這裡只測新增的行為，並確認預設值（全部 `None`）時同步路徑自己取快照鎖。
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import acquire_advisory_lock, lock_key, snapshot_lock_key
from repopilot.errors import (
    DatabaseUnavailableError,
    ImportCancelledError,
    SnapshotImportInProgressError,
)
from repopilot.services import snapshots as snapshot_service
from repopilot.services.import_profile import ImportProfile
from repopilot.services.snapshots import import_snapshot
from tests.git_helpers import git, init_repo
from tests.integration.jobs.conftest import scalar

FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n"}


@pytest.fixture
def repo(roots: Path) -> Path:
    return init_repo(roots / "demo", files=FILES)


@pytest.fixture
def repository_id(client: TestClient, repo: Path) -> int:
    response = client.post("/api/v1/repositories", json={"path": str(repo)})
    assert response.status_code == 201
    return int(response.json()["id"])


@pytest.fixture
def snapshot_key(repo: Path, repository_id: int, settings: Settings) -> int:
    return snapshot_lock_key(
        repository_id, git(repo, "rev-parse", "HEAD"), ImportProfile.from_settings(settings).hash()
    )


def _importing_snapshot(
    engine: Engine, repository_id: int, repo: Path, settings: Settings, *, age: str = "0 seconds"
) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO snapshots (repository_id, commit_sha, import_profile_hash, "
                    "status, file_count, skipped_count, total_bytes, started_at) "
                    "VALUES (:r, :s, :h, 'importing', 0, 0, 0, now() - CAST(:age AS interval)) "
                    "RETURNING id"
                ),
                {
                    "r": repository_id,
                    "s": git(repo, "rev-parse", "HEAD"),
                    "h": ImportProfile.from_settings(settings).hash(),
                    "age": age,
                },
            ).scalar_one()
        )


def _snapshot_status(engine: Engine) -> str:
    return str(scalar(engine, "SELECT status FROM snapshots"))


# --- takeover ---


def test_without_takeover_a_fresh_importing_snapshot_is_refused_as_in_phase_one(
    db_engine: Engine, settings: Settings, repository_id: int, repo: Path
) -> None:
    _importing_snapshot(db_engine, repository_id, repo, settings)

    with Session(db_engine) as session, pytest.raises(SnapshotImportInProgressError):
        import_snapshot(session, repository_id, "HEAD", settings)

    assert _snapshot_status(db_engine) == "importing"  # 沒被動到


def test_holding_the_snapshot_lock_lets_the_caller_take_over_immediately(
    db_engine: Engine,
    settings: Settings,
    repository_id: int,
    repo: Path,
    snapshot_key: int,
) -> None:
    leftover = _importing_snapshot(db_engine, repository_id, repo, settings)  # 剛建立，遠未逾時
    lock = acquire_advisory_lock(db_engine, snapshot_key)
    assert lock is not None
    try:
        with Session(db_engine) as session:
            snapshot, created = import_snapshot(
                session, repository_id, "HEAD", settings, takeover=lock
            )
    finally:
        lock.release()

    assert created is True
    assert snapshot.id == leftover
    assert snapshot.status == "completed"
    assert scalar(db_engine, "SELECT count(*) FROM snapshot_files") == len(FILES)


def test_the_takeover_lock_must_be_the_exclusive_lock_of_this_exact_snapshot(
    db_engine: Engine, settings: Settings, repository_id: int, snapshot_key: int
) -> None:
    other = acquire_advisory_lock(db_engine, lock_key("snapshot", "someone-else"))
    shared = acquire_advisory_lock(db_engine, snapshot_key, shared=True)
    assert other is not None
    assert shared is not None
    try:
        for wrong in (other, shared):
            with Session(db_engine) as session, pytest.raises(ValueError, match="snapshot lock"):
                import_snapshot(session, repository_id, "HEAD", settings, takeover=wrong)
    finally:
        other.release()
        shared.release()

    assert scalar(db_engine, "SELECT count(*) FROM snapshots") == 0  # 什麼都沒建立


def test_a_lost_lock_connection_is_detected_before_publishing_and_nothing_is_published(
    db_engine: Engine,
    settings: Settings,
    repository_id: int,
    snapshot_key: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = acquire_advisory_lock(db_engine, snapshot_key)
    assert lock is not None
    real = snapshot_service._build_records

    def kill_the_lock_connection(*args: Any, **kwargs: Any) -> Any:
        pid = scalar(
            db_engine,
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid <> pg_backend_pid() LIMIT 1",
        )
        assert pid is not None
        scalar(db_engine, "SELECT pg_terminate_backend(:p)", p=pid)  # 鎖默默消失
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "_build_records", kill_the_lock_connection)
    try:
        with Session(db_engine) as session, pytest.raises(DatabaseUnavailableError):
            import_snapshot(session, repository_id, "HEAD", settings, takeover=lock)
    finally:
        lock.release()

    assert _snapshot_status(db_engine) == "failed"
    assert scalar(db_engine, "SELECT count(*) FROM snapshot_files") == 0


# --- 同步路徑自己取快照鎖 ---


def test_the_sync_path_is_refused_while_another_process_holds_the_snapshot_lock(
    db_engine: Engine, settings: Settings, repository_id: int, snapshot_key: int
) -> None:
    held = acquire_advisory_lock(db_engine, snapshot_key)
    assert held is not None
    try:
        with Session(db_engine) as session, pytest.raises(SnapshotImportInProgressError):
            import_snapshot(session, repository_id, "HEAD", settings)
    finally:
        held.release()

    assert scalar(db_engine, "SELECT count(*) FROM snapshots") == 0
    with Session(db_engine) as session:  # 釋放後可以匯入
        assert import_snapshot(session, repository_id, "HEAD", settings)[1] is True


def test_a_completed_snapshot_is_reused_even_while_the_lock_is_held(
    db_engine: Engine, settings: Settings, repository_id: int, snapshot_key: int
) -> None:
    with Session(db_engine) as session:
        first, _ = import_snapshot(session, repository_id, "HEAD", settings)
    held = acquire_advisory_lock(db_engine, snapshot_key)
    assert held is not None
    try:
        with Session(db_engine) as session:
            again, created = import_snapshot(session, repository_id, "HEAD", settings)
    finally:
        held.release()

    assert (again.id, created) == (first.id, False)  # 與第一階段相同：重用不會因為有人持鎖而變 409


def test_the_sync_path_releases_its_lock_after_success_and_after_failure(
    db_engine: Engine,
    settings: Settings,
    repository_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(snapshot_service, "_build_records", boom)
    with Session(db_engine) as session, pytest.raises(RuntimeError):
        import_snapshot(session, repository_id, "HEAD", settings)
    assert scalar(db_engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0

    monkeypatch.undo()
    with Session(db_engine) as session:
        import_snapshot(session, repository_id, "HEAD", settings)
    assert scalar(db_engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0


# --- 其他選用參數 ---


def test_an_explicit_path_replaces_the_registered_path(
    db_engine: Engine, settings: Settings, repository_id: int, repo: Path
) -> None:
    with (
        db_engine.begin() as connection
    ):  # 資料庫裡的主機路徑已不可用（例如 worker 看到的是容器路徑）
        connection.execute(
            text("UPDATE repositories SET canonical_local_path = :p WHERE id = :i"),
            {"p": str(repo.parent / "gone"), "i": repository_id},
        )

    with Session(db_engine) as session, pytest.raises(Exception, match="does not exist"):
        import_snapshot(session, repository_id, "HEAD", settings)
    with Session(db_engine) as session:
        snapshot, created = import_snapshot(
            session, repository_id, "HEAD", settings, path=repo.resolve()
        )

    assert created is True
    assert snapshot.status == "completed"


def test_a_progress_callback_that_raises_aborts_the_import_and_marks_the_snapshot_failed(
    db_engine: Engine, settings: Settings, repository_id: int
) -> None:
    calls: list[tuple[int, int | None]] = []

    def stop_at_the_second_report(processed: int, total: int | None) -> None:
        calls.append((processed, total))
        if len(calls) == 2:
            raise ImportCancelledError("The job was cancelled.")

    with Session(db_engine) as session, pytest.raises(ImportCancelledError):
        import_snapshot(
            session, repository_id, "HEAD", settings, on_progress=stop_at_the_second_report
        )

    assert calls[0] == (0, len(FILES))
    assert _snapshot_status(db_engine) == "failed"
    assert scalar(db_engine, "SELECT error_summary FROM snapshots") == "The job was cancelled."
    assert scalar(db_engine, "SELECT count(*) FROM snapshot_files") == 0


def test_a_finalizer_that_declines_publication_leaves_a_failed_invisible_snapshot(
    db_engine: Engine, settings: Settings, repository_id: int
) -> None:
    seen: list[int] = []

    def decline(snapshot_id: int, complete: Any) -> bool:
        seen.append(snapshot_id)
        return False  # 例如使用者在發布前取消

    with Session(db_engine) as session, pytest.raises(ImportCancelledError):
        import_snapshot(session, repository_id, "HEAD", settings, finalize=decline)

    assert seen == [scalar(db_engine, "SELECT id FROM snapshots")]
    assert _snapshot_status(db_engine) == "failed"
    assert scalar(db_engine, "SELECT count(*) FROM snapshot_files") == 0


def test_a_finalizer_that_publishes_completes_the_snapshot_in_its_own_transaction(
    db_engine: Engine, settings: Settings, repository_id: int
) -> None:
    def publish(snapshot_id: int, complete: Any) -> bool:
        with Session(db_engine) as txn:
            complete(txn)  # 完成寫入由接手者的交易提交
            txn.commit()
        return True

    with Session(db_engine) as session:
        snapshot, created = import_snapshot(
            session, repository_id, "HEAD", settings, finalize=publish
        )

    assert (snapshot.status, created) == ("completed", True)
    assert scalar(db_engine, "SELECT count(*) FROM snapshot_files") == len(FILES)
