"""切片 6：匯入的失敗注入、重試與所有權（PostgreSQL ＋ 真實暫存 Git repo）。

能用真實故障就不用 mock：Git 失敗以刪除／還原 `.git/objects` 內的物件檔製造，Git 逾時以極小的
逾時值觸發真實的 subprocess 逾時。只有「Git 回傳不一致資料」與「資料庫在寫入途中出錯」這類
無法自然製造的外部故障才以 monkeypatch 注入。
"""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.errors import GitCommandError
from repopilot.git.objects import TreeEntry
from repopilot.git.objects import list_tree as git_list_tree
from repopilot.main import create_app
from repopilot.services import snapshots as snapshot_service
from repopilot.services.snapshots import import_snapshot
from tests.git_helpers import git, init_repo
from tests.github_helpers import capture_logs
from tests.lock_helpers import without_snapshot_lock

URL = "/api/v1/repositories"
SNAPSHOTS = "/api/v1/snapshots"
FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n", "docs/guide.md": "guide\n"}


def _register(client: TestClient, repo: Path) -> int:
    response = client.post(URL, json={"path": str(repo)})
    assert response.status_code in (200, 201)
    repository_id: int = response.json()["id"]
    return repository_id


def _import(client: TestClient, repository_id: int, **body: Any) -> Any:
    return client.post(f"{URL}/{repository_id}/imports", json=body)


def _snapshot(db_engine: Engine) -> Snapshot:
    with Session(db_engine) as session:
        return session.scalars(select(Snapshot)).one()


def _file_paths(db_engine: Engine, snapshot_id: int | None = None) -> set[str]:
    with Session(db_engine) as session:
        query = select(SnapshotFile.path)
        if snapshot_id is not None:
            query = query.where(SnapshotFile.snapshot_id == snapshot_id)
        return set(session.scalars(query))


def _file_count(db_engine: Engine) -> int:
    with Session(db_engine) as session:
        return session.scalar(select(func.count()).select_from(SnapshotFile)) or 0


@pytest.fixture
def repo(roots: Path) -> Path:
    return init_repo(roots / "demo", files=dict(FILES))


@contextmanager
def _object_missing(repo: Path, rev_path: str) -> Iterator[None]:
    """暫時移走某個 loose 物件檔（真實的 Git 損壞），離開時還原。"""
    object_id = git(repo, "rev-parse", rev_path)
    target = repo / ".git" / "objects" / object_id[:2] / object_id[2:]
    assert target.is_file(), "測試前提：小型 repo 的物件是 loose 物件"
    data = target.read_bytes()
    os.chmod(target, stat.S_IWRITE)  # Windows 的物件檔為唯讀
    target.unlink()
    try:
        yield
    finally:
        target.write_bytes(data)
        os.chmod(target, stat.S_IREAD)


def _assert_failed_without_partial_data(
    client: TestClient, db_engine: Engine, response: Any, *, status: int, code: str
) -> Snapshot:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    snapshot = _snapshot(db_engine)
    assert snapshot.status == "failed"
    assert snapshot.error_summary
    assert snapshot.completed_at is not None
    assert _file_count(db_engine) == 0
    files = client.get(f"{SNAPSHOTS}/{snapshot.id}/files")
    assert files.status_code == 409
    assert files.json()["error"]["code"] == "snapshot_not_completed"
    detail = client.get(f"{SNAPSHOTS}/{snapshot.id}").json()
    assert detail["status"] == "failed"
    assert detail["file_status_counts"] == {}
    return snapshot


def _assert_retry_completes_the_same_snapshot(
    client: TestClient, db_engine: Engine, failed: Snapshot, repository_id: int
) -> None:
    retried = _import(client, repository_id)

    assert retried.status_code == 201
    assert retried.json()["id"] == failed.id
    assert retried.json()["status"] == "completed"
    assert retried.json()["error_summary"] is None
    assert _file_paths(db_engine, failed.id) == set(FILES)
    assert _file_count(db_engine) == len(FILES)  # 重試後沒有重複的檔案列
    files = client.get(f"{SNAPSHOTS}/{failed.id}/files").json()
    assert files["total"] == len(FILES)


