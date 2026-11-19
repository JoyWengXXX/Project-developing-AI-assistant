"""移除快照 API（第二階段切片 3b，D41）：各分支、互斥與保護、與匯入競爭、大型快照。

全部使用真實 PostgreSQL（db-test）。「busy」的三種來源（未逾時的 importing、進行中的 job、
快照鎖被持有）各有確定性測試：鎖由第二條連線持有，job 直接寫入資料庫，不靠時間或 sleep。
每個擋下的案例都斷言**什麼都沒被刪**（快照、檔案列、參考、job 都原封不動）。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import acquire_advisory_lock, snapshot_lock_key
from repopilot.services import snapshot_removal
from repopilot.services import snapshots as snapshot_service
from tests.git_helpers import commit_files, git, init_repo
from tests.integration.snapshot_helpers import (
    TEST_PROFILE,
    add_snapshot,
    commit_for,
    count,
    import_key_for,
    insert_job,
    wait_until_a_query_is_blocked,
)
from tests.lock_helpers import without_snapshot_lock

REPOS = "/api/v1/repositories"
SNAPSHOTS = "/api/v1/snapshots"
REFERENCES = "/api/v1/references"


@pytest.fixture
def repo(roots: Path) -> Path:
    return init_repo(roots / "demo", files={"README.md": "hello\n", "src/app.py": "x = 1\n"})


@pytest.fixture
def repository_id(client: TestClient, repo: Path) -> int:
    response = client.post(REPOS, json={"path": str(repo)})
    assert response.status_code in (200, 201), response.text
    return int(response.json()["id"])


def _import(client: TestClient, repository_id: int, revision: str = "HEAD") -> dict[str, Any]:
    response = client.post(f"{REPOS}/{repository_id}/imports", json={"revision": revision})
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


def _delete(client: TestClient, snapshot_id: int | str, **params: Any) -> Any:
    return client.delete(f"{SNAPSHOTS}/{snapshot_id}", params=params)


def _error_code(response: Any) -> str:
    code = response.json()["error"]["code"]
    assert isinstance(code, str)
    return code


def _set_reference(client: TestClient, repository_id: int, snapshot_id: int) -> None:
    response = client.put(f"{REPOS}/{repository_id}/reference", json={"snapshot_id": snapshot_id})
    assert response.status_code == 200, response.text


def _totals(engine: Engine) -> tuple[int, int, int]:
    """(snapshots, snapshot_files, repository_references) 的列數。"""
    return (
        count(engine, "snapshots"),
        count(engine, "snapshot_files"),
        count(engine, "repository_references"),
    )


def _lock_key_of(engine: Engine, snapshot_id: int) -> int:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT repository_id, commit_sha, import_profile_hash FROM snapshots WHERE id=:i"
            ),
            {"i": snapshot_id},
        ).one()
    return snapshot_lock_key(row[0], row[1], row[2])


# --- 正常移除 ---


def test_remove_deletes_the_snapshot_and_its_files_and_returns_the_summary(
    client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    kept = _import(client, repository_id)
    commit_files(repo, {"src/app.py": "x = 2\n", "extra.txt": "e\n"})
    removed = _import(client, repository_id)
    head_before = git(repo, "rev-parse", "HEAD")

    response = _delete(client, removed["id"])

    assert response.status_code == 200
    assert response.json() == {
        "snapshot_id": removed["id"],
        "repository_id": repository_id,
        "deleted_files": 3,
        "was_referenced": False,
    }
    assert client.get(f"{SNAPSHOTS}/{removed['id']}").status_code == 404
    assert client.get(f"{SNAPSHOTS}/{removed['id']}/files").status_code == 404
    assert count(db_engine, "snapshot_files", "snapshot_id = :s", s=removed["id"]) == 0
    # 別的快照與它的檔案原封不動；Git repo 沒被動到
    assert client.get(f"{SNAPSHOTS}/{kept['id']}").status_code == 200
    assert count(db_engine, "snapshot_files", "snapshot_id = :s", s=kept["id"]) == 2
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert (repo / "extra.txt").read_text(encoding="utf-8") == "e\n"
    listed = client.get(f"{REPOS}/{repository_id}/snapshots").json()
    assert [item["id"] for item in listed["items"]] == [kept["id"]]


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_completed_and_failed_snapshots_can_be_removed(
    client: TestClient, repository_id: int, db_engine: Engine, status: str
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, status=status, files=2)

    response = _delete(client, snapshot_id)

    assert response.status_code == 200
    assert response.json()["deleted_files"] == 2
    assert _totals(db_engine) == (0, 0, 0)


def test_removing_twice_or_an_unknown_snapshot_is_404(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id)

    first = _delete(client, snapshot_id)
    second = _delete(client, snapshot_id)
    unknown = _delete(client, 99999)

    assert first.status_code == 200
    assert (second.status_code, _error_code(second)) == (404, "snapshot_not_found")
    assert (unknown.status_code, _error_code(unknown)) == (404, "snapshot_not_found")


@pytest.mark.parametrize("bad_id", ["0", "-1", str(2**31), str(2**63), "abc"])
def test_remove_rejects_out_of_range_ids_with_422(client: TestClient, bad_id: str) -> None:
    response = _delete(client, bad_id)

    assert response.status_code == 422
    assert _error_code(response) == "validation_error"


def test_remove_rejects_a_non_boolean_force_with_422(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id)

    response = _delete(client, snapshot_id, force="maybe")

    assert response.status_code == 422
    assert _totals(db_engine) == (1, 0, 0)


def test_a_removed_snapshot_can_be_imported_again_at_the_same_commit(
    client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    first = _import(client, repository_id)
    assert _delete(client, first["id"]).status_code == 200

    response = client.post(f"{REPOS}/{repository_id}/imports", json={})

    assert response.status_code == 201  # 不是 200：唯一約束已釋放，重新建立了新快照
    again = response.json()
    assert again["id"] != first["id"]
    assert again["commit_sha"] == first["commit_sha"] and again["status"] == "completed"
    assert count(db_engine, "snapshot_files", "snapshot_id = :s", s=again["id"]) == 2


def test_removing_a_snapshot_keeps_job_history_with_a_null_snapshot_id(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id)
    job_id = insert_job(
        db_engine, "succeeded", repository_id=repository_id, snapshot_id=snapshot_id
    )

    assert _delete(client, snapshot_id).status_code == 200

    job = client.get(f"/api/v1/jobs/{job_id}")
    assert job.status_code == 200  # 歷史紀錄保留
    assert job.json()["snapshot_id"] is None  # D39：ON DELETE SET NULL
    assert job.json()["repository_id"] == repository_id


# --- busy：未逾時的 importing ---


def test_fresh_importing_snapshot_is_busy_and_nothing_is_deleted(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, status="importing")

    for params in ({}, {"force": "true"}):  # force 不能繞過 busy
        response = _delete(client, snapshot_id, **params)
        assert response.status_code == 409
        assert _error_code(response) == "snapshot_busy"

    assert _totals(db_engine) == (1, 0, 0)


def test_stale_importing_snapshot_can_be_removed(
    client: TestClient, repository_id: int, db_engine: Engine, settings: Settings
) -> None:
    stale = datetime.now(UTC) - timedelta(seconds=settings.import_stale_seconds + 60)
    snapshot_id = add_snapshot(db_engine, repository_id, status="importing", started_at=stale)

    response = _delete(client, snapshot_id)

    assert response.status_code == 200
    assert _totals(db_engine) == (0, 0, 0)


def test_importing_snapshot_just_inside_the_stale_window_is_still_busy(
    client: TestClient, repository_id: int, db_engine: Engine, settings: Settings
) -> None:
    fresh = datetime.now(UTC) - timedelta(seconds=settings.import_stale_seconds - 120)
    snapshot_id = add_snapshot(db_engine, repository_id, status="importing", started_at=fresh)

    assert _error_code(_delete(client, snapshot_id)) == "snapshot_busy"
    assert _totals(db_engine) == (1, 0, 0)


# --- busy：進行中的 job ---


@pytest.mark.parametrize("job_status", ["queued", "running"])
def test_active_job_pointing_at_the_snapshot_is_busy_and_nothing_is_deleted(
    client: TestClient, repository_id: int, db_engine: Engine, job_status: str
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=2)
    insert_job(db_engine, job_status, repository_id=repository_id, snapshot_id=snapshot_id)

    for params in ({}, {"force": "true"}):
        response = _delete(client, snapshot_id, **params)
        assert response.status_code == 409
        assert _error_code(response) == "snapshot_busy"

    assert _totals(db_engine) == (1, 2, 0)


@pytest.mark.parametrize("job_status", ["queued", "running"])
def test_active_import_job_for_the_same_dedupe_key_is_busy_even_before_it_has_a_snapshot_id(
    client: TestClient, repository_id: int, db_engine: Engine, job_status: str
) -> None:
    """匯入 job 在完成前 snapshot_id 是 NULL（D40）：必須靠 dedupe_key 找到它。"""
    snapshot_id = add_snapshot(db_engine, repository_id, status="failed", index=3)
    insert_job(
        db_engine,
        job_status,
        repository_id=repository_id,
        snapshot_id=None,
        dedupe_key=import_key_for(repository_id, 3),
    )

    response = _delete(client, snapshot_id)

    assert (response.status_code, _error_code(response)) == (409, "snapshot_busy")
    assert _totals(db_engine) == (1, 0, 0)


@pytest.mark.parametrize("job_status", ["succeeded", "failed", "cancelled"])
def test_finished_jobs_do_not_block_removal(
    client: TestClient, repository_id: int, db_engine: Engine, job_status: str
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, index=4)
    insert_job(db_engine, job_status, repository_id=repository_id, snapshot_id=snapshot_id)
    insert_job(
        db_engine,
        job_status,
        repository_id=repository_id,
        dedupe_key=import_key_for(repository_id, 4),
    )

    assert _delete(client, snapshot_id).status_code == 200


def test_active_jobs_of_other_snapshots_or_other_commits_do_not_block_removal(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, index=1)
    other_snapshot = add_snapshot(db_engine, repository_id, index=2)
    insert_job(db_engine, "queued", repository_id=repository_id, snapshot_id=other_snapshot)
    insert_job(
        db_engine,
        "running",
        repository_id=repository_id,
        dedupe_key=import_key_for(repository_id, 2),  # 另一個 commit 的匯入
    )
    insert_job(
        db_engine,
        "queued",
        repository_id=repository_id,
        dedupe_key=import_key_for(repository_id, 1, profile_hash="another-profile"),
    )  # 同 commit 但不同匯入設定＝不同快照

    assert _delete(client, snapshot_id).status_code == 200
    assert count(db_engine, "snapshots", "id = :i", i=other_snapshot) == 1


def test_an_active_job_blocks_even_a_forced_removal_of_a_referenced_snapshot(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=1)
    _set_reference(client, repository_id, snapshot_id)
    insert_job(db_engine, "running", repository_id=repository_id, snapshot_id=snapshot_id)

    response = _delete(client, snapshot_id, force="true")

    assert (response.status_code, _error_code(response)) == (409, "snapshot_busy")
    assert _totals(db_engine) == (1, 1, 1)


# --- busy：快照鎖 ---


def test_snapshot_lock_held_by_another_connection_is_busy_and_nothing_is_deleted(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=3)
    _set_reference(client, repository_id, snapshot_id)
    held = acquire_advisory_lock(db_engine, _lock_key_of(db_engine, snapshot_id))
    assert held is not None
    try:
        for params in ({}, {"force": "true"}):
            response = _delete(client, snapshot_id, **params)
            assert (response.status_code, _error_code(response)) == (409, "snapshot_busy")
        assert _totals(db_engine) == (1, 3, 1)
    finally:
        held.release()

    assert _delete(client, snapshot_id, force="true").status_code == 200  # 釋放後可以移除
    assert _totals(db_engine) == (0, 0, 0)


def test_the_lock_key_comes_from_the_snapshots_own_profile_hash(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    """快照用舊的匯入設定匯入時，持有的是『當時』的鎖；移除必須用快照自己的雜湊去取同一把。"""
    snapshot_id = add_snapshot(db_engine, repository_id, profile_hash="old-profile")
    held = acquire_advisory_lock(
        db_engine, snapshot_lock_key(repository_id, commit_for(1), "old-profile")
    )
    assert held is not None
    try:
        assert _error_code(_delete(client, snapshot_id)) == "snapshot_busy"
    finally:
        held.release()
    assert _delete(client, snapshot_id).status_code == 200


def test_snapshot_locks_are_per_snapshot(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    busy = add_snapshot(db_engine, repository_id, index=1)
    free = add_snapshot(db_engine, repository_id, index=2)
    held = acquire_advisory_lock(db_engine, _lock_key_of(db_engine, busy))
    assert held is not None
    try:
        assert _delete(client, busy).status_code == 409
        assert _delete(client, free).status_code == 200
    finally:
        held.release()


def test_removal_releases_its_snapshot_lock_after_success_and_after_refusal(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    referenced = add_snapshot(db_engine, repository_id, index=1)
    removable = add_snapshot(db_engine, repository_id, index=2)
    _set_reference(client, repository_id, referenced)
    assert _delete(client, referenced).status_code == 409  # 拒絕（referenced）後鎖要放掉
    assert _delete(client, removable).status_code == 200  # 成功後鎖要放掉

    for snapshot_id, index in ((referenced, 1), (removable, 2)):
        lock = acquire_advisory_lock(
            db_engine, snapshot_lock_key(repository_id, commit_for(index), TEST_PROFILE)
        )
        assert lock is not None, f"snapshot {snapshot_id} 的鎖沒有被釋放"
        lock.release()


# --- referenced 與 force ---


def test_referenced_snapshot_is_refused_without_force_and_nothing_is_deleted(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=2)
    _set_reference(client, repository_id, snapshot_id)

    response = _delete(client, snapshot_id)

    assert (response.status_code, _error_code(response)) == (409, "snapshot_referenced")
    assert _totals(db_engine) == (1, 2, 1)
    assert client.get(f"{SNAPSHOTS}/{snapshot_id}").json()["is_referenced"] is True
    refused_again = _delete(client, snapshot_id, force="false")
    assert _error_code(refused_again) == "snapshot_referenced"


def test_force_removes_the_referenced_snapshot_and_its_reference_in_one_go(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=2)
    _set_reference(client, repository_id, snapshot_id)

    response = _delete(client, snapshot_id, force="true")

    assert response.status_code == 200
    assert response.json() == {
        "snapshot_id": snapshot_id,
        "repository_id": repository_id,
        "deleted_files": 2,
        "was_referenced": True,
    }
    assert _totals(db_engine) == (0, 0, 0)
    assert client.get(REFERENCES).json() == {"items": []}


def test_force_on_an_unreferenced_snapshot_reports_was_referenced_false(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id)

    response = _delete(client, snapshot_id, force="true")

    assert response.status_code == 200
    assert response.json()["was_referenced"] is False


def test_removing_a_non_referenced_snapshot_keeps_the_reference_of_the_same_repository(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    referenced = add_snapshot(db_engine, repository_id, index=1, files=1)
    other = add_snapshot(db_engine, repository_id, index=2, files=1)
    _set_reference(client, repository_id, referenced)

    assert _delete(client, other).status_code == 200

    items = client.get(REFERENCES).json()["items"]
    assert [item["snapshot_id"] for item in items] == [referenced]
    assert _totals(db_engine) == (1, 1, 1)


def test_force_only_clears_the_reference_of_that_snapshot_not_other_repositories(
    client: TestClient, roots: Path, repository_id: int, db_engine: Engine
) -> None:
    other_repo = init_repo(roots / "other", files={"a.txt": "a\n"})
    other_id = int(client.post(REPOS, json={"path": str(other_repo)}).json()["id"])
    mine = add_snapshot(db_engine, repository_id)
    theirs = add_snapshot(db_engine, other_id)
    _set_reference(client, repository_id, mine)
    _set_reference(client, other_id, theirs)

    assert _delete(client, mine, force="true").status_code == 200

    items = client.get(REFERENCES).json()["items"]
    assert [item["snapshot_id"] for item in items] == [theirs]


def test_a_referenced_snapshot_can_be_referenced_again_after_it_is_reimported(
    client: TestClient, repository_id: int
) -> None:
    first = _import(client, repository_id)
    _set_reference(client, repository_id, first["id"])
    assert _delete(client, first["id"], force="true").status_code == 200

    again = _import(client, repository_id)
    _set_reference(client, repository_id, again["id"])

    assert [item["snapshot_id"] for item in client.get(REFERENCES).json()["items"]] == [again["id"]]


def test_a_reference_being_created_while_removing_makes_the_removal_see_it_and_refuse(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    """扮演『PUT 進行到一半』：第二條連線已寫入（未提交）參考。

    移除的 FOR UPDATE 必須等它提交，之後看見參考並回 409，而不是靜默把剛建立的參考一起刪掉。
    """
    snapshot_id = add_snapshot(db_engine, repository_id, files=2)
    put_connection = db_engine.connect()
    transaction = put_connection.begin()
    put_connection.execute(
        text("SELECT id FROM snapshots WHERE id = :i FOR KEY SHARE"), {"i": snapshot_id}
    )
    put_connection.execute(
        text("INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"),
        {"r": repository_id, "s": snapshot_id},
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_delete, client, snapshot_id)
            wait_until_a_query_is_blocked(db_engine)
            transaction.commit()
            response = future.result(timeout=30)
    finally:
        put_connection.close()

    assert (response.status_code, _error_code(response)) == (409, "snapshot_referenced")
    assert _totals(db_engine) == (1, 2, 1)


# --- 原子性 ---


def test_a_database_failure_during_removal_leaves_everything_in_place_and_can_be_retried(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=4)
    _set_reference(client, repository_id, snapshot_id)
    engine: Engine = client.app.state.session_factory.kw["bind"]  # type: ignore[attr-defined]

    def fail_the_last_delete(*args: Any) -> None:
        if str(args[2]).lstrip().upper().startswith("DELETE FROM SNAPSHOTS"):
            raise OperationalError(str(args[2]), None, Exception("injected failure"))

    event.listen(engine, "before_cursor_execute", fail_the_last_delete)
    try:
        response = _delete(client, snapshot_id, force="true")
    finally:
        event.remove(engine, "before_cursor_execute", fail_the_last_delete)

    assert response.status_code == 503
    # 檔案列與參考的 DELETE 已執行過，但同一交易 rollback：全部原封不動
    assert _totals(db_engine) == (1, 4, 1)
    retry = _delete(client, snapshot_id, force="true")
    assert retry.status_code == 200
    assert (retry.json()["deleted_files"], retry.json()["was_referenced"]) == (4, True)
    assert _totals(db_engine) == (0, 0, 0)


# --- 與匯入競爭 ---


def test_import_is_refused_while_a_removal_holds_the_snapshot_lock_and_works_after(
    client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    """扮演『移除進行中』：第二條連線持有該快照的鎖。匯入不得建立半套快照，釋放後恢復正常。"""
    first = _import(client, repository_id)
    assert _delete(client, first["id"]).status_code == 200
    held = acquire_advisory_lock(
        db_engine,
        snapshot_lock_key(repository_id, first["commit_sha"], first["import_profile_hash"]),
    )
    assert held is not None
    try:
        refused = client.post(f"{REPOS}/{repository_id}/imports", json={})
        assert (refused.status_code, _error_code(refused)) == (409, "snapshot_import_in_progress")
        assert _totals(db_engine) == (0, 0, 0)  # 沒有任何半套資料
    finally:
        held.release()

    assert client.post(f"{REPOS}/{repository_id}/imports", json={}).status_code == 201


def test_an_import_that_loses_its_snapshot_to_a_removal_fails_with_409_and_leaves_nothing(
    client: TestClient,
    repo: Path,
    repository_id: int,
    db_engine: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逾時的 importing 可被移除（與 D24 一致）；舊匯入若還在跑，完成時的條件式 UPDATE 找不到列。

    鎖失效時的資料面保證：關閉匯入端的快照鎖，模擬『舊匯入者已逾時、不再持鎖』。
    """
    without_snapshot_lock(monkeypatch)
    real_build = snapshot_service._build_records
    removals: list[int] = []

    def remove_while_building(*args: Any, **kwargs: Any) -> Any:
        with db_engine.begin() as connection:  # 讓這個 importing 快照逾時
            connection.execute(text("UPDATE snapshots SET started_at = now() - interval '1 day'"))
        with Session(db_engine) as other:
            snapshot_id = other.execute(text("SELECT id FROM snapshots")).scalar_one()
            removals.append(
                snapshot_removal.remove_snapshot(other, snapshot_id, settings).deleted_files
            )
        return real_build(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "_build_records", remove_while_building)

    response = client.post(f"{REPOS}/{repository_id}/imports", json={})

    assert removals == [0]  # importing 的快照沒有檔案列
    assert (response.status_code, _error_code(response)) == (409, "snapshot_import_in_progress")
    assert _totals(db_engine) == (0, 0, 0)  # 沒有殘留的快照或檔案列，也不是 500
    monkeypatch.undo()
    assert client.post(f"{REPOS}/{repository_id}/imports", json={}).status_code == 201


