"""切片 6：匯入的併發（多執行緒、多資料庫連線；PostgreSQL ＋ 真實暫存 Git repo）。

每個請求在 TestClient 的 threadpool 內各自取得獨立的 Session 與連線；同步以 `threading.Barrier`
讓請求同時出發。斷言的是「不論誰先」都成立的不變量，不依賴 `sleep` 判斷成功。
"""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.errors import SnapshotImportInProgressError
from repopilot.main import create_app
from repopilot.services import snapshots as snapshot_service
from repopilot.services.import_profile import ImportProfile
from repopilot.services.snapshots import import_snapshot
from tests.git_helpers import RemoteRepo, commit_files, git, init_repo
from tests.github_helpers import FakeGitHub

URL = "/api/v1/repositories"
SNAPSHOTS = "/api/v1/snapshots"


def _register(client: TestClient, repo: Path) -> int:
    response = client.post(URL, json={"path": str(repo)})
    assert response.status_code in (200, 201)
    repository_id: int = response.json()["id"]
    return repository_id


def _import(client: TestClient, repository_id: int, **body: Any) -> Any:
    return client.post(f"{URL}/{repository_id}/imports", json=body)


def _run_together(workers: int, task: Any) -> list[Any]:
    barrier = threading.Barrier(workers)

    def attempt(index: int) -> Any:
        barrier.wait()
        return task(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [
            future.result(timeout=120)
            for future in [pool.submit(attempt, i) for i in range(workers)]
        ]


def _snapshots(db_engine: Engine) -> list[Snapshot]:
    with Session(db_engine) as session:
        return list(session.scalars(select(Snapshot).order_by(Snapshot.id)))


def _file_rows(db_engine: Engine, snapshot_id: int) -> list[tuple[str, str | None]]:
    with Session(db_engine) as session:
        rows = session.execute(
            select(SnapshotFile.path, SnapshotFile.content).where(
                SnapshotFile.snapshot_id == snapshot_id
            )
        ).all()
        return sorted((path, content) for path, content in rows)


def test_concurrent_imports_of_different_commits_create_independent_complete_snapshots(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "versions", files={"version.txt": "v0\n", "keep.txt": "keep\n"})
    shas = [git(repo, "rev-parse", "HEAD")]
    for number in range(1, 6):
        shas.append(
            commit_files(repo, {"version.txt": f"v{number}\n", f"added{number}.txt": "n\n"})
        )
    repository_id = _register(client, repo)

    responses = _run_together(len(shas), lambda i: _import(client, repository_id, revision=shas[i]))

    assert [r.status_code for r in responses] == [201] * len(shas)
    snapshots = _snapshots(db_engine)
    assert sorted(s.commit_sha for s in snapshots) == sorted(shas)
    for number, sha in enumerate(shas):
        snapshot = next(s for s in snapshots if s.commit_sha == sha)
        assert snapshot.status == "completed"
        rows = dict(_file_rows(db_engine, snapshot.id))
        # 各快照只含自己那個 commit 的內容，沒有混入其他 commit 的檔案或版本
        assert rows["version.txt"] == f"v{number}\n"
        assert set(rows) == {
            "version.txt",
            "keep.txt",
            *(f"added{n}.txt" for n in range(1, number + 1)),
        }
        assert (snapshot.file_count, snapshot.skipped_count) == (len(rows), 0)


def test_concurrent_http_imports_of_the_same_commit_yield_one_created_and_no_duplicate_files(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "same", files={f"f{i}.txt": f"{i}\n" for i in range(40)})
    repository_id = _register(client, repo)

    responses = _run_together(8, lambda _: _import(client, repository_id))

    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1
    assert set(statuses) <= {200, 201, 409}
    [snapshot] = _snapshots(db_engine)
    assert snapshot.status == "completed"
    assert len(_file_rows(db_engine, snapshot.id)) == 40
    for response in responses:
        if response.status_code in (200, 201):
            assert response.json()["id"] == snapshot.id
    assert _import(client, repository_id).status_code == 200


def test_concurrent_imports_of_the_same_commit_with_different_profiles_are_independent(
    settings: Settings, clean_repositories: None, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(
        roots / "profiles", files={"small.txt": "tiny\n", "large.txt": "L" * 500 + "\n"}
    )
    profiles = [
        settings.model_copy(update={"import_max_file_bytes": limit})
        for limit in (100, 1000, 10_000)
    ]
    with TestClient(create_app(settings)) as client:
        repository_id = _register(client, repo)
        factory = client.app.state.session_factory  # type: ignore[attr-defined]

        def run(index: int) -> tuple[int, bool]:
            with factory() as session:
                snapshot, created = import_snapshot(session, repository_id, "HEAD", profiles[index])
                return snapshot.id, created

        results = _run_together(len(profiles), run)

    assert all(created for _, created in results)
    snapshots = {s.import_profile_hash: s for s in _snapshots(db_engine)}
    assert len(snapshots) == len(profiles)
    for profile in profiles:
        snapshot = snapshots[ImportProfile.from_settings(profile).hash()]
        assert snapshot.status == "completed"
        reasons = dict(_skip_reasons(db_engine, snapshot.id))
        expected = "too_large" if profile.import_max_file_bytes < 501 else None
        assert reasons == {"small.txt": None, "large.txt": expected}


def _skip_reasons(db_engine: Engine, snapshot_id: int) -> list[tuple[str, str | None]]:
    with Session(db_engine) as session:
        rows = session.execute(
            select(SnapshotFile.path, SnapshotFile.skip_reason).where(
                SnapshotFile.snapshot_id == snapshot_id
            )
        ).all()
        return [(path, reason) for path, reason in rows]


@pytest.mark.parametrize("initial", ["stale_importing", "failed"])
def test_concurrent_takeover_of_a_stale_or_failed_snapshot_has_exactly_one_new_owner(
    settings: Settings,
    clean_repositories: None,
    roots: Path,
    db_engine: Engine,
    initial: str,
) -> None:
    repo = init_repo(roots / "takeover", files={f"f{i}.txt": f"{i}\n" for i in range(30)})
    with TestClient(create_app(settings)) as client:
        repository_id = _register(client, repo)
        factory = client.app.state.session_factory  # type: ignore[attr-defined]
        sha = git(repo, "rev-parse", "HEAD")
        with factory() as session:
            leftover = Snapshot(
                repository_id=repository_id,
                commit_sha=sha,
                import_profile_hash=ImportProfile.from_settings(settings).hash(),
                status="importing" if initial == "stale_importing" else "failed",
                started_at=datetime.now(UTC) - timedelta(hours=2),
                error_summary=None if initial == "stale_importing" else "earlier failure",
            )
            session.add(leftover)
            session.commit()
            leftover_id = leftover.id

        def run(_: int) -> tuple[str, bool]:
            with factory() as session:
                try:
                    snapshot, created = import_snapshot(session, repository_id, "HEAD", settings)
                except SnapshotImportInProgressError:
                    return "in_progress", False
                return snapshot.status, created

        results = _run_together(8, run)

    # 條件式 UPDATE 只讓一個請求接手；其他請求若晚到，只會看到進行中（409）或已完成（重用）
    assert sum(1 for _, created in results if created) == 1
    assert {status for status, _ in results} <= {"in_progress", "completed"}
    [snapshot] = _snapshots(db_engine)
    assert snapshot.id == leftover_id
    assert snapshot.status == "completed"
    assert snapshot.error_summary is None
    assert len(_file_rows(db_engine, snapshot.id)) == 30


@pytest.mark.parametrize("initial", ["stale_importing", "failed"])
def test_only_the_first_of_two_claims_on_the_same_leftover_snapshot_wins(
    client: TestClient, roots: Path, db_engine: Engine, initial: str
) -> None:
    """兩個請求都讀到「可接手」的狀態後依序接手：條件式 UPDATE 只讓第一個成功。

    這是確定性的版本（不靠執行緒時序）：第二次接手時該列已是新鮮的 importing，必須回傳 None。
    """
    repo = init_repo(roots / "claim")
    repository_id = _register(client, repo)
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    old = datetime.now(UTC) - timedelta(hours=2)
    with factory() as session:
        leftover = Snapshot(
            repository_id=repository_id,
            commit_sha=git(repo, "rev-parse", "HEAD"),
            import_profile_hash="p" * 64,
            status="importing" if initial == "stale_importing" else "failed",
            started_at=old,
            error_summary="earlier failure" if initial == "failed" else None,
        )
        session.add(leftover)
        session.commit()
        snapshot_id = leftover.id
    cutoff = datetime.now(UTC) - timedelta(seconds=1800)

    with factory() as first_session, factory() as second_session:
        first = snapshot_service._claim_snapshot(first_session, snapshot_id, cutoff)
        second = snapshot_service._claim_snapshot(second_session, snapshot_id, cutoff)

    assert first is not None
    assert second is None
    [snapshot] = _snapshots(db_engine)
    assert snapshot.status == "importing"
    assert snapshot.started_at == first.started_at > old
    assert snapshot.error_summary is None


def test_snapshot_is_never_readable_as_partial_data_while_an_import_is_in_progress(
    client: TestClient,
    roots: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """讀取端在匯入期間輪詢：未 completed 時檔案端點一律 409，completed 後一次看到全部檔案。"""
    total = 600
    repo = init_repo(roots / "poll", files={f"d{i % 10}/f{i}.txt": f"{i}\n" for i in range(total)})
    repository_id = _register(client, repo)
    observed_importing = threading.Event()
    real_complete = snapshot_service._complete_snapshot

    def wait_until_a_reader_saw_importing(*args: Any, **kwargs: Any) -> None:
        assert observed_importing.wait(timeout=30), "讀取端沒有在匯入期間觀察到 importing"
        real_complete(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "INSERT_BATCH_SIZE", 25)
    monkeypatch.setattr(snapshot_service, "_complete_snapshot", wait_until_a_reader_saw_importing)
    observations: list[tuple[str, int, int | None]] = []
    stop = threading.Event()

    def importer() -> int:
        try:
            return int(_import(client, repository_id).status_code)
        finally:
            stop.set()

    def reader() -> None:
        while not stop.is_set():
            listing = client.get(f"{URL}/{repository_id}/snapshots").json()["items"]
            if not listing:
                continue
            snapshot_id = listing[0]["id"]
            status = client.get(f"{SNAPSHOTS}/{snapshot_id}").json()["status"]
            files = client.get(f"{SNAPSHOTS}/{snapshot_id}/files?limit=1")
            total_seen = files.json().get("total") if files.status_code == 200 else None
            observations.append((status, files.status_code, total_seen))
            if status == "importing" and files.status_code == 409:
                observed_importing.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(reader)
        writing = pool.submit(importer)
        assert writing.result(timeout=120) == 201
        reading.result(timeout=120)

    # 匯入被擋住期間，讀取端確實看到過「importing 且檔案端點 409」
    assert ("importing", 409, None) in observations
    for status, files_status, total_seen in observations:
        if files_status == 200:
            # 檔案端點一旦能讀，就是完整的 600 個檔案，不會有「讀到一半」的總數
            assert total_seen == total
        else:
            assert (files_status, total_seen) == (409, None)
            assert status == "importing"  # 409 表示尚未 completed，之前讀到的狀態只可能是 importing
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(SnapshotFile)) == total


# --- 匯入與 fetch 競爭（GitHub 來源） ---


@pytest.fixture
def fake_github() -> FakeGitHub:
    github = FakeGitHub()
    github.add_repo("octocat/Hello-World")
    return github


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    directory = tmp_path / "remotes"
    directory.mkdir()
    return directory


@pytest.fixture
def github_client(
    test_database_url: str,
    roots: Path,
    tmp_path: Path,
    remotes: Path,
    fake_github: FakeGitHub,
    clean_repositories: None,
) -> Iterator[TestClient]:
    settings = Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        managed_repos_dir=tmp_path / "managed",
        github_clone_base_url=remotes.as_uri(),
        github_clone_timeout_seconds=60,
    )
    app = create_app(settings)
    app.dependency_overrides[get_github_client] = fake_github.client
    with TestClient(app) as test_client:
        yield test_client


def test_imports_racing_with_fetch_always_store_the_content_of_the_commit_they_record(
    github_client: TestClient, remotes: Path, db_engine: Engine
) -> None:
    """fetch 寫入新的 Git 物件與 refs 時，同時匯入 `origin/main` 與舊 commit：

    不論 `origin/main` 在匯入解析時指向舊或新 commit，快照記錄的 SHA 與其內容都必須一致，
    且不會有 5xx（例如讀到寫到一半的物件）。
    """
    remote = RemoteRepo(
        remotes, "octocat", "Hello-World", files={"version.txt": "v0\n", "stable.txt": "same\n"}
    )
    registered = github_client.post(f"{URL}/github", json={"repo": "octocat/Hello-World"})
    assert registered.status_code == 201
    repository_id = registered.json()["id"]
    first_sha = remote.head()
    assert _import(github_client, repository_id, revision=first_sha).status_code == 201
    seen: dict[str, str] = {first_sha: "v0\n"}

    for number in range(1, 4):
        new_sha = remote.commit({"version.txt": f"v{number}\n", f"added{number}.txt": "n\n"})
        seen[new_sha] = f"v{number}\n"

        def task(index: int) -> tuple[str, int, Any]:
            if index == 0:
                response = github_client.post(f"{URL}/{repository_id}/fetch")
                return "fetch", response.status_code, None
            revision = "origin/main" if index % 2 else first_sha
            response = _import(github_client, repository_id, revision=revision)
            return "import", response.status_code, response.json()

        results = _run_together(5, task)

        for kind, status, body in results:
            if kind == "fetch":
                assert status == 200
                continue
            assert status in (200, 201, 409), (status, body)
            if status == 409:
                continue
            assert body["commit_sha"] in seen
            assert body["status"] == "completed"
            rows = dict(_file_rows(db_engine, body["id"]))
            assert rows["version.txt"] == seen[body["commit_sha"]]
            assert rows["stable.txt"] == "same\n"
        # fetch 完成後，`origin/main` 一定指向最新 commit
        latest = _import(github_client, repository_id, revision="origin/main")
        assert latest.status_code in (200, 201)
        assert latest.json()["commit_sha"] == new_sha
        assert dict(_file_rows(db_engine, latest.json()["id"]))["version.txt"] == f"v{number}\n"

    assert all(snapshot.status == "completed" for snapshot in _snapshots(db_engine))
    assert git(remote.work, "rev-parse", "HEAD") in seen
