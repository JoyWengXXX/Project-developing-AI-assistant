"""切片 5：快照與檔案查詢 API 的 PostgreSQL 整合測試（真實暫存 Git repo）。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.main import create_app
from repopilot.services import snapshot_queries
from tests.git_helpers import commit_files, git, init_repo

REPOS = "/api/v1/repositories"
SNAPSHOTS = "/api/v1/snapshots"


def _register(client: TestClient, repo: Path) -> int:
    response = client.post(REPOS, json={"path": str(repo)})
    assert response.status_code in (200, 201)
    repository_id: int = response.json()["id"]
    return repository_id


def _import(client: TestClient, repository_id: int, revision: str = "HEAD") -> dict[str, Any]:
    response = client.post(f"{REPOS}/{repository_id}/imports", json={"revision": revision})
    assert response.status_code in (200, 201)
    body: dict[str, Any] = response.json()
    return body


def _file_ids(client: TestClient, snapshot_id: int) -> dict[str, int]:
    """以 API 取得 path → file_id（單頁最大 200 筆，測試資料量遠小於此）。"""
    response = client.get(f"{SNAPSHOTS}/{snapshot_id}/files", params={"limit": 200})
    assert response.status_code == 200
    return {item["path"]: item["id"] for item in response.json()["items"]}


def _insert_snapshot(db_engine: Engine, repository_id: int, commit_sha: str, *, status: str) -> int:
    """直接寫入指定狀態的快照（模擬匯入中或失敗）；用不同的 profile hash 避免唯一約束衝突。"""
    with Session(db_engine) as session:
        snapshot = Snapshot(
            repository_id=repository_id,
            commit_sha=commit_sha,
            import_profile_hash=f"{status}-{commit_sha}"[:64],
            status=status,
            started_at=datetime.now(UTC),
        )
        session.add(snapshot)
        session.commit()
        return snapshot.id


@pytest.fixture
def repo(roots: Path) -> Path:
    return init_repo(
        roots / "demo",
        files={"README.md": "hello\n", "src/app.py": "x = 1\n", "src/util.py": "y = 2\n"},
    )


@pytest.fixture
def repository_id(client: TestClient, repo: Path) -> int:
    return _register(client, repo)


@pytest.fixture
def snapshot(client: TestClient, repository_id: int) -> dict[str, Any]:
    return _import(client, repository_id)


def _assert_error(response: Any, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    error = response.json()["error"]
    assert error["code"] == code
    assert error["request_id"]
    assert response.headers["X-Request-ID"] == error["request_id"]


# --- GET /repositories/{id}/snapshots ---


def test_list_snapshots_of_missing_repository_returns_404(client: TestClient) -> None:
    _assert_error(client.get(f"{REPOS}/999/snapshots"), 404, "repository_not_found")


def test_list_snapshots_empty_repository(client: TestClient, repository_id: int) -> None:
    response = client.get(f"{REPOS}/{repository_id}/snapshots")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_list_snapshots_newest_first_without_file_content(
    client: TestClient, repo: Path, repository_id: int
) -> None:
    first = _import(client, repository_id)
    commit_files(repo, {"src/app.py": "x = 2\n"})
    second = _import(client, repository_id)

    body = client.get(f"{REPOS}/{repository_id}/snapshots").json()

    assert [item["id"] for item in body["items"]] == [second["id"], first["id"]]
    assert body["total"] == 2
    item = body["items"][0]
    # 切片 3b 起列表項目多了 is_referenced／is_stale_profile 兩個增量欄位；既有欄位逐一相同
    assert item == {**second, "is_referenced": False, "is_stale_profile": False}
    assert not {"content", "files", "file_status_counts"} & set(item)


def test_list_snapshots_only_returns_that_repository(
    client: TestClient, roots: Path, repo: Path, repository_id: int
) -> None:
    other = init_repo(roots / "other", files={"a.txt": "a"})
    other_id = _register(client, other)
    mine = _import(client, repository_id)
    _import(client, other_id)

    body = client.get(f"{REPOS}/{repository_id}/snapshots").json()

    assert [item["id"] for item in body["items"]] == [mine["id"]]
    assert body["total"] == 1


def test_list_snapshots_includes_all_statuses_and_filters_by_status(
    client: TestClient, repository_id: int, repo: Path, db_engine: Engine
) -> None:
    completed = _import(client, repository_id)
    sha = git(repo, "rev-parse", "HEAD")
    failed_id = _insert_snapshot(db_engine, repository_id, sha, status="failed")
    importing_id = _insert_snapshot(db_engine, repository_id, sha, status="importing")

    everything = client.get(f"{REPOS}/{repository_id}/snapshots").json()
    assert {item["id"]: item["status"] for item in everything["items"]} == {
        completed["id"]: "completed",
        failed_id: "failed",
        importing_id: "importing",
    }
    for status, expected in (
        ("completed", completed["id"]),
        ("failed", failed_id),
        ("importing", importing_id),
    ):
        body = client.get(f"{REPOS}/{repository_id}/snapshots", params={"status": status}).json()
        assert [item["id"] for item in body["items"]] == [expected]
        assert body["total"] == 1


def test_list_snapshots_pagination(
    client: TestClient, repository_id: int, repo: Path, db_engine: Engine
) -> None:
    ids = [
        _insert_snapshot(db_engine, repository_id, f"{n:040x}", status="failed") for n in range(5)
    ]
    newest_first = list(reversed(ids))

    page1 = client.get(f"{REPOS}/{repository_id}/snapshots", params={"limit": 2}).json()
    page2 = client.get(
        f"{REPOS}/{repository_id}/snapshots", params={"limit": 2, "offset": 2}
    ).json()
    page3 = client.get(
        f"{REPOS}/{repository_id}/snapshots", params={"limit": 2, "offset": 4}
    ).json()
    beyond = client.get(f"{REPOS}/{repository_id}/snapshots", params={"offset": 99}).json()

    assert [i["id"] for i in page1["items"]] == newest_first[0:2]
    assert [i["id"] for i in page2["items"]] == newest_first[2:4]
    assert [i["id"] for i in page3["items"]] == newest_first[4:]
    assert page1["total"] == page2["total"] == page3["total"] == 5
    assert (page2["limit"], page2["offset"]) == (2, 2)
    assert beyond["items"] == []
    assert beyond["total"] == 5


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"limit": -1},
        {"limit": "abc"},
        {"offset": -1},
        {"offset": 2**31},
        {"status": "done"},
    ],
)
def test_list_snapshots_rejects_invalid_query(
    client: TestClient, repository_id: int, params: dict[str, Any]
) -> None:
    _assert_error(
        client.get(f"{REPOS}/{repository_id}/snapshots", params=params), 422, "validation_error"
    )


def test_list_snapshots_accepts_maximum_limit(client: TestClient, repository_id: int) -> None:
    response = client.get(f"{REPOS}/{repository_id}/snapshots", params={"limit": 200})

    assert response.status_code == 200
    assert response.json()["limit"] == 200


@pytest.mark.parametrize("bad_id", ["0", "-1", str(2**31), "abc"])
def test_snapshot_routes_reject_out_of_range_ids(client: TestClient, bad_id: str) -> None:
    for url in (
        f"{REPOS}/{bad_id}/snapshots",
        f"{SNAPSHOTS}/{bad_id}",
        f"{SNAPSHOTS}/{bad_id}/files",
        f"{SNAPSHOTS}/{bad_id}/files/1",
    ):
        _assert_error(client.get(url), 422, "validation_error")


# --- GET /snapshots/{id} ---


def test_get_missing_snapshot_returns_404(client: TestClient) -> None:
    _assert_error(client.get(f"{SNAPSHOTS}/999"), 404, "snapshot_not_found")


def test_get_snapshot_detail_has_stats_from_group_by(
    client: TestClient, roots: Path, repository_id: int, settings: Settings
) -> None:
    repo = init_repo(roots / "mixed", files={"seed.txt": "x", ".env": "TOKEN=FAKE_FOR_TEST\n"})
    commit_files(repo, {"bin.dat": b"\x00\x01", "bad.txt": b"caf\xe9\n", "ok.py": "ok = 1\n"})
    mixed_id = _register(client, repo)
    created = _import(client, mixed_id)

    body = client.get(f"{SNAPSHOTS}/{created['id']}").json()

    assert body["id"] == created["id"]
    assert body["repository_id"] == mixed_id
    assert body["commit_sha"] == git(repo, "rev-parse", "HEAD")
    assert body["import_profile_hash"] == created["import_profile_hash"]
    assert body["status"] == "completed"
    assert (body["file_count"], body["skipped_count"]) == (2, 3)
    assert body["total_bytes"] == len("x") + len("ok = 1\n") + 2 + len(b"caf\xe9\n")
    assert body["started_at"]
    assert body["completed_at"] is not None
    assert body["error_summary"] is None
    assert body["file_status_counts"] == {"imported": 2, "skipped": 3}
    assert body["skip_reason_counts"] == {"binary": 1, "invalid_utf8": 1, "excluded": 1}
    assert "FAKE_FOR_TEST" not in str(body)


@pytest.mark.parametrize("status", ["importing", "failed"])
def test_get_unfinished_snapshot_shows_status_but_no_file_stats(
    client: TestClient, repository_id: int, repo: Path, db_engine: Engine, status: str
) -> None:
    snapshot_id = _insert_snapshot(
        db_engine, repository_id, git(repo, "rev-parse", "HEAD"), status=status
    )
    # 即使資料庫殘留檔案列（不應發生），也不得被統計為有效資料
    with Session(db_engine) as session:
        session.add(
            SnapshotFile(snapshot_id=snapshot_id, path="stray.txt", status="imported", content="x")
        )
        session.commit()

    response = client.get(f"{SNAPSHOTS}/{snapshot_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["file_status_counts"] == {}
    assert body["skip_reason_counts"] == {}


def test_failed_import_detail_reports_error_summary(
    client: TestClient, settings: Settings, repository_id: int, db_engine: Engine
) -> None:
    limited = settings.model_copy(update={"import_max_files": 1})
    with TestClient(create_app(limited)) as limited_client:
        response = limited_client.post(f"{REPOS}/{repository_id}/imports", json={})
    assert response.status_code == 422
    failed = client.get(f"{REPOS}/{repository_id}/snapshots", params={"status": "failed"}).json()
    snapshot_id = failed["items"][0]["id"]

    body = client.get(f"{SNAPSHOTS}/{snapshot_id}").json()

    assert body["status"] == "failed"
    assert body["error_summary"]
    assert body["file_status_counts"] == {}


# --- GET /snapshots/{id}/files ---


def test_list_files_missing_snapshot_returns_404(client: TestClient) -> None:
    _assert_error(client.get(f"{SNAPSHOTS}/999/files"), 404, "snapshot_not_found")


def test_list_files_omits_content_and_sorts_by_path(
    client: TestClient, snapshot: dict[str, Any]
) -> None:
    response = client.get(f"{SNAPSHOTS}/{snapshot['id']}/files")

    assert response.status_code == 200
    body = response.json()
    assert [item["path"] for item in body["items"]] == ["README.md", "src/app.py", "src/util.py"]
    assert (body["total"], body["limit"], body["offset"]) == (3, 50, 0)
    for item in body["items"]:
        assert "content" not in item
        assert set(item) == {
            "id",
            "snapshot_id",
            "path",
            "git_blob_id",
            "sha256",
            "byte_size",
            "status",
            "skip_reason",
        }
        assert item["snapshot_id"] == snapshot["id"]
        assert item["status"] == "imported"
    assert "x = 1" not in response.text


def test_list_files_sorts_by_byte_order_not_locale(client: TestClient, roots: Path) -> None:
    repo = init_repo(
        roots / "order", files={"b.txt": "1", "A.txt": "2", "_c.txt": "3", "Z.txt": "4"}
    )
    created = _import(client, _register(client, repo))

    paths = [i["path"] for i in client.get(f"{SNAPSHOTS}/{created['id']}/files").json()["items"]]

    assert paths == ["A.txt", "Z.txt", "_c.txt", "b.txt"]


def test_list_files_pagination_is_stable_and_complete(client: TestClient, roots: Path) -> None:
    names = {f"dir/file_{n:02d}.txt": str(n) for n in range(7)}
    repo = init_repo(roots / "paged", files=names)
    created = _import(client, _register(client, repo))
    url = f"{SNAPSHOTS}/{created['id']}/files"

    pages = [client.get(url, params={"limit": 3, "offset": offset}).json() for offset in (0, 3, 6)]
    beyond = client.get(url, params={"limit": 3, "offset": 7}).json()
    one = client.get(url, params={"limit": 1, "offset": 6}).json()

    assert [len(p["items"]) for p in pages] == [3, 3, 1]
    assert all(p["total"] == 7 for p in pages)
    collected = [item["path"] for page in pages for item in page["items"]]
    assert collected == sorted(names)
    assert len(set(collected)) == 7
    assert beyond["items"] == []
    assert beyond["total"] == 7
    assert [i["path"] for i in one["items"]] == ["dir/file_06.txt"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"status": "completed"},
        {"path_prefix": "a" * 1025},
    ],
)
def test_list_files_rejects_invalid_query(
    client: TestClient, snapshot: dict[str, Any], params: dict[str, Any]
) -> None:
    _assert_error(
        client.get(f"{SNAPSHOTS}/{snapshot['id']}/files", params=params), 422, "validation_error"
    )


def test_list_files_accepts_maximum_limit(client: TestClient, snapshot: dict[str, Any]) -> None:
    response = client.get(f"{SNAPSHOTS}/{snapshot['id']}/files", params={"limit": 200})

    assert response.status_code == 200
    assert len(response.json()["items"]) == 3


def test_list_files_filters_by_status(client: TestClient, roots: Path) -> None:
    repo = init_repo(roots / "mixed", files={"a.txt": "a", ".env": "TOKEN=FAKE_FOR_TEST\n"})
    commit_files(repo, {"b.bin": b"\x00\x00", "c.txt": "c"})
    created = _import(client, _register(client, repo))
    url = f"{SNAPSHOTS}/{created['id']}/files"

    imported = client.get(url, params={"status": "imported"}).json()
    skipped = client.get(url, params={"status": "skipped", "limit": 1}).json()

    assert [i["path"] for i in imported["items"]] == ["a.txt", "c.txt"]
    assert imported["total"] == 2
    assert [i["path"] for i in skipped["items"]] == [".env"]  # 分頁只回第一筆，total 仍為 2
    assert skipped["total"] == 2
    assert {i["skip_reason"] for i in skipped["items"]} == {"excluded"}


def test_list_files_filters_by_path_prefix_literally(client: TestClient, roots: Path) -> None:
    names = {
        "src/a.py": "1",
        "src/sub/b.py": "2",
        "srcx/c.py": "3",
        "100%/d.txt": "4",
        "1000/e.txt": "5",
        "a_b/f.txt": "6",
        "aXb/g.txt": "7",
    }
    repo = init_repo(roots / "prefix", files=names)
    created = _import(client, _register(client, repo))
    url = f"{SNAPSHOTS}/{created['id']}/files"

    def paths(prefix: str) -> list[str]:
        body = client.get(url, params={"path_prefix": prefix}).json()
        return [i["path"] for i in body["items"]]

    assert paths("src/") == ["src/a.py", "src/sub/b.py"]
    assert paths("src") == ["src/a.py", "src/sub/b.py", "srcx/c.py"]
    assert paths("100%") == ["100%/d.txt"]  # % 不是萬用字元
    assert paths("a_b") == ["a_b/f.txt"]  # _ 不是萬用字元
    assert paths("SRC/") == []  # 區分大小寫
    assert paths("nothing") == []
    assert len(paths("")) == len(names)  # 空字串等同不篩選


def test_list_files_combines_filters_with_pagination(client: TestClient, roots: Path) -> None:
    names = {f"src/f{n}.py": "x" for n in range(5)}
    repo = init_repo(roots / "combo", files=names)
    commit_files(repo, {"src/skip.bin": b"\x00", "docs/readme.md": "doc"})
    created = _import(client, _register(client, repo))

    body = client.get(
        f"{SNAPSHOTS}/{created['id']}/files",
        params={"path_prefix": "src/", "status": "imported", "limit": 2, "offset": 2},
    ).json()

    assert [i["path"] for i in body["items"]] == ["src/f2.py", "src/f3.py"]
    assert body["total"] == 5


@pytest.mark.parametrize("status", ["importing", "failed"])
def test_list_files_of_unfinished_snapshot_returns_409(
    client: TestClient, repository_id: int, repo: Path, db_engine: Engine, status: str
) -> None:
    snapshot_id = _insert_snapshot(
        db_engine, repository_id, git(repo, "rev-parse", "HEAD"), status=status
    )
    # 殘留列（不應發生）也不可被回傳
    with Session(db_engine) as session:
        session.add(
            SnapshotFile(
                snapshot_id=snapshot_id, path="stray.txt", status="imported", content="LEAK"
            )
        )
        session.commit()

    response = client.get(f"{SNAPSHOTS}/{snapshot_id}/files")

    _assert_error(response, 409, "snapshot_not_completed")
    assert "LEAK" not in response.text


def test_files_of_really_failed_import_return_409(
    client: TestClient, settings: Settings, repository_id: int
) -> None:
    limited = settings.model_copy(update={"import_max_snapshot_bytes": 8})
    with TestClient(create_app(limited)) as limited_client:
        assert limited_client.post(f"{REPOS}/{repository_id}/imports", json={}).status_code == 422
    snapshot_id = client.get(f"{REPOS}/{repository_id}/snapshots").json()["items"][0]["id"]

    _assert_error(client.get(f"{SNAPSHOTS}/{snapshot_id}/files"), 409, "snapshot_not_completed")
    _assert_error(client.get(f"{SNAPSHOTS}/{snapshot_id}/files/1"), 409, "snapshot_not_completed")


# --- GET /snapshots/{id}/files/{file_id} ---


def test_get_file_returns_content_and_metadata(
    client: TestClient, snapshot: dict[str, Any]
) -> None:
    file_id = _file_ids(client, snapshot["id"])["src/app.py"]

    response = client.get(f"{SNAPSHOTS}/{snapshot['id']}/files/{file_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == file_id
    assert body["snapshot_id"] == snapshot["id"]
    assert body["path"] == "src/app.py"
    assert body["content"] == "x = 1\n"
    assert body["status"] == "imported"
    assert body["skip_reason"] is None
    assert body["byte_size"] == len("x = 1\n")
    assert len(body["sha256"]) == 64


def test_get_file_preserves_crlf_bom_and_empty_content(client: TestClient, roots: Path) -> None:
    repo = init_repo(roots / "text", files={"seed.txt": "x"})
    commit_files(
        repo, {"crlf.txt": b"a\r\nb\r\n", "bom.txt": b"\xef\xbb\xbfbom\n", "empty.txt": b""}
    )
    created = _import(client, _register(client, repo))
    ids = _file_ids(client, created["id"])

    def content(path: str) -> Any:
        return client.get(f"{SNAPSHOTS}/{created['id']}/files/{ids[path]}").json()["content"]

    assert content("crlf.txt") == "a\r\nb\r\n"
    assert content("bom.txt") == "\ufeffbom\n"
    assert content("empty.txt") == ""


def test_get_skipped_files_report_status_without_content(client: TestClient, roots: Path) -> None:
    repo = init_repo(
        roots / "skipped", files={".env": "TOKEN=FAKE_SECRET_FOR_TEST\n", "ok.txt": "ok"}
    )
    commit_files(repo, {"bin.dat": b"\x89PNG\x00\x00", "bad.txt": b"caf\xe9\n"})
    created = _import(client, _register(client, repo))
    ids = _file_ids(client, created["id"])
    expected = {".env": "excluded", "bin.dat": "binary", "bad.txt": "invalid_utf8"}

    for path, reason in expected.items():
        response = client.get(f"{SNAPSHOTS}/{created['id']}/files/{ids[path]}")

        assert response.status_code == 200
        body = response.json()
        assert (body["status"], body["skip_reason"]) == ("skipped", reason)
        assert body["content"] is None
        assert "FAKE_SECRET_FOR_TEST" not in response.text
    # 排除檔不保存 SHA-256；二進位仍有
    env = client.get(f"{SNAPSHOTS}/{created['id']}/files/{ids['.env']}").json()
    assert env["sha256"] is None
    binary = client.get(f"{SNAPSHOTS}/{created['id']}/files/{ids['bin.dat']}").json()
    assert binary["sha256"] is not None


def test_get_file_from_another_snapshot_returns_404(
    client: TestClient, repo: Path, repository_id: int
) -> None:
    first = _import(client, repository_id)
    commit_files(repo, {"src/app.py": "x = 2\n"})
    second = _import(client, repository_id)
    other_file_id = _file_ids(client, second["id"])["src/app.py"]

    response = client.get(f"{SNAPSHOTS}/{first['id']}/files/{other_file_id}")

    _assert_error(response, 404, "snapshot_file_not_found")
    assert "x = 2" not in response.text


def test_get_missing_file_returns_404(client: TestClient, snapshot: dict[str, Any]) -> None:
    _assert_error(
        client.get(f"{SNAPSHOTS}/{snapshot['id']}/files/999999"), 404, "snapshot_file_not_found"
    )


def test_get_file_of_missing_snapshot_returns_404(client: TestClient) -> None:
    _assert_error(client.get(f"{SNAPSHOTS}/999/files/1"), 404, "snapshot_not_found")


@pytest.mark.parametrize("status", ["importing", "failed"])
def test_get_file_of_unfinished_snapshot_returns_409_even_for_its_own_rows(
    client: TestClient, repository_id: int, repo: Path, db_engine: Engine, status: str
) -> None:
    snapshot_id = _insert_snapshot(
        db_engine, repository_id, git(repo, "rev-parse", "HEAD"), status=status
    )
    with Session(db_engine) as session:
        row = SnapshotFile(
            snapshot_id=snapshot_id, path="stray.txt", status="imported", content="LEAK"
        )
        session.add(row)
        session.commit()
        file_id = row.id

    response = client.get(f"{SNAPSHOTS}/{snapshot_id}/files/{file_id}")

    _assert_error(response, 409, "snapshot_not_completed")
    assert "LEAK" not in response.text


def test_completed_snapshot_cannot_read_file_of_unfinished_snapshot(
    client: TestClient, repository_id: int, repo: Path, db_engine: Engine
) -> None:
    completed = _import(client, repository_id)
    failed_id = _insert_snapshot(
        db_engine, repository_id, git(repo, "rev-parse", "HEAD"), status="failed"
    )
    with Session(db_engine) as session:
        row = SnapshotFile(
            snapshot_id=failed_id, path="stray.txt", status="imported", content="LEAK"
        )
        session.add(row)
        session.commit()
        file_id = row.id

    response = client.get(f"{SNAPSHOTS}/{completed['id']}/files/{file_id}")

    _assert_error(response, 404, "snapshot_file_not_found")


@pytest.mark.parametrize("bad_file_id", ["0", "abc", str(2**63)])
def test_get_file_rejects_invalid_file_id(
    client: TestClient, snapshot: dict[str, Any], bad_file_id: str
) -> None:
    _assert_error(
        client.get(f"{SNAPSHOTS}/{snapshot['id']}/files/{bad_file_id}"), 422, "validation_error"
    )


# --- 舊 commit 經 API 讀取（roadmap 驗收）---


def test_old_commit_snapshot_serves_old_content_via_api(
    client: TestClient, repo: Path, repository_id: int
) -> None:
    old_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "--quiet", "README.md")
    new_sha = commit_files(repo, {"src/app.py": "x = 2\n", "new.txt": "new\n"})
    old = _import(client, repository_id, old_sha)
    new = _import(client, repository_id, new_sha)
    # 工作目錄再被改動、未提交，也不影響已保存的快照
    (repo / "src" / "app.py").write_text("dirty\n", encoding="utf-8")

    def read(snapshot: dict[str, Any], path: str) -> str:
        file_id = _file_ids(client, snapshot["id"])[path]
        content: str = client.get(f"{SNAPSHOTS}/{snapshot['id']}/files/{file_id}").json()["content"]
        return content

    assert old["commit_sha"] == old_sha
    assert new["commit_sha"] == new_sha
    assert read(old, "src/app.py") == "x = 1\n"
    assert read(old, "README.md") == "hello\n"
    assert read(new, "src/app.py") == "x = 2\n"
    assert set(_file_ids(client, old["id"])) == {"README.md", "src/app.py", "src/util.py"}
    assert set(_file_ids(client, new["id"])) == {"src/app.py", "src/util.py", "new.txt"}
    # 以 commit_sha 在列表中也能對應回兩個版本
    listed = client.get(f"{REPOS}/{repository_id}/snapshots").json()["items"]
    assert {item["commit_sha"] for item in listed} == {old_sha, new_sha}


def test_failed_snapshot_stays_unreadable_after_a_new_snapshot_is_created(
    client: TestClient, settings: Settings, repository_id: int
) -> None:
    limited = settings.model_copy(update={"import_max_files": 1})
    with TestClient(create_app(limited)) as limited_client:
        assert limited_client.post(f"{REPOS}/{repository_id}/imports", json={}).status_code == 422
    snapshot_id = client.get(f"{REPOS}/{repository_id}/snapshots").json()["items"][0]["id"]
    _assert_error(client.get(f"{SNAPSHOTS}/{snapshot_id}/files"), 409, "snapshot_not_completed")

    # 預設設定的匯入是另一筆快照（匯入設定不同）；失敗的那筆仍然 409
    created = _import(client, repository_id)

    assert created["id"] != snapshot_id
    assert client.get(f"{SNAPSHOTS}/{created['id']}/files").status_code == 200
    _assert_error(client.get(f"{SNAPSHOTS}/{snapshot_id}/files"), 409, "snapshot_not_completed")


# --- 查詢效率：語句數不隨資料量成長（無 N+1）---


def _capture_statements(db_engine: Engine, action: Any) -> list[str]:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])  # before_cursor_execute(conn, cursor, statement, ...)

    event.listen(db_engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(db_engine, "before_cursor_execute", record)
    return statements


def test_detail_stats_use_one_group_by_query(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    names = {f"f{n}.txt": str(n) for n in range(30)}
    repo = init_repo(roots / "many", files=names)
    created = _import(client, _register(client, repo))

    def action() -> None:
        with Session(db_engine) as session:
            snapshot = snapshot_queries.get_snapshot(session, created["id"])
            snapshot_queries.count_files_by_status(session, snapshot)

    statements = _capture_statements(db_engine, action)

    # 取得快照 + 單一 GROUP BY，與檔案數無關
    assert len(statements) == 2
    assert "GROUP BY" in statements[1]


def test_list_queries_use_constant_statement_count(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "many2", files={f"f{n}.txt": str(n) for n in range(30)})
    created = _import(client, _register(client, repo))

    def list_files() -> None:
        with Session(db_engine) as session:
            snapshot_queries.list_files(
                session, created["id"], status=None, path_prefix="f", limit=200, offset=0
            )

    def list_snapshots() -> None:
        with Session(db_engine) as session:
            snapshot_queries.list_snapshots(
                session, created["repository_id"], status=None, limit=50, offset=0
            )

    # 檔案列表：快照檢查 + count + 一頁資料；快照列表：repo 檢查 + count + 一頁資料
    assert len(_capture_statements(db_engine, list_files)) == 3
    assert len(_capture_statements(db_engine, list_snapshots)) == 3
