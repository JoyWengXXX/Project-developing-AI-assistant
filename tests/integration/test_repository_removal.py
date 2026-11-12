"""移除已註冊的 repo（D33）：`DELETE /api/v1/repositories/{id}`（PostgreSQL ＋ 真實 Git）。

「遠端」是本機 bare repo（`file://`）；GitHub API 以 `httpx.MockTransport` 模擬，不連真實 GitHub。
重點不變量：local 來源絕不碰磁碟上的 repo 目錄；github 來源只刪受管目錄內的那一個 clone；
驗證不過或刪除失敗時資料列與快照原封不動；不留孤兒資料。
"""

import hashlib
import os
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, event, func, insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.db.locks import acquire_advisory_lock, repo_lock_key
from repopilot.db.models import Repository, Snapshot, SnapshotFile
from repopilot.errors import GitCommandError
from repopilot.main import create_app
from repopilot.services import managed_repos
from tests.git_helpers import RemoteRepo, commit_files, init_repo, link_directory
from tests.github_helpers import FAKE_TOKEN, FakeGitHub, capture_logs

URL = "/api/v1/repositories"
GITHUB_URL = f"{URL}/github"
REPO_FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n"}


# --- fixtures ---


@pytest.fixture
def managed_dir(tmp_path: Path) -> Path:
    return tmp_path / "managed"


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    directory = tmp_path / "remotes"
    directory.mkdir()
    return directory


@pytest.fixture
def settings(test_database_url: str, roots: Path, managed_dir: Path, remotes: Path) -> Settings:
    return Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
        managed_repos_dir=managed_dir,
        github_clone_base_url=remotes.as_uri(),
        github_clone_timeout_seconds=60,
    )


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def clean_state(db_engine: Engine) -> None:
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))
        connection.execute(text("TRUNCATE github_credentials"))


@pytest.fixture
def app(settings: Settings, clean_state: None, fake_github: FakeGitHub) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_github_client] = fake_github.client
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def remote(remotes: Path, fake_github: FakeGitHub) -> RemoteRepo:
    fake_github.add_repo("octocat/Hello-World")
    return RemoteRepo(remotes, "octocat", "Hello-World", files=dict(REPO_FILES))


@pytest.fixture
def local_repo(roots: Path) -> Path:
    return init_repo(roots / "demo", files=dict(REPO_FILES))


# --- helpers ---


def _delete(client: TestClient, repository_id: int | str) -> Any:
    return client.delete(f"{URL}/{repository_id}")


def _register_local(client: TestClient, path: Path) -> int:
    response = client.post(URL, json={"path": str(path)})
    assert response.status_code in (200, 201)
    return int(response.json()["id"])


def _register_github(client: TestClient, repo: str = "octocat/Hello-World") -> Any:
    return client.post(GITHUB_URL, json={"repo": repo})


def _import(client: TestClient, repository_id: int, **body: Any) -> Any:
    return client.post(f"{URL}/{repository_id}/imports", json=body)


def _clone_path(managed_dir: Path, owner: str = "octocat", name: str = "Hello-World") -> Path:
    return managed_dir / "github" / owner / name


def _totals(db_engine: Engine) -> tuple[int, int, int]:
    """(repositories, snapshots, snapshot_files) 的資料列數；每個測試開始時都已清空。"""
    with Session(db_engine) as session:
        return (
            session.scalar(select(func.count()).select_from(Repository)) or 0,
            session.scalar(select(func.count()).select_from(Snapshot)) or 0,
            session.scalar(select(func.count()).select_from(SnapshotFile)) or 0,
        )