def test_a_failing_import_after_its_snapshot_was_removed_does_not_resurrect_anything(
    client: TestClient,
    repository_id: int,
    db_engine: Engine,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without_snapshot_lock(monkeypatch)

    def remove_then_fail(*args: Any, **kwargs: Any) -> Any:
        with db_engine.begin() as connection:
            connection.execute(text("UPDATE snapshots SET started_at = now() - interval '1 day'"))
        with Session(db_engine) as other:
            snapshot_id = other.execute(text("SELECT id FROM snapshots")).scalar_one()
            snapshot_removal.remove_snapshot(other, snapshot_id, settings)
        raise RuntimeError("the original import failed after losing its snapshot")

    monkeypatch.setattr(snapshot_service, "_build_records", remove_then_fail)

    response = client.post(f"{REPOS}/{repository_id}/imports", json={})

    assert response.status_code == 500  # 原本的失敗照常回報（既有行為），但沒有留下任何資料
    assert _totals(db_engine) == (0, 0, 0)


def test_concurrent_removals_of_the_same_snapshot_have_one_winner_and_no_500(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=300)
    workers = 6
    barrier = threading.Barrier(workers)

    def attempt(_: int) -> int:
        barrier.wait()
        return int(_delete(client, snapshot_id, force="true").status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(attempt, range(workers)))

    assert statuses.count(200) == 1
    assert set(statuses) <= {200, 404, 409}
    assert _totals(db_engine) == (0, 0, 0)


def test_removals_racing_with_imports_never_return_500_or_leave_partial_data(
    client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    for _round in range(4):
        existing = _import(client, repository_id)
        barrier = threading.Barrier(4)

        def remove(gate: threading.Barrier = barrier, target: int = existing["id"]) -> int:
            gate.wait()
            return int(_delete(client, target, force="true").status_code)

        def reimport(gate: threading.Barrier = barrier) -> int:
            gate.wait()
            return int(client.post(f"{REPOS}/{repository_id}/imports", json={}).status_code)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(remove), pool.submit(remove), pool.submit(reimport)]
            futures.append(pool.submit(reimport))
            statuses = [future.result(timeout=60) for future in futures]

        assert set(statuses) <= {200, 201, 404, 409}
        # 不論誰先：該 commit 最多一個快照；存在就必須是完整的 completed（檔案列數吻合）
        with db_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT s.status, s.file_count, "
                    "(SELECT count(*) FROM snapshot_files f WHERE f.snapshot_id = s.id) "
                    "FROM snapshots s"
                )
            ).all()
        assert len(rows) <= 1
        for status, file_count, files in rows:
            assert (status, file_count, files) == ("completed", 2, 2)
        with db_engine.begin() as connection:
            connection.execute(text("DELETE FROM snapshots"))  # 下一輪從乾淨狀態開始


# --- 大型快照 ---


def test_removing_a_max_size_snapshot_uses_a_few_sql_statements_and_finishes_quickly(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = add_snapshot(db_engine, repository_id, files=20_000)
    _set_reference(client, repository_id, snapshot_id)
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    engine: Engine = client.app.state.session_factory.kw["bind"]  # type: ignore[attr-defined]
    event.listen(engine, "before_cursor_execute", record)
    started = time.monotonic()
    try:
        response = _delete(client, snapshot_id, force="true")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["deleted_files"] == 20_000
    assert _totals(db_engine) == (0, 0, 0)
    # 與檔案數無關：查詢身分、鎖列、job／參考檢查、三個 DELETE，加上取鎖與解鎖（另一條連線）；
    # 切片 6 另有索引大小檢查（先檢查再分批清除，鎖列與檢查多做一次，再加符號數 SELECT：12 → 13）
    assert len(statements) <= 13
    # SQL 層級刪除：不載入 snapshot_files 的任何資料列
    assert not any(
        statement.lstrip().upper().startswith("SELECT") and "snapshot_files" in statement
        for statement in statements
    )
    assert elapsed < 20, f"移除 20,000 個檔案花了 {elapsed:.1f} 秒"