# --- Git 真實故障：物件遺失 ---


@pytest.mark.parametrize(
    "missing",
    ["HEAD:src/app.py", "HEAD^{tree}"],
    ids=["missing-blob-during-cat-file", "missing-tree-during-ls-tree"],
)
def test_missing_git_object_fails_the_import_without_partial_data_and_retry_succeeds(
    client: TestClient, repo: Path, db_engine: Engine, missing: str
) -> None:
    repository_id = _register(client, repo)

    with _object_missing(repo, missing):
        response = _import(client, repository_id)
        failed = _assert_failed_without_partial_data(
            client, db_engine, response, status=500, code="git_command_failed"
        )

    _assert_retry_completes_the_same_snapshot(client, db_engine, failed, repository_id)


# --- Git 逾時（真實的 subprocess 逾時） ---


@pytest.mark.parametrize("stage", ["list_tree", "read_blobs"])
def test_git_timeout_returns_504_marks_the_snapshot_failed_and_retry_succeeds(
    client: TestClient,
    settings: Settings,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    repository_id = _register(client, repo)
    real = getattr(snapshot_service, stage)

    def with_tiny_timeout(*args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = 1e-6  # 一定先逾時：git 程序啟動就需要數毫秒
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, stage, with_tiny_timeout)
    timed_out = _import(client, repository_id)
    monkeypatch.setattr(snapshot_service, stage, real)

    failed = _assert_failed_without_partial_data(
        client, db_engine, timed_out, status=504, code="git_timeout"
    )
    _assert_retry_completes_the_same_snapshot(client, db_engine, failed, repository_id)


def test_import_read_timeout_setting_is_what_bounds_the_blob_read(
    settings: Settings, repo: Path, db_engine: Engine, client: TestClient
) -> None:
    """`IMPORT_GIT_TIMEOUT_SECONDS` 只作用於讀取 blob：極小值使匯入逾時，tree 列舉仍用一般逾時。"""
    repository_id = _register(client, repo)
    tiny = settings.model_copy(update={"import_git_timeout_seconds": 1e-6})

    with TestClient(create_app(tiny)) as tiny_client:
        response = _import(tiny_client, repository_id)

    assert response.status_code == 504
    assert _snapshot(db_engine).status == "failed"


# --- Git 回傳不一致的資料（注入） ---


def test_blob_size_mismatch_between_ls_tree_and_cat_file_fails_without_partial_data(
    client: TestClient,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = _register(client, repo)

    def lying_sizes(*args: Any, **kwargs: Any) -> list[TreeEntry]:
        return [replace(e, size=(e.size or 0) + 1) for e in git_list_tree(*args, **kwargs)]

    monkeypatch.setattr(snapshot_service, "list_tree", lying_sizes)
    response = _import(client, repository_id)
    monkeypatch.setattr(snapshot_service, "list_tree", git_list_tree)

    failed = _assert_failed_without_partial_data(
        client, db_engine, response, status=500, code="git_command_failed"
    )
    _assert_retry_completes_the_same_snapshot(client, db_engine, failed, repository_id)


# --- 資料庫在寫入途中出錯 ---


def test_failure_in_a_later_insert_batch_rolls_back_the_earlier_batches_too(
    client: TestClient,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """檔案列分批 INSERT；最後一批失敗時，前面已執行的批次與 completed 狀態都必須一併回滾。"""
    repository_id = _register(client, repo)
    monkeypatch.setattr(snapshot_service, "INSERT_BATCH_SIZE", 1)
    real_build = snapshot_service._build_records

    def duplicate_last_path(*args: Any, **kwargs: Any) -> Any:
        records, stats = real_build(*args, **kwargs)
        return [*records, dict(records[-1])], stats  # 最後一批才違反 (snapshot_id, path)

    monkeypatch.setattr(snapshot_service, "_build_records", duplicate_last_path)

    response = _import(client, repository_id)

    assert response.status_code == 500
    snapshot = _snapshot(db_engine)
    assert snapshot.status == "failed"
    assert snapshot.error_summary == "Unexpected IntegrityError."
    assert _file_count(db_engine) == 0
    assert (snapshot.file_count, snapshot.skipped_count, snapshot.total_bytes) == (0, 0, 0)


def test_database_unavailable_during_the_write_returns_503_and_the_snapshot_can_be_retried(
    client: TestClient,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = _register(client, repo)
    real_complete = snapshot_service._complete_snapshot

    def connection_lost(*_: Any, **__: Any) -> None:
        raise OperationalError("INSERT ...", {}, Exception("server closed the connection"))

    monkeypatch.setattr(snapshot_service, "_complete_snapshot", connection_lost)
    response = _import(client, repository_id)
    monkeypatch.setattr(snapshot_service, "_complete_snapshot", real_complete)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "server closed" not in response.text
    snapshot = _snapshot(db_engine)
    assert snapshot.status == "failed"
    assert _file_count(db_engine) == 0
    _assert_retry_completes_the_same_snapshot(client, db_engine, snapshot, repository_id)


def test_mark_failed_that_cannot_reach_the_database_does_not_mask_the_original_error(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    sha = git(repo, "rev-parse", "HEAD")
    started = datetime.now(UTC)
    with Session(db_engine) as session:
        snapshot = Snapshot(
            repository_id=repository_id,
            commit_sha=sha,
            import_profile_hash="h" * 64,
            status="importing",
            started_at=started,
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = snapshot.id

    class BrokenSession(Session):
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise OperationalError("UPDATE ...", {}, Exception("connection lost"))

    with capture_logs() as logs, BrokenSession(db_engine) as broken:
        snapshot_service._mark_failed(broken, snapshot_id, started, "original failure")

    assert "snapshot_mark_failed_error" in logs.text()
    # 資料庫沒收到更新：仍是 importing，之後逾時由其他請求接手（D24）
    assert _snapshot(db_engine).status == "importing"


# --- 失去所有權（過期後被接手）---


def _make_stale(db_engine: Engine) -> None:
    with Session(db_engine) as session:
        session.execute(update(Snapshot).values(started_at=datetime.now(UTC) - timedelta(hours=2)))
        session.commit()


def _takeover_during_build(
    monkeypatch: pytest.MonkeyPatch,
    db_engine: Engine,
    settings: Settings,
    repository_id: int,
    *,
    original_fails: bool,
) -> list[tuple[str, bool]]:
    """第一個請求建置途中，它的 started_at 逾時，另一個請求接手並完成匯入。

    第二階段切片 3：活著的匯入者現在持有快照鎖，接手者會被擋下；這裡驗證的是資料面的
    `started_at` 柵欄（鎖失效時的最後一道防線），所以關閉快照鎖（原因見 `lock_helpers`）。
    """
    without_snapshot_lock(monkeypatch)
    real_build = snapshot_service._build_records
    takeovers: list[tuple[str, bool]] = []

    def takeover_then_continue(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(snapshot_service, "_build_records", real_build)  # 接手者走正常流程
        _make_stale(db_engine)
        with Session(db_engine) as other:
            snapshot, created = import_snapshot(other, repository_id, "HEAD", settings)
            takeovers.append((snapshot.status, created))
        if original_fails:
            raise GitCommandError("original request failed after losing ownership")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "_build_records", takeover_then_continue)
    return takeovers


@pytest.mark.parametrize(
    "original_fails", [True, False], ids=["original-fails", "original-finishes"]
)
def test_request_that_lost_ownership_to_a_takeover_never_overwrites_the_new_owner(
    client: TestClient,
    settings: Settings,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    original_fails: bool,
) -> None:
    repository_id = _register(client, repo)
    takeovers = _takeover_during_build(
        monkeypatch, db_engine, settings, repository_id, original_fails=original_fails
    )

    response = _import(client, repository_id)

    assert takeovers == [("completed", True)]  # 接手者確實完成了匯入
    assert response.status_code == (500 if original_fails else 409)
    snapshot = _snapshot(db_engine)
    # 新擁有者的結果完好：原請求不論失敗或完成都不得改寫狀態、錯誤摘要或檔案列
    assert snapshot.status == "completed"
    assert snapshot.error_summary is None
    assert _file_paths(db_engine, snapshot.id) == set(FILES)
    assert _file_count(db_engine) == len(FILES)
    assert _import(client, repository_id).status_code == 200


def test_failure_of_a_request_that_lost_ownership_does_not_mark_the_new_owners_snapshot_failed(
    client: TestClient,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新擁有者仍在匯入（status 還是 importing）時，舊請求失敗不得把它標成 failed。"""
    repository_id = _register(client, repo)

    def new_owner_takes_over_then_fail(*_: Any, **__: Any) -> None:
        with Session(db_engine) as session:
            session.execute(update(Snapshot).values(started_at=datetime.now(UTC)))
            session.commit()
        raise GitCommandError("original request failed after losing ownership")

    monkeypatch.setattr(snapshot_service, "_build_records", new_owner_takes_over_then_fail)

    response = _import(client, repository_id)

    assert response.status_code == 500
    snapshot = _snapshot(db_engine)
    assert snapshot.status == "importing"  # 仍屬於新擁有者
    assert snapshot.error_summary is None
    assert snapshot.completed_at is None
    assert _file_count(db_engine) == 0


# --- 失敗不洩漏內容 ---


def test_failed_import_does_not_leak_file_content_into_the_response_or_error_summary(
    client: TestClient,
    roots: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "FAKE_KEY_FOR_TEST_content_must_not_be_logged"
    repo = init_repo(roots / "leak", files={"a.py": f"KEY = '{marker}'\n", "b.py": "x = 1\n"})
    repository_id = _register(client, repo)
    real_build = snapshot_service._build_records

    def duplicate_paths(*args: Any, **kwargs: Any) -> Any:
        records, stats = real_build(*args, **kwargs)
        return [*records, dict(records[0])], stats  # 重複路徑 → IntegrityError，參數含檔案內容

    monkeypatch.setattr(snapshot_service, "_build_records", duplicate_paths)

    response = _import(client, repository_id)

    assert response.status_code == 500
    assert marker not in response.text
    assert marker not in (_snapshot(db_engine).error_summary or "")


def test_failed_import_does_not_log_file_content_from_sql_parameters(
    client: TestClient,
    roots: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "FAKE_KEY_FOR_TEST_content_must_not_be_logged"
    repo = init_repo(roots / "leak-log", files={"a.py": f"KEY = '{marker}'\n", "b.py": "x = 1\n"})
    repository_id = _register(client, repo)
    real_build = snapshot_service._build_records

    def duplicate_paths(*args: Any, **kwargs: Any) -> Any:
        records, stats = real_build(*args, **kwargs)
        return [*records, dict(records[0])], stats  # 重複路徑 → IntegrityError，參數含檔案內容

    monkeypatch.setattr(snapshot_service, "_build_records", duplicate_paths)

    with capture_logs() as logs:
        response = _import(client, repository_id)

    assert response.status_code == 500
    text = logs.text()
    assert marker not in text
    # 只隱藏參數值；例外型別、失敗的語句與資料庫端訊息仍在，除錯資訊沒有被一併抹掉
    assert "IntegrityError" in text
    assert "INSERT INTO snapshot_files" in text
    assert "uq_snapshot_files_snapshot_id_path" in text
    assert "[parameters:" not in text