def _add_snapshot(
    db_engine: Engine,
    repository_id: int,
    *,
    status: str = "completed",
    started_at: datetime | None = None,
    files: int = 0,
    index: int = 1,
) -> int:
    """直接寫入快照（與可選的大量檔案列）；`index` 讓同一 repo 的 commit_sha 不重複。"""
    with Session(db_engine) as session:
        snapshot = Snapshot(
            repository_id=repository_id,
            commit_sha=f"{index:040x}",
            import_profile_hash="test-profile",
            status=status,
            started_at=started_at or datetime.now(UTC),
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id
        if files:
            session.execute(
                insert(SnapshotFile),
                [
                    {
                        "snapshot_id": snapshot_id,
                        "path": f"dir/file-{number}.txt",
                        "content": "x" * 100,
                        "byte_size": 100,
                        "status": "imported",
                    }
                    for number in range(files)
                ],
            )
        session.commit()
    return snapshot_id


def _fingerprint(root: Path) -> dict[str, str]:
    """目錄樹的內容指紋（含 `.git`）：路徑 → 檔案 SHA-256 或 `dir`。"""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _update_row(db_engine: Engine, repository_id: int, **values: str) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    with db_engine.begin() as connection:
        connection.execute(
            text(f"UPDATE repositories SET {assignments} WHERE id = :id"),  # noqa: S608
            {**values, "id": repository_id},
        )


def _error_code(response: Any) -> str:
    code = response.json()["error"]["code"]
    assert isinstance(code, str)
    return code


def _sqlalchemy_engine(app: FastAPI) -> Engine:
    engine: Engine = app.state.session_factory.kw["bind"]
    return engine


def _fail_statements(engine: Engine, prefix: str) -> Callable[[], None]:
    """對以 `prefix` 開頭的 SQL 注入 OperationalError；回傳移除注入的函式。"""

    def hook(*args: Any) -> None:
        statement = args[2]
        if statement.lstrip().upper().startswith(prefix):
            raise OperationalError(statement, None, Exception("injected failure"))

    event.listen(engine, "before_cursor_execute", hook)
    return lambda: event.remove(engine, "before_cursor_execute", hook)


def _wait_until_a_query_is_blocked(db_engine: Engine) -> None:
    """輪詢 pg_stat_activity，直到有查詢在等待列鎖（觀察條件，不是用 sleep 判斷成功）。"""
    deadline = time.monotonic() + 15
    with db_engine.connect() as connection:
        while time.monotonic() < deadline:
            blocked = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar()
            connection.rollback()
            if blocked:
                return
            time.sleep(0.02)
    pytest.fail("沒有觀察到被列鎖擋住的查詢")


# --- local 來源 ---


def test_remove_local_repository_deletes_records_but_leaves_the_directory_untouched(
    client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    repository_id = _register_local(client, local_repo)
    first = _import(client, repository_id).json()
    commit_files(local_repo, {"src/app.py": "x = 2\n"})
    second = _import(client, repository_id).json()
    assert first["id"] != second["id"]
    before = _fingerprint(local_repo)
    expected_files = first["file_count"] + second["file_count"]

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert response.json() == {
        "repository_id": repository_id,
        "source_type": "local",
        "deleted_snapshots": 2,
        "deleted_files": expected_files,
        "removed_clone": False,
    }
    assert _totals(db_engine) == (0, 0, 0)
    assert _fingerprint(local_repo) == before  # 磁碟上的使用者 repo 原封不動
    assert client.get(f"{URL}/{repository_id}").status_code == 404
    assert client.get(f"{URL}/{repository_id}/snapshots").status_code == 404
    assert client.get(f"/api/v1/snapshots/{first['id']}").status_code == 404
    again = _delete(client, repository_id)
    assert again.status_code == 404
    assert _error_code(again) == "repository_not_found"


def test_remove_only_affects_the_requested_repository(
    client: TestClient, local_repo: Path, roots: Path, db_engine: Engine
) -> None:
    other = init_repo(roots / "other", files={"o.txt": "other\n"})
    removed_id = _register_local(client, local_repo)
    kept_id = _register_local(client, other)
    _import(client, removed_id)
    kept_snapshot = _import(client, kept_id).json()

    assert _delete(client, removed_id).status_code == 200

    assert _totals(db_engine) == (1, 1, kept_snapshot["file_count"])
    files = client.get(f"/api/v1/snapshots/{kept_snapshot['id']}/files")
    assert files.status_code == 200
    assert [item["path"] for item in files.json()["items"]] == ["o.txt"]
    assert client.get(f"{URL}/{kept_id}").status_code == 200


def test_remove_repository_without_snapshots(client: TestClient, local_repo: Path) -> None:
    repository_id = _register_local(client, local_repo)

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert response.json()["deleted_snapshots"] == 0
    assert response.json()["deleted_files"] == 0


@pytest.mark.parametrize("bad_id", ["0", "-1", str(2**31), str(2**63), "abc"])
def test_remove_rejects_out_of_range_ids_with_422(client: TestClient, bad_id: str) -> None:
    response = _delete(client, bad_id)

    assert response.status_code == 422
    assert _error_code(response) == "validation_error"


def test_removed_local_repository_can_be_registered_and_imported_again(
    client: TestClient, local_repo: Path
) -> None:
    repository_id = _register_local(client, local_repo)
    _import(client, repository_id)
    assert _delete(client, repository_id).status_code == 200

    response = client.post(URL, json={"path": str(local_repo)})

    assert response.status_code == 201
    new_id = response.json()["id"]
    assert new_id != repository_id
    assert _import(client, new_id).status_code == 201


# --- 匯入中的保護 ---


def test_fresh_importing_snapshot_blocks_removal_and_nothing_is_deleted(
    client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    repository_id = _register_local(client, local_repo)
    _add_snapshot(db_engine, repository_id, files=3, index=1)
    _add_snapshot(db_engine, repository_id, status="importing", index=2)

    response = _delete(client, repository_id)

    assert response.status_code == 409
    assert _error_code(response) == "repository_busy"
    assert _totals(db_engine) == (1, 2, 3)
    assert client.get(f"{URL}/{repository_id}").status_code == 200


def test_stale_importing_snapshot_does_not_block_removal(
    client: TestClient, local_repo: Path, db_engine: Engine, settings: Settings
) -> None:
    repository_id = _register_local(client, local_repo)
    stale = datetime.now(UTC) - timedelta(seconds=settings.import_stale_seconds + 60)
    _add_snapshot(db_engine, repository_id, status="importing", started_at=stale)

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert response.json()["deleted_snapshots"] == 1
    assert _totals(db_engine) == (0, 0, 0)


def test_failed_and_completed_snapshots_do_not_block_removal(
    client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    repository_id = _register_local(client, local_repo)
    _add_snapshot(db_engine, repository_id, status="failed", index=1)
    _add_snapshot(db_engine, repository_id, status="completed", files=2, index=2)

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert (response.json()["deleted_snapshots"], response.json()["deleted_files"]) == (2, 2)


def test_importing_github_repository_keeps_its_clone_when_removal_is_refused(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client).json()["id"]
    _add_snapshot(db_engine, repository_id, status="importing")
    before = _fingerprint(_clone_path(managed_dir))

    response = _delete(client, repository_id)

    assert response.status_code == 409
    assert _fingerprint(_clone_path(managed_dir)) == before
    assert _totals(db_engine)[0] == 1


# --- github 來源 ---


def test_remove_github_repository_deletes_the_clone_and_allows_registering_again(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    repository_id = _register_github(client).json()["id"]
    snapshot = _import(client, repository_id).json()
    clone = _clone_path(managed_dir)
    assert clone.is_dir()

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert response.json() == {
        "repository_id": repository_id,
        "source_type": "github",
        "deleted_snapshots": 1,
        "deleted_files": snapshot["file_count"],
        "removed_clone": True,
    }
    assert not clone.exists()
    assert not clone.parent.exists()  # 變空的 <owner> 目錄一併移除
    assert (managed_dir / "github").is_dir()  # github 目錄與受管根不受影響
    assert managed_dir.is_dir()
    assert _totals(db_engine) == (0, 0, 0)
    assert (remote.bare / "HEAD").exists()  # 「遠端」完全不受影響
    assert _delete(client, repository_id).status_code == 404

    again = _register_github(client)

    assert again.status_code == 201
    assert again.json()["canonical_local_path"] == str(clone.resolve())
    assert (clone / ".git").is_dir()
    assert _import(client, again.json()["id"]).status_code == 201


def test_remove_github_repository_keeps_other_repositories_and_other_owners(
    client: TestClient,
    remote: RemoteRepo,
    remotes: Path,
    fake_github: FakeGitHub,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    fake_github.add_repo("octocat/Sibling")
    fake_github.add_repo("someone/Else")
    RemoteRepo(remotes, "octocat", "Sibling", files={"s.txt": "s\n"})
    RemoteRepo(remotes, "someone", "Else", files={"e.txt": "e\n"})
    target_id = _register_github(client).json()["id"]
    sibling_id = _register_github(client, "octocat/Sibling").json()["id"]
    else_id = _register_github(client, "someone/Else").json()["id"]
    sibling_before = _fingerprint(_clone_path(managed_dir, "octocat", "Sibling"))
    else_before = _fingerprint(_clone_path(managed_dir, "someone", "Else"))

    assert _delete(client, target_id).status_code == 200

    assert not _clone_path(managed_dir).exists()
    # 同一 owner 還有其他 repo：owner 目錄保留
    assert (managed_dir / "github" / "octocat").is_dir()
    assert _fingerprint(_clone_path(managed_dir, "octocat", "Sibling")) == sibling_before
    assert _fingerprint(_clone_path(managed_dir, "someone", "Else")) == else_before
    assert _totals(db_engine)[0] == 2
    assert client.get(f"{URL}/{sibling_id}").status_code == 200
    assert client.get(f"{URL}/{else_id}").status_code == 200


def test_remove_github_repository_deletes_a_clone_with_a_link_inside_without_following_it(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, tmp_path: Path
) -> None:
    repository_id = _register_github(client).json()["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link_directory(_clone_path(managed_dir) / ".git" / "linked", outside)

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert not _clone_path(managed_dir).exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_remove_succeeds_when_the_clone_directory_was_already_deleted_by_hand(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client).json()["id"]
    shutil.rmtree(_clone_path(managed_dir).parent, onexc=_make_writable_and_retry)

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert response.json()["removed_clone"] is False
    assert _totals(db_engine) == (0, 0, 0)


def test_remove_succeeds_when_the_whole_managed_dir_is_gone(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client).json()["id"]
    shutil.rmtree(managed_dir, onexc=_make_writable_and_retry)

    response = _delete(client, repository_id)

    assert response.status_code == 200
    assert response.json()["removed_clone"] is False
    assert not managed_dir.exists()  # 移除不會順手建立受管目錄
    assert _totals(db_engine)[0] == 0


def _make_writable_and_retry(function: Any, path: str, error: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def test_remove_github_repository_without_managed_dir_returns_503_and_keeps_everything(
    client: TestClient,
    remote: RemoteRepo,
    settings: Settings,
    managed_dir: Path,
    local_repo: Path,
    db_engine: Engine,
) -> None:
    github_id = _register_github(client).json()["id"]
    local_id = _register_local(client, local_repo)
    before = _fingerprint(_clone_path(managed_dir))
    settings.managed_repos_dir = None

    response = _delete(client, github_id)

    assert response.status_code == 503
    assert _error_code(response) == "github_registration_disabled"
    assert _totals(db_engine)[0] == 2
    assert _fingerprint(_clone_path(managed_dir)) == before
    # local 來源不需要受管目錄，仍可移除
    assert _delete(client, local_id).status_code == 200
    # 設定恢復後重試即可
    settings.managed_repos_dir = managed_dir
    assert _delete(client, github_id).status_code == 200


def test_repo_lock_held_by_a_fetch_or_clone_returns_409_then_succeeds_after_release(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client).json()["id"]
    # 第二階段切片 5：D32 的行程內鎖已由 advisory lock 取代；clone／fetch（可能在別的程序）
    # 持有的是同一把獨占鎖，鍵是不分大小寫的 owner/name
    lock = acquire_advisory_lock(db_engine, repo_lock_key("github:octocat/hello-world"))
    assert lock is not None
    try:
        response = _delete(client, repository_id)
    finally:
        lock.release()

    assert response.status_code == 409
    assert _error_code(response) == "repository_busy"
    assert _clone_path(managed_dir).is_dir()
    assert _totals(db_engine)[0] == 1
    assert _delete(client, repository_id).status_code == 200


# --- 竄改的資料列：不刪任何檔案、資料列保留 ---


def _assert_refused(response: Any, db_engine: Engine, snapshots: int) -> None:
    assert response.status_code == 422
    assert _error_code(response) == "invalid_repository_path"
    assert _totals(db_engine)[0] == 1
    assert _totals(db_engine)[1] == snapshots


def test_remove_refuses_a_row_whose_path_points_outside_the_managed_dir(
    client: TestClient,
    remote: RemoteRepo,
    roots: Path,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    repository_id = _register_github(client).json()["id"]
    _add_snapshot(db_engine, repository_id)
    victim = init_repo(roots / "victim")
    victim_before = _fingerprint(victim)
    clone_before = _fingerprint(_clone_path(managed_dir))
    _update_row(db_engine, repository_id, canonical_local_path=str(victim))

    response = _delete(client, repository_id)

    _assert_refused(response, db_engine, snapshots=1)
    assert _fingerprint(victim) == victim_before
    assert _fingerprint(_clone_path(managed_dir)) == clone_before


def test_remove_refuses_a_row_whose_path_points_to_another_clone(
    client: TestClient,
    remote: RemoteRepo,
    remotes: Path,
    fake_github: FakeGitHub,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    fake_github.add_repo("octocat/Other")
    RemoteRepo(remotes, "octocat", "Other", files={"o.txt": "o\n"})
    first = _register_github(client).json()
    second = _register_github(client, "octocat/Other").json()
    other_before = _fingerprint(_clone_path(managed_dir, "octocat", "Other"))
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM repositories WHERE id = :i"), {"i": second["id"]})
    _update_row(db_engine, first["id"], canonical_local_path=second["canonical_local_path"])

    response = _delete(client, first["id"])

    _assert_refused(response, db_engine, snapshots=0)
    assert _fingerprint(_clone_path(managed_dir, "octocat", "Other")) == other_before
    assert _clone_path(managed_dir).is_dir()


@pytest.mark.parametrize(
    ("owner", "name", "victim_relative"),
    [
        ("octocat", "..", "github/octocat"),
        ("..", "victim", "victim"),
        ("octocat", ".", "github/octocat"),
    ],
)
def test_remove_refuses_dot_names_in_the_row_and_deletes_nothing(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
    owner: str,
    name: str,
    victim_relative: str,
) -> None:
    repository_id = _register_github(client).json()["id"]
    (managed_dir / "victim").mkdir()
    (managed_dir / "victim" / "keep.txt").write_text("keep", encoding="utf-8")
    managed_before = _fingerprint(managed_dir)
    forged = f"{managed_dir.resolve()}\\github\\{owner}\\{name}"
    _update_row(
        db_engine,
        repository_id,
        github_owner=owner,
        github_name=name,
        canonical_local_path=forged,
    )

    response = _delete(client, repository_id)

    _assert_refused(response, db_engine, snapshots=0)
    assert (managed_dir / victim_relative).exists()
    assert _fingerprint(managed_dir) == managed_before


def test_remove_refuses_a_clone_directory_replaced_by_a_link_to_the_outside(
    client: TestClient, remote: RemoteRepo, roots: Path, managed_dir: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client).json()["id"]
    outside = init_repo(roots / "outside")
    outside_before = _fingerprint(outside)
    clone = _clone_path(managed_dir)
    managed_repos.remove_created_tree(
        clone, managed_root=managed_dir.resolve(), expected_parent=clone.parent
    )
    link_directory(clone, outside)

    response = _delete(client, repository_id)

    _assert_refused(response, db_engine, snapshots=0)
    assert _fingerprint(outside) == outside_before
    assert clone.exists()  # 連結本身也沒有被刪除


def test_remove_refuses_an_owner_directory_replaced_by_a_link_to_the_outside(
    client: TestClient, remote: RemoteRepo, tmp_path: Path, managed_dir: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client).json()["id"]
    outside = tmp_path / "outside-owner"
    shutil.move(str(_clone_path(managed_dir).parent), str(outside))
    link_directory(managed_dir / "github" / "octocat", outside)
    outside_before = _fingerprint(outside)

    response = _delete(client, repository_id)

    _assert_refused(response, db_engine, snapshots=0)
    assert _fingerprint(outside) == outside_before
    assert (outside / "Hello-World" / ".git").is_dir()


# --- 失敗注入：資料列保留、之後可重試 ---


def test_directory_removal_failure_returns_500_keeps_the_row_and_can_be_retried(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = _register_github(client).json()["id"]
    _add_snapshot(db_engine, repository_id, files=4)
    clone = _clone_path(managed_dir)

    def locked(*_: object, **__: object) -> None:
        raise PermissionError(13, "file is in use", str(clone))

    monkeypatch.setattr(shutil, "rmtree", locked)

    response = _delete(client, repository_id)

    assert response.status_code == 500
    assert _error_code(response) == "managed_storage_error"
    assert str(clone) not in response.text
    assert str(managed_dir) not in response.text
    assert _totals(db_engine) == (1, 1, 4)
    assert (clone / ".git").is_dir()

    monkeypatch.undo()
    retry = _delete(client, repository_id)

    assert retry.status_code == 200
    assert retry.json()["removed_clone"] is True
    assert not clone.exists()
    assert _totals(db_engine) == (0, 0, 0)


def test_database_failure_after_the_clone_was_removed_can_be_retried(
    app: FastAPI,
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    db_engine: Engine,
) -> None:
    repository_id = _register_github(client).json()["id"]
    _add_snapshot(db_engine, repository_id, files=4)
    undo = _fail_statements(_sqlalchemy_engine(app), "DELETE FROM REPOSITORIES")

    try:
        response = _delete(client, repository_id)
    finally:
        undo()

    assert response.status_code == 503
    assert _totals(db_engine) == (1, 1, 4)  # 同一交易：快照與檔案也一起 rollback
    assert not _clone_path(managed_dir).exists()  # 目錄已刪（無法與資料庫交易一起 rollback）

    retry = _delete(client, repository_id)

    assert retry.status_code == 200
    assert retry.json()["removed_clone"] is False  # 目錄已不存在視為成功
    assert retry.json()["deleted_files"] == 4
    assert _totals(db_engine) == (0, 0, 0)


def test_database_failure_on_a_local_repository_leaves_no_partial_deletion(
    app: FastAPI, client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    repository_id = _register_local(client, local_repo)
    _add_snapshot(db_engine, repository_id, files=5, index=1)
    _add_snapshot(db_engine, repository_id, files=3, index=2)
    undo = _fail_statements(_sqlalchemy_engine(app), "DELETE FROM REPOSITORIES")

    try:
        response = _delete(client, repository_id)
    finally:
        undo()

    assert response.status_code == 503
    assert _totals(db_engine) == (1, 2, 8)
    assert _delete(client, repository_id).status_code == 200
    assert _totals(db_engine) == (0, 0, 0)


# --- 大量資料 ---


def test_remove_repository_with_max_size_snapshot_uses_a_few_sql_statements(
    app: FastAPI, client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    repository_id = _register_local(client, local_repo)
    _add_snapshot(db_engine, repository_id, files=20_000)
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    engine = _sqlalchemy_engine(app)
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = _delete(client, repository_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    assert response.json()["deleted_files"] == 20_000
    assert _totals(db_engine) == (0, 0, 0)
    # 與檔案數無關：查詢 repo、鎖列、importing 檢查、進行中 job 檢查（切片 5 新增的一個 SELECT）、
    # 三個 DELETE 與少量讀取
    assert len(statements) <= 9
    # SQL 層級刪除：不載入 snapshot_files 的任何資料列
    assert not any(
        statement.lstrip().upper().startswith("SELECT") and "snapshot_files" in statement
        for statement in statements
    )


# --- 併發 ---


def test_concurrent_removals_of_the_same_local_repository_have_one_winner(
    client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    repository_id = _register_local(client, local_repo)
    _add_snapshot(db_engine, repository_id, files=200)
    workers = 6
    barrier = threading.Barrier(workers)

    def attempt(_: int) -> int:
        barrier.wait()
        return int(_delete(client, repository_id).status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(attempt, range(workers)))

    # 第二階段切片 3：移除先 try 取得 repo 級獨占 advisory lock（規格 5.11），贏家持鎖期間到達的
    # 輸家得到 409 repository_busy；較晚到者看到資料列已不在得到 404。只有一個贏家、沒有 500
    assert statuses.count(200) == 1
    assert set(statuses) <= {200, 404, 409}
    assert _totals(db_engine) == (0, 0, 0)


def test_concurrent_removals_of_the_same_github_repository_have_one_winner(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client).json()["id"]
    workers = 4
    barrier = threading.Barrier(workers)

    def attempt(_: int) -> int:
        barrier.wait()
        return int(_delete(client, repository_id).status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(attempt, range(workers)))

    # 輸家只可能是 404（資料列已不在）或 409（贏家持有 repo 級鎖時），不會有 500
    assert statuses.count(200) == 1
    assert set(statuses) <= {200, 404, 409}
    assert not _clone_path(managed_dir).exists()
    assert _totals(db_engine) == (0, 0, 0)
    assert _register_github(client).status_code == 201


def test_removal_racing_with_imports_never_returns_500_or_leaves_orphans(
    client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    for number in range(2, 5):
        commit_files(local_repo, {"src/app.py": f"x = {number}\n"})
    revisions = ["HEAD", "HEAD~1", "HEAD~2"]
    for _round in range(4):
        repository_id = _register_local(client, local_repo)
        barrier = threading.Barrier(len(revisions) + 1)

        def do_import(
            revision: str, rid: int = repository_id, gate: threading.Barrier = barrier
        ) -> int:
            gate.wait()
            return int(_import(client, rid, revision=revision).status_code)

        def do_delete(rid: int = repository_id, gate: threading.Barrier = barrier) -> int:
            gate.wait()
            return int(_delete(client, rid).status_code)

        with ThreadPoolExecutor(max_workers=len(revisions) + 1) as pool:
            deleter = pool.submit(do_delete)
            importers = [pool.submit(do_import, revision) for revision in revisions]
            delete_status = deleter.result(timeout=60)
            import_statuses = [future.result(timeout=60) for future in importers]

        assert delete_status in (200, 409)
        assert set(import_statuses) <= {200, 201, 404, 409}
        if delete_status == 409:  # 匯入先取得所有權：等匯入結束後再移除
            assert _delete(client, repository_id).status_code == 200
        # 不論誰先：沒有殘留的 repo、快照或檔案列
        assert _totals(db_engine) == (0, 0, 0)


def test_import_blocked_by_a_removal_in_progress_returns_404_after_it_commits(
    client: TestClient, local_repo: Path, db_engine: Engine
) -> None:
    repository_id = _register_local(client, local_repo)
    lock_connection = db_engine.connect()
    transaction = lock_connection.begin()
    lock_connection.execute(
        text("SELECT id FROM repositories WHERE id = :i FOR UPDATE"), {"i": repository_id}
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_import, client, repository_id)
            _wait_until_a_query_is_blocked(db_engine)  # 匯入的 INSERT 在等移除交易
            lock_connection.execute(
                text("DELETE FROM repositories WHERE id = :i"), {"i": repository_id}
            )
            transaction.commit()
            response = future.result(timeout=30)
    finally:
        lock_connection.close()

    assert response.status_code == 404
    assert _error_code(response) == "repository_not_found"
    assert _totals(db_engine) == (0, 0, 0)


def _import_with_failing_preflight(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_removal: bool,
) -> Any:
    """匯入的 Git 前置檢查失敗（例如 clone 目錄剛被移除），同時另一個交易持有列鎖進行移除。"""

    def failing_preflight(*_: object, **__: object) -> None:
        raise GitCommandError("simulated: the directory vanished")

    monkeypatch.setattr("repopilot.services.snapshots.ensure_repository_root", failing_preflight)
    lock_connection = db_engine.connect()
    transaction = lock_connection.begin()
    lock_connection.execute(
        text("SELECT id FROM repositories WHERE id = :i FOR UPDATE"), {"i": repository_id}
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_import, client, repository_id)
            _wait_until_a_query_is_blocked(db_engine)  # 在等移除交易的結果
            if commit_removal:
                lock_connection.execute(
                    text("DELETE FROM repositories WHERE id = :i"), {"i": repository_id}
                )
                transaction.commit()
            else:
                transaction.rollback()
            return future.result(timeout=30)
    finally:
        lock_connection.close()


def test_import_whose_git_preflight_fails_because_of_a_removal_returns_404(
    client: TestClient, local_repo: Path, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id = _register_local(client, local_repo)

    response = _import_with_failing_preflight(
        client, repository_id, db_engine, monkeypatch, commit_removal=True
    )

    assert response.status_code == 404
    assert _error_code(response) == "repository_not_found"


def test_import_whose_git_preflight_fails_keeps_its_own_error_when_removal_rolled_back(
    client: TestClient, local_repo: Path, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id = _register_local(client, local_repo)

    response = _import_with_failing_preflight(
        client, repository_id, db_engine, monkeypatch, commit_removal=False
    )

    assert response.status_code == 500
    assert _error_code(response) == "git_command_failed"
    assert _totals(db_engine)[0] == 1


# --- 日誌與秘密 ---


def test_removal_logs_a_summary_without_token_or_paths(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine
) -> None:
    assert client.put("/api/v1/github/token", json={"token": FAKE_TOKEN}).status_code == 200
    repository_id = _register_github(client).json()["id"]
    _add_snapshot(db_engine, repository_id, files=2)

    with capture_logs() as logs:
        response = _delete(client, repository_id)

    assert response.status_code == 200
    text_logs = logs.text()
    assert "repository_removed" in text_logs
    assert '"deleted_files": 2' in text_logs
    assert '"removed_clone": true' in text_logs
    for secret in (FAKE_TOKEN, str(managed_dir), str(managed_dir.resolve())):
        assert secret not in text_logs
        assert secret not in response.text
    # 刪除 repo 不會刪除已保存的 token
    assert client.get("/api/v1/github/auth").json()["authenticated"] is True
