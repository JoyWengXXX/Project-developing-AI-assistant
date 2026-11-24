"""參考版本 API（第二階段切片 3b，D41）：upsert、歸屬、非 completed、冪等、級聯、增量欄位。

全部使用真實 PostgreSQL（db-test）與真實暫存 Git repo；參考只是資料庫標記，不呼叫 Git。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from repopilot.config import Settings
from repopilot.main import create_app
from tests.git_helpers import commit_files, init_repo
from tests.integration.jobs.conftest import remove_tree
from tests.integration.snapshot_helpers import (
    TEST_PROFILE,
    add_snapshot,
    count,
    wait_until_a_query_is_blocked,
)

REPOS = "/api/v1/repositories"
REFERENCES = "/api/v1/references"
SNAPSHOTS = "/api/v1/snapshots"


@pytest.fixture
def repo(roots: Path) -> Path:
    return init_repo(roots / "demo", files={"README.md": "hello\n", "src/app.py": "x = 1\n"})


def _register(client: TestClient, path: Path) -> int:
    response = client.post(REPOS, json={"path": str(path)})
    assert response.status_code in (200, 201), response.text
    return int(response.json()["id"])


def _import(client: TestClient, repository_id: int, revision: str = "HEAD") -> dict[str, Any]:
    response = client.post(f"{REPOS}/{repository_id}/imports", json={"revision": revision})
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


def _put(client: TestClient, repository_id: int, snapshot_id: Any) -> Any:
    return client.put(f"{REPOS}/{repository_id}/reference", json={"snapshot_id": snapshot_id})


def _error_code(response: Any) -> str:
    code = response.json()["error"]["code"]
    assert isinstance(code, str)
    return code


def _reference_rows(engine: Engine) -> list[tuple[int, int]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT repository_id, snapshot_id FROM repository_references ORDER BY id")
        )
        return [(row[0], row[1]) for row in rows]


@pytest.fixture
def repository_id(client: TestClient, repo: Path) -> int:
    return _register(client, repo)


@pytest.fixture
def snapshot_id(client: TestClient, repository_id: int) -> int:
    return int(_import(client, repository_id)["id"])


# --- GET /references ---


def test_references_are_empty_by_default(client: TestClient) -> None:
    response = client.get(REFERENCES)

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_put_sets_the_reference_and_get_lists_it_with_the_documented_shape(
    client: TestClient, repository_id: int, snapshot_id: int, repo: Path
) -> None:
    response = _put(client, repository_id, snapshot_id)

    assert response.status_code == 200
    body = response.json()
    snapshot = client.get(f"{SNAPSHOTS}/{snapshot_id}").json()
    assert body == {
        "id": body["id"],
        "repository": {
            "id": repository_id,
            "name": "demo",
            "source_type": "local",
            "github_owner": None,
            "github_name": None,
        },
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "snapshot_status": "completed",
        "file_count": 2,
        "skipped_count": 0,
        "total_bytes": snapshot["total_bytes"],
        "created_at": body["created_at"],
        "embedding": body["embedding"],
    }
    # 快照沒有結構索引（或沒設定 embedding）：略過排入，參考版本仍成功（issue #25）
    assert body["embedding"]["status"] == "skipped"
    assert body["embedding"]["job"] is None
    assert body["created_at"].endswith("Z") or "+" in body["created_at"]
    listed = {key: value for key, value in body.items() if key != "embedding"}
    assert client.get(REFERENCES).json() == {"items": [listed]}


def test_listing_references_does_not_need_the_git_repository(
    client: TestClient, repository_id: int, snapshot_id: int, repo: Path
) -> None:
    assert _put(client, repository_id, snapshot_id).status_code == 200
    remove_tree(repo)  # Git repo 不見了：只讀資料庫的 GET 仍然成功

    response = client.get(REFERENCES)

    assert response.status_code == 200
    assert [item["snapshot_id"] for item in response.json()["items"]] == [snapshot_id]


def test_references_of_several_repositories_are_listed_by_repository_id(
    client: TestClient, roots: Path, repo: Path, repository_id: int, snapshot_id: int
) -> None:
    other = init_repo(roots / "other", files={"a.txt": "a\n"})
    other_id = _register(client, other)
    other_snapshot = _import(client, other_id)["id"]
    # 後設定的在前面也不影響排序：依 repository_id
    assert _put(client, other_id, other_snapshot).status_code == 200
    assert _put(client, repository_id, snapshot_id).status_code == 200

    items = client.get(REFERENCES).json()["items"]

    assert [item["repository"]["id"] for item in items] == [repository_id, other_id]
    assert [item["snapshot_id"] for item in items] == [snapshot_id, other_snapshot]


# --- PUT：upsert 與冪等 ---


def test_put_another_snapshot_replaces_the_reference_and_keeps_one_row_per_repository(
    client: TestClient, repo: Path, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    commit_files(repo, {"src/app.py": "x = 2\n"})
    second = int(_import(client, repository_id)["id"])
    first_body = _put(client, repository_id, snapshot_id).json()

    response = _put(client, repository_id, second)

    assert response.status_code == 200
    body = response.json()
    assert body["snapshot_id"] == second
    assert body["id"] == first_body["id"]  # 同一列被更新，不是新增
    assert body["created_at"] >= first_body["created_at"]
    assert _reference_rows(db_engine) == [(repository_id, second)]
    assert [item["snapshot_id"] for item in client.get(REFERENCES).json()["items"]] == [second]


def test_putting_the_same_snapshot_twice_is_idempotent(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    first = _put(client, repository_id, snapshot_id)
    second = _put(client, repository_id, snapshot_id)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()  # 連 id 與 created_at 都不變
    assert _reference_rows(db_engine) == [(repository_id, snapshot_id)]


def test_concurrent_puts_of_different_snapshots_leave_exactly_one_reference(
    client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    snapshot_ids = [add_snapshot(db_engine, repository_id, index=number) for number in range(1, 7)]
    barrier = threading.Barrier(len(snapshot_ids))

    def attempt(target: int) -> int:
        barrier.wait()
        return int(_put(client, repository_id, target).status_code)

    with ThreadPoolExecutor(max_workers=len(snapshot_ids)) as pool:
        statuses = list(pool.map(attempt, snapshot_ids))

    assert statuses == [200] * len(snapshot_ids)
    rows = _reference_rows(db_engine)
    assert len(rows) == 1
    assert rows[0][0] == repository_id and rows[0][1] in snapshot_ids


# --- PUT：歸屬與狀態檢查 ---


def test_put_rejects_a_snapshot_of_another_repository_with_404_and_stores_nothing(
    client: TestClient, roots: Path, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    other_id = _register(client, init_repo(roots / "other", files={"a.txt": "a\n"}))

    response = _put(client, other_id, snapshot_id)  # snapshot_id 屬於 repository_id，不是 other_id

    assert response.status_code == 404
    assert _error_code(response) == "snapshot_not_found"
    assert _reference_rows(db_engine) == []


@pytest.mark.parametrize("status", ["completed", "failed", "importing"])
def test_another_repositorys_snapshot_is_404_whatever_its_status_so_nothing_leaks(
    client: TestClient, roots: Path, repository_id: int, db_engine: Engine, status: str
) -> None:
    """D30：歸屬檢查先於狀態檢查——別的 repo 的 failed／importing 快照不能因 409 而洩漏存在與否。"""
    other_id = _register(client, init_repo(roots / "other", files={"a.txt": "a\n"}))
    foreign = add_snapshot(db_engine, other_id, status=status)

    response = _put(client, repository_id, foreign)

    assert (response.status_code, _error_code(response)) == (404, "snapshot_not_found")
    assert _reference_rows(db_engine) == []


def test_a_failed_put_keeps_the_existing_reference(
    client: TestClient, roots: Path, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    other_id = _register(client, init_repo(roots / "other", files={"a.txt": "a\n"}))
    other_snapshot = int(_import(client, other_id)["id"])
    assert _put(client, repository_id, snapshot_id).status_code == 200

    assert _put(client, repository_id, other_snapshot).status_code == 404

    assert _reference_rows(db_engine) == [(repository_id, snapshot_id)]


def test_put_rejects_unknown_snapshot_and_unknown_repository(
    client: TestClient, repository_id: int, snapshot_id: int
) -> None:
    missing_snapshot = _put(client, repository_id, snapshot_id + 1000)
    missing_repository = _put(client, repository_id + 1000, snapshot_id)

    assert (missing_snapshot.status_code, _error_code(missing_snapshot)) == (
        404,
        "snapshot_not_found",
    )
    assert (missing_repository.status_code, _error_code(missing_repository)) == (
        404,
        "repository_not_found",
    )


@pytest.mark.parametrize("status", ["importing", "failed"])
def test_put_rejects_a_snapshot_that_is_not_completed_with_409(
    client: TestClient, repository_id: int, db_engine: Engine, status: str
) -> None:
    unfinished = add_snapshot(db_engine, repository_id, status=status)

    response = _put(client, repository_id, unfinished)

    assert response.status_code == 409
    assert _error_code(response) == "snapshot_not_completed"
    assert _reference_rows(db_engine) == []


@pytest.mark.parametrize("bad_id", [0, -1, 2**31, 2**63, "abc", None, 1.5])
def test_put_rejects_invalid_snapshot_ids_with_422(
    client: TestClient, repository_id: int, bad_id: Any
) -> None:
    response = _put(client, repository_id, bad_id)

    assert response.status_code == 422
    assert _error_code(response) == "validation_error"


def test_put_rejects_a_missing_body_and_unknown_fields_with_422(
    client: TestClient, repository_id: int, snapshot_id: int
) -> None:
    assert client.put(f"{REPOS}/{repository_id}/reference").status_code == 422
    assert client.put(f"{REPOS}/{repository_id}/reference", json={}).status_code == 422
    extra = client.put(
        f"{REPOS}/{repository_id}/reference", json={"snapshot_id": snapshot_id, "force": True}
    )
    assert extra.status_code == 422


@pytest.mark.parametrize("bad_id", ["0", "-1", str(2**31), str(2**63), "abc"])
def test_reference_endpoints_reject_out_of_range_repository_ids_with_422(
    client: TestClient, bad_id: str
) -> None:
    put = client.put(f"{REPOS}/{bad_id}/reference", json={"snapshot_id": 1})
    delete = client.delete(f"{REPOS}/{bad_id}/reference")

    assert (put.status_code, delete.status_code) == (422, 422)
    assert _error_code(put) == _error_code(delete) == "validation_error"


# --- DELETE ---


def test_delete_reference_is_204_and_idempotent(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    assert _put(client, repository_id, snapshot_id).status_code == 200

    first = client.delete(f"{REPOS}/{repository_id}/reference")
    second = client.delete(f"{REPOS}/{repository_id}/reference")

    assert (first.status_code, second.status_code) == (204, 204)
    assert first.content == b""
    assert client.get(REFERENCES).json() == {"items": []}
    assert _reference_rows(db_engine) == []
    # 取消參考不會動快照
    assert client.get(f"{SNAPSHOTS}/{snapshot_id}").status_code == 200


def test_delete_reference_only_affects_that_repository(
    client: TestClient, roots: Path, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    other_id = _register(client, init_repo(roots / "other", files={"a.txt": "a\n"}))
    other_snapshot = int(_import(client, other_id)["id"])
    _put(client, repository_id, snapshot_id)
    _put(client, other_id, other_snapshot)

    assert client.delete(f"{REPOS}/{repository_id}/reference").status_code == 204

    assert _reference_rows(db_engine) == [(other_id, other_snapshot)]


def test_delete_reference_of_an_unknown_repository_is_404(client: TestClient) -> None:
    response = client.delete(f"{REPOS}/12345/reference")

    assert response.status_code == 404
    assert _error_code(response) == "repository_not_found"


# --- 資料庫約束（最後防線）---


def test_database_rejects_a_reference_to_a_snapshot_of_another_repository(
    client: TestClient, roots: Path, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    other_id = _register(client, init_repo(roots / "other", files={"a.txt": "a\n"}))

    with (
        pytest.raises(IntegrityError, match="fk_repository_references_snapshot_id_snapshots"),
        db_engine.begin() as connection,
    ):
        connection.execute(
            text("INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"),
            {"r": other_id, "s": snapshot_id},
        )


def test_database_allows_only_one_reference_per_repository(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    second = add_snapshot(db_engine, repository_id, index=2)
    insert = "INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"
    with db_engine.begin() as connection:
        connection.execute(text(insert), {"r": repository_id, "s": snapshot_id})

    with (
        pytest.raises(IntegrityError, match="uq_repository_references_repository_id"),
        db_engine.begin() as connection,
    ):
        connection.execute(text(insert), {"r": repository_id, "s": second})


def test_database_does_not_require_completed_status_the_service_layer_does(
    client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    """記錄現況：`completed` 由 service 層檢查（completed 是終態，D30），資料庫只管歸屬。"""
    failed = add_snapshot(db_engine, repository_id, status="failed")

    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO repository_references (repository_id, snapshot_id) VALUES (:r, :s)"),
            {"r": repository_id, "s": failed},
        )

    assert count(db_engine, "repository_references") == 1


# --- 級聯 ---


def test_deleting_a_snapshot_row_cascades_to_its_reference(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    _put(client, repository_id, snapshot_id)

    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM snapshots WHERE id = :i"), {"i": snapshot_id})

    assert _reference_rows(db_engine) == []


def test_removing_a_repository_removes_its_references_and_leaves_no_orphans(
    client: TestClient,
    roots: Path,
    repo: Path,
    repository_id: int,
    snapshot_id: int,
    db_engine: Engine,
) -> None:
    """D33 的刪除順序不變（snapshot_files → snapshots → repositories）；參考隨快照級聯消失。"""
    other_id = _register(client, init_repo(roots / "other", files={"a.txt": "a\n"}))
    other_snapshot = int(_import(client, other_id)["id"])
    _put(client, repository_id, snapshot_id)
    _put(client, other_id, other_snapshot)

    response = client.delete(f"{REPOS}/{repository_id}")

    assert response.status_code == 200
    assert _reference_rows(db_engine) == [(other_id, other_snapshot)]  # 別的 repo 的參考不受影響
    items = client.get(REFERENCES).json()["items"]
    assert [item["repository"]["id"] for item in items] == [other_id]
    assert count(db_engine, "snapshots", "repository_id = :r", r=repository_id) == 0
    assert count(db_engine, "snapshot_files", "snapshot_id = :s", s=snapshot_id) == 0


def test_removing_the_only_repository_leaves_the_reference_table_empty(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    _put(client, repository_id, snapshot_id)

    assert client.delete(f"{REPOS}/{repository_id}").status_code == 200

    assert count(db_engine, "repository_references") == 0
    assert client.get(REFERENCES).json() == {"items": []}


# --- 與「移除快照」競爭 ---


def test_put_waits_for_a_snapshot_removal_in_progress_and_returns_404_after_it_commits(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    """以第二條連線扮演「移除到一半」（持有快照列的 FOR UPDATE）：PUT 必須等，之後得到 404。"""
    lock_connection = db_engine.connect()
    transaction = lock_connection.begin()
    lock_connection.execute(
        text("SELECT id FROM snapshots WHERE id = :i FOR UPDATE"), {"i": snapshot_id}
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_put, client, repository_id, snapshot_id)
            wait_until_a_query_is_blocked(db_engine)  # PUT 的外鍵檢查在等移除交易
            lock_connection.execute(text("DELETE FROM snapshots WHERE id = :i"), {"i": snapshot_id})
            transaction.commit()
            response = future.result(timeout=30)
    finally:
        lock_connection.close()

    assert response.status_code == 404
    assert _error_code(response) == "snapshot_not_found"
    assert _reference_rows(db_engine) == []


def test_put_proceeds_when_the_removal_in_progress_rolls_back(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    lock_connection = db_engine.connect()
    transaction = lock_connection.begin()
    lock_connection.execute(
        text("SELECT id FROM snapshots WHERE id = :i FOR UPDATE"), {"i": snapshot_id}
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_put, client, repository_id, snapshot_id)
            wait_until_a_query_is_blocked(db_engine)
            transaction.rollback()
            response = future.result(timeout=30)
    finally:
        lock_connection.close()

    assert response.status_code == 200
    assert _reference_rows(db_engine) == [(repository_id, snapshot_id)]


# --- 快照列表與詳情的增量欄位 ---

_OLD_SNAPSHOT_KEYS = {
    "id",
    "repository_id",
    "commit_sha",
    "import_profile_hash",
    "status",
    "file_count",
    "skipped_count",
    "total_bytes",
    "started_at",
    "completed_at",
    "error_summary",
}
_NEW_SNAPSHOT_KEYS = {"is_referenced", "is_stale_profile"}


def test_snapshot_list_and_detail_only_add_the_two_new_fields(
    client: TestClient, repository_id: int, snapshot_id: int
) -> None:
    item = client.get(f"{REPOS}/{repository_id}/snapshots").json()["items"][0]
    detail = client.get(f"{SNAPSHOTS}/{snapshot_id}").json()

    assert set(item) == _OLD_SNAPSHOT_KEYS | _NEW_SNAPSHOT_KEYS
    assert set(detail) == _OLD_SNAPSHOT_KEYS | _NEW_SNAPSHOT_KEYS | {
        "file_status_counts",
        "skip_reason_counts",
    }
    created = client.post(f"{REPOS}/{repository_id}/imports", json={})
    assert set(created.json()) == _OLD_SNAPSHOT_KEYS  # POST /imports 的回應契約完全不變


def test_is_referenced_follows_the_reference_in_list_and_detail(
    client: TestClient, repo: Path, repository_id: int, snapshot_id: int
) -> None:
    commit_files(repo, {"src/app.py": "x = 2\n"})
    second = int(_import(client, repository_id)["id"])

    def flags() -> dict[int, bool]:
        items = client.get(f"{REPOS}/{repository_id}/snapshots").json()["items"]
        listed = {item["id"]: item["is_referenced"] for item in items}
        for snapshot, referenced in listed.items():  # 詳情與列表一致
            assert client.get(f"{SNAPSHOTS}/{snapshot}").json()["is_referenced"] is referenced
        return listed

    assert flags() == {snapshot_id: False, second: False}
    _put(client, repository_id, snapshot_id)
    assert flags() == {snapshot_id: True, second: False}
    _put(client, repository_id, second)
    assert flags() == {snapshot_id: False, second: True}
    client.delete(f"{REPOS}/{repository_id}/reference")
    assert flags() == {snapshot_id: False, second: False}


def test_is_referenced_is_per_repository(
    client: TestClient, roots: Path, repository_id: int, snapshot_id: int
) -> None:
    other_id = _register(client, init_repo(roots / "other", files={"a.txt": "a\n"}))
    other_snapshot = int(_import(client, other_id)["id"])
    _put(client, other_id, other_snapshot)

    assert client.get(f"{SNAPSHOTS}/{snapshot_id}").json()["is_referenced"] is False
    assert client.get(f"{SNAPSHOTS}/{other_snapshot}").json()["is_referenced"] is True


def test_is_stale_profile_compares_with_the_current_import_settings(
    client: TestClient, repository_id: int, snapshot_id: int, db_engine: Engine
) -> None:
    old = add_snapshot(db_engine, repository_id, index=9, profile_hash=TEST_PROFILE)

    items = client.get(f"{REPOS}/{repository_id}/snapshots").json()["items"]
    stale = {item["id"]: item["is_stale_profile"] for item in items}

    assert stale == {snapshot_id: False, old: True}  # 目前設定匯入的不過期；舊雜湊的過期
    assert client.get(f"{SNAPSHOTS}/{snapshot_id}").json()["is_stale_profile"] is False
    assert client.get(f"{SNAPSHOTS}/{old}").json()["is_stale_profile"] is True


def test_changing_the_import_settings_makes_existing_snapshots_stale(
    client: TestClient, repository_id: int, snapshot_id: int, settings: Settings
) -> None:
    changed = settings.model_copy(
        update={"import_max_file_bytes": settings.import_max_file_bytes - 1}
    )
    with TestClient(create_app(changed)) as other_client:
        assert other_client.get(f"{SNAPSHOTS}/{snapshot_id}").json()["is_stale_profile"] is True
    assert client.get(f"{SNAPSHOTS}/{snapshot_id}").json()["is_stale_profile"] is False
