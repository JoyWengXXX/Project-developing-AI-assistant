import hashlib
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.errors import GitCommandError, SnapshotImportInProgressError
from repopilot.main import create_app
from repopilot.services import snapshots as snapshot_service
from repopilot.services.import_profile import ImportProfile
from repopilot.services.snapshots import import_snapshot
from tests.git_helpers import commit_files, commit_special_entries, git, init_repo

URL = "/api/v1/repositories"


def _register(client: TestClient, repo: Path) -> int:
    response = client.post(URL, json={"path": str(repo)})
    assert response.status_code in (200, 201)
    repository_id: int = response.json()["id"]
    return repository_id


def _import(client: TestClient, repository_id: int, **body: Any) -> Any:
    return client.post(f"{URL}/{repository_id}/imports", json=body)


def _files(db_engine: Engine, snapshot_id: int) -> dict[str, SnapshotFile]:
    with Session(db_engine) as session:
        rows = session.scalars(
            select(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot_id)
        ).all()
        return {row.path: row for row in rows}


def _snapshot_count(db_engine: Engine) -> int:
    with Session(db_engine) as session:
        return session.scalar(select(func.count()).select_from(Snapshot)) or 0


def _file_count(db_engine: Engine) -> int:
    with Session(db_engine) as session:
        return session.scalar(select(func.count()).select_from(SnapshotFile)) or 0


def _snapshot(db_engine: Engine, snapshot_id: int) -> Snapshot:
    with Session(db_engine) as session:
        snapshot = session.get(Snapshot, snapshot_id)
        assert snapshot is not None
        return snapshot


@pytest.fixture
def repo(roots: Path) -> Path:
    return init_repo(roots / "demo", files={"README.md": "hello\n", "src/app.py": "x = 1\n"})


def test_import_head_creates_completed_snapshot(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)

    response = client.post(f"{URL}/{repository_id}/imports")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["commit_sha"] == git(repo, "rev-parse", "HEAD")
    assert body["repository_id"] == repository_id
    assert (body["file_count"], body["skipped_count"]) == (2, 0)
    assert body["total_bytes"] == len("hello\n") + len("x = 1\n")
    assert body["completed_at"] is not None
    assert body["error_summary"] is None
    files = _files(db_engine, body["id"])
    assert files["src/app.py"].content == "x = 1\n"
    assert files["src/app.py"].sha256 == hashlib.sha256(b"x = 1\n").hexdigest()
    assert files["src/app.py"].git_blob_id == git(repo, "rev-parse", "HEAD:src/app.py")
    assert files["src/app.py"].status == "imported"


def test_import_specific_commit_and_branch(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "feature")
    commit_files(repo, {"src/app.py": "x = 2\n"})
    repository_id = _register(client, repo)

    by_sha = _import(client, repository_id, revision=first).json()
    by_branch = _import(client, repository_id, revision="feature").json()
    head = _import(client, repository_id, revision="HEAD").json()

    assert by_sha["commit_sha"] == first
    assert by_branch["id"] == by_sha["id"]
    assert head["id"] != by_sha["id"]
    assert _files(db_engine, by_sha["id"])["src/app.py"].content == "x = 1\n"
    assert _files(db_engine, head["id"])["src/app.py"].content == "x = 2\n"


def test_uncommitted_changes_do_not_affect_import(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    (repo / "README.md").write_text("modified, not committed\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    git(repo, "rm", "--cached", "--quiet", "src/app.py")
    repository_id = _register(client, repo)

    body = _import(client, repository_id).json()

    files = _files(db_engine, body["id"])
    assert set(files) == {"README.md", "src/app.py"}
    assert files["README.md"].content == "hello\n"


def test_import_reflects_added_modified_and_deleted_files(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "--quiet", "README.md")
    second = commit_files(repo, {"src/app.py": "x = 2\n", "new.txt": "new\n"})
    repository_id = _register(client, repo)

    old = _import(client, repository_id, revision=first).json()
    new = _import(client, repository_id, revision=second).json()

    assert set(_files(db_engine, old["id"])) == {"README.md", "src/app.py"}
    new_files = _files(db_engine, new["id"])
    assert set(new_files) == {"src/app.py", "new.txt"}
    assert new_files["src/app.py"].content == "x = 2\n"


def test_duplicate_import_returns_200_and_reuses_snapshot(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    first = _import(client, repository_id)

    second = _import(client, repository_id, revision="main")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json()
    assert _snapshot_count(db_engine) == 1
    assert _file_count(db_engine) == 2


def test_different_import_profile_creates_separate_snapshot(
    client: TestClient, settings: Settings, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    first = _import(client, repository_id).json()

    other = settings.model_copy(update={"import_max_file_bytes": 4})
    with TestClient(create_app(other)) as other_client:
        second = _import(other_client, repository_id).json()

    assert second["id"] != first["id"]
    assert second["import_profile_hash"] != first["import_profile_hash"]
    assert second["commit_sha"] == first["commit_sha"]
    assert _snapshot_count(db_engine) == 2
    assert _files(db_engine, second["id"])["README.md"].skip_reason == "too_large"
    assert _files(db_engine, first["id"])["README.md"].status == "imported"


def test_paths_with_spaces_unicode_and_nesting(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    names = {"a b.txt": "1", "目錄/檔 案.py": "2", "x/y/z w/deep.txt": "3", "it's #1.txt": "4"}
    repo = init_repo(roots / "paths", files=names)
    repository_id = _register(client, repo)

    body = _import(client, repository_id).json()

    files = _files(db_engine, body["id"])
    assert set(files) == set(names)
    assert files["目錄/檔 案.py"].content == "2"


def test_content_classification(
    client: TestClient, settings: Settings, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "mixed", files={"seed.txt": "x"})
    png = b"\x89PNG\r\n\x1a\n\x00\x00"
    late_nul = b"a" * 9000 + b"\x00" + b"b"
    bad_utf8 = b"caf\xe9\n"
    crlf = b"line1\r\nline2\r\n"
    bom = b"\xef\xbb\xbfbom\n"
    commit_files(
        repo,
        {
            "bin.dat": png,
            "late_nul.dat": late_nul,
            "bad.txt": bad_utf8,
            "crlf.txt": crlf,
            "bom.txt": bom,
            "empty.txt": b"",
            "big.txt": b"z" * 20_000,
        },
    )
    repository_id = _register(client, repo)
    small = settings.model_copy(update={"import_max_file_bytes": 10_000})
    with TestClient(create_app(small)) as small_client:
        body = _import(small_client, repository_id).json()

    files = _files(db_engine, body["id"])
    assert (files["bin.dat"].status, files["bin.dat"].skip_reason) == ("skipped", "binary")
    assert files["bin.dat"].content is None
    assert files["bin.dat"].sha256 == hashlib.sha256(png).hexdigest()
    assert files["late_nul.dat"].skip_reason == "binary"
    assert files["bad.txt"].skip_reason == "invalid_utf8"
    assert files["bad.txt"].byte_size == len(bad_utf8)
    assert files["crlf.txt"].content == "line1\r\nline2\r\n"
    assert files["bom.txt"].content == "\ufeffbom\n"
    assert files["bom.txt"].sha256 == hashlib.sha256(bom).hexdigest()
    assert files["empty.txt"].content == ""
    assert files["empty.txt"].status == "imported"
    assert files["big.txt"].skip_reason == "too_large"
    assert files["big.txt"].content is None
    assert files["big.txt"].sha256 is None
    assert files["big.txt"].byte_size == 20_000
    assert body["skipped_count"] == 4  # bin、late_nul、bad、big
    assert body["file_count"] == 4  # seed、crlf、bom、empty
    # big.txt 未讀取，不計入 total_bytes
    read = [b"x", png, late_nul, bad_utf8, crlf, bom, b""]
    assert body["total_bytes"] == sum(len(data) for data in read)


def test_symlink_and_submodule_are_skipped_without_reading_targets(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "special", files={"secret.txt": "outside-target\n"})
    commit_special_entries(repo, symlinks={"link": "secret.txt"}, gitlinks={"vendor/lib": "c" * 40})
    repository_id = _register(client, repo)

    body = _import(client, repository_id).json()

    files = _files(db_engine, body["id"])
    assert (files["link"].status, files["link"].skip_reason) == ("skipped", "symlink")
    assert files["link"].content is None
    assert files["link"].sha256 is None
    assert files["link"].git_blob_id is not None
    assert (files["vendor/lib"].status, files["vendor/lib"].skip_reason) == (
        "skipped",
        "submodule",
    )
    assert files["vendor/lib"].git_blob_id is None
    assert files["vendor/lib"].byte_size is None
    assert files["secret.txt"].status == "imported"


def test_secret_files_are_excluded_without_reading_content(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(
        roots / "secrets",
        files={
            ".env": "TOKEN=super-secret-value\n",
            ".env.example": "TOKEN=\n",
            "deploy/server.pem": "-----BEGIN-----\n",
            "app.py": "x = 1\n",
        },
    )
    repository_id = _register(client, repo)

    body = _import(client, repository_id).json()

    files = _files(db_engine, body["id"])
    for name in (".env", "deploy/server.pem"):
        assert files[name].skip_reason == "excluded"
        assert files[name].content is None
        assert files[name].sha256 is None
    assert files[".env.example"].status == "imported"
    assert body["total_bytes"] == len("TOKEN=\n") + len("x = 1\n")
    assert "super-secret-value" not in str(body)


def test_file_count_limit_fails_import_and_stores_no_files(
    client: TestClient, settings: Settings, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    limited = settings.model_copy(update={"import_max_files": 1})

    with TestClient(create_app(limited)) as limited_client:
        response = _import(limited_client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "import_limit_exceeded"
    with Session(db_engine) as session:
        snapshot = session.scalars(select(Snapshot)).one()
    assert snapshot.status == "failed"
    assert snapshot.error_summary is not None
    assert _file_count(db_engine) == 0


def test_total_size_limit_fails_import(
    client: TestClient, settings: Settings, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    limited = settings.model_copy(update={"import_max_snapshot_bytes": 8})

    with TestClient(create_app(limited)) as limited_client:
        response = _import(limited_client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "import_limit_exceeded"
    assert _file_count(db_engine) == 0


def test_blobs_are_not_read_when_a_limit_is_exceeded(
    client: TestClient,
    settings: Settings,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = _register(client, repo)
    limited = settings.model_copy(update={"import_max_snapshot_bytes": 8})

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("blobs must not be read before the size check passes")

    monkeypatch.setattr(snapshot_service, "read_blobs", forbidden)

    with TestClient(create_app(limited)) as limited_client:
        response = _import(limited_client, repository_id)

    assert response.status_code == 422


@pytest.mark.parametrize("revision", ["no-such-branch", "-h", "HEAD^{tree}", "HEAD\nmain", ""])
def test_invalid_revision_returns_422_and_creates_no_snapshot(
    client: TestClient, repo: Path, db_engine: Engine, revision: str
) -> None:
    repository_id = _register(client, repo)

    response = _import(client, repository_id, revision=revision)

    assert response.status_code == 422
    assert _snapshot_count(db_engine) == 0
    if revision:
        assert response.json()["error"]["code"] == "invalid_revision"


def test_unknown_repository_returns_404(client: TestClient) -> None:
    response = client.post(f"{URL}/999/imports")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "repository_not_found"


def test_unknown_body_field_is_rejected(client: TestClient, repo: Path) -> None:
    repository_id = _register(client, repo)

    response = _import(client, repository_id, revision="HEAD", path="/etc")

    assert response.status_code == 422


def test_repository_removed_from_disk_returns_422(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "gone")
    repository_id = _register(client, repo)
    # .git 內的物件檔為唯讀，Windows 刪除前須先放寬權限
    shutil.rmtree(repo, onexc=lambda func, path, _exc: (os.chmod(path, 0o700), func(path)))

    response = _import(client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"
    assert _snapshot_count(db_engine) == 0


def test_failed_import_can_be_retried_and_exposes_no_partial_files(
    client: TestClient,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = _register(client, repo)
    real_read_blobs = snapshot_service.read_blobs

    def failing(*_: object, **__: object) -> None:
        raise GitCommandError("git cat-file failed: simulated")

    monkeypatch.setattr(snapshot_service, "read_blobs", failing)
    failed = _import(client, repository_id)

    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "git_command_failed"
    with Session(db_engine) as session:
        snapshot = session.scalars(select(Snapshot)).one()
    assert snapshot.status == "failed"
    assert snapshot.error_summary == "git cat-file failed: simulated"
    assert _file_count(db_engine) == 0

    monkeypatch.setattr(snapshot_service, "read_blobs", real_read_blobs)
    retried = _import(client, repository_id)

    assert retried.status_code == 201
    assert retried.json()["id"] == snapshot.id
    assert retried.json()["status"] == "completed"
    assert retried.json()["error_summary"] is None
    assert _snapshot_count(db_engine) == 1
    assert set(_files(db_engine, snapshot.id)) == {"README.md", "src/app.py"}


def test_database_error_during_file_insert_rolls_back_everything(
    client: TestClient,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = _register(client, repo)
    real_build = snapshot_service._build_records

    def duplicate_paths(*args: Any, **kwargs: Any) -> Any:
        records, stats = real_build(*args, **kwargs)
        return [*records, dict(records[0])], stats

    monkeypatch.setattr(snapshot_service, "_build_records", duplicate_paths)

    response = _import(client, repository_id)

    assert response.status_code == 500
    with Session(db_engine) as session:
        snapshot = session.scalars(select(Snapshot)).one()
    assert snapshot.status == "failed"
    assert snapshot.error_summary == "Unexpected IntegrityError."
    assert snapshot.completed_at is not None
    assert _file_count(db_engine) == 0


def test_concurrent_imports_create_exactly_one_snapshot(
    settings: Settings, clean_repositories: None, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "race", files={f"f{i}.txt": f"{i}\n" for i in range(30)})
    with TestClient(create_app(settings)) as client:
        repository_id = _register(client, repo)
        factory = client.app.state.session_factory  # type: ignore[attr-defined]

        workers = 6
        barrier = threading.Barrier(workers)

        def run() -> tuple[str, bool]:
            barrier.wait()
            with factory() as session:
                try:
                    snapshot, created = import_snapshot(session, repository_id, "HEAD", settings)
                except SnapshotImportInProgressError:
                    return "in_progress", False
                return snapshot.status, created

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda _: run(), range(workers)))

    assert sum(1 for _, created in results if created) == 1
    assert {status for status, _ in results} <= {"completed", "in_progress"}
    assert _snapshot_count(db_engine) == 1
    assert _file_count(db_engine) == 30


def test_fresh_importing_snapshot_returns_409(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    sha = git(repo, "rev-parse", "HEAD")
    _insert_snapshot(db_engine, repository_id, sha, started_at=datetime.now(UTC))

    response = _import(client, repository_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_import_in_progress"
    assert _file_count(db_engine) == 0


def test_stale_importing_snapshot_is_taken_over(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    sha = git(repo, "rev-parse", "HEAD")
    old = datetime.now(UTC) - timedelta(hours=2)
    stale_id = _insert_snapshot(db_engine, repository_id, sha, started_at=old)

    response = _import(client, repository_id)

    assert response.status_code == 201
    assert response.json()["id"] == stale_id
    assert response.json()["status"] == "completed"
    assert _snapshot_count(db_engine) == 1


def test_import_that_lost_ownership_does_not_overwrite_the_new_owner(
    client: TestClient,
    repo: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_id = _register(client, repo)
    real_build = snapshot_service._build_records

    def steal_then_build(*args: Any, **kwargs: Any) -> Any:
        with Session(db_engine) as session:
            session.execute(update(Snapshot).values(started_at=datetime.now(UTC)))
            session.commit()
        return real_build(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "_build_records", steal_then_build)

    response = _import(client, repository_id)

    assert response.status_code == 409
    with Session(db_engine) as session:
        snapshot = session.scalars(select(Snapshot)).one()
    assert snapshot.status == "importing"
    assert _file_count(db_engine) == 0


def test_failed_snapshot_with_leftover_files_is_cleaned_before_retry(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    sha = git(repo, "rev-parse", "HEAD")
    snapshot_id = _insert_snapshot(db_engine, repository_id, sha, status="failed")
    with Session(db_engine) as session:
        session.add(
            SnapshotFile(
                snapshot_id=snapshot_id,
                path="leftover.txt",
                content="stale",
                status="imported",
            )
        )
        session.commit()

    response = _import(client, repository_id)

    assert response.status_code == 201
    assert set(_files(db_engine, snapshot_id)) == {"README.md", "src/app.py"}


def test_database_rejects_inconsistent_file_rows(
    client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, repo)
    snapshot_id = _insert_snapshot(
        db_engine, repository_id, git(repo, "rev-parse", "HEAD"), status="failed"
    )

    with Session(db_engine) as session, pytest.raises(IntegrityError):
        session.add(SnapshotFile(snapshot_id=snapshot_id, path="x", status="imported"))
        session.commit()


def _insert_snapshot(
    db_engine: Engine,
    repository_id: int,
    commit_sha: str,
    *,
    status: str = "importing",
    started_at: datetime | None = None,
) -> int:
    profile_hash = ImportProfile.from_settings(Settings()).hash()
    with Session(db_engine) as session:
        snapshot = Snapshot(
            repository_id=repository_id,
            commit_sha=commit_sha,
            import_profile_hash=profile_hash,
            status=status,
            started_at=started_at or datetime.now(UTC),
        )
        session.add(snapshot)
        session.commit()
        return snapshot.id
