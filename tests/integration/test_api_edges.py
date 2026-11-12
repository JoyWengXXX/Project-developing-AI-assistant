"""切片 6：API 層的邊界（id 範圍、錯誤格式一致性、request ID、超長路徑）。

PostgreSQL ＋ 真實暫存 Git repo。"""

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from repopilot.db.models import Snapshot, SnapshotFile
from tests.git_helpers import commit_raw_paths, init_repo

URL = "/api/v1/repositories"


def _assert_unified_error(response: Any, status: int, code: str | None = None) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert set(error) >= {"code", "message", "request_id"}
    assert error["request_id"] == response.headers["X-Request-ID"]
    if code is not None:
        assert error["code"] == code


@pytest.mark.parametrize("bad_id", ["0", "-1"])
@pytest.mark.parametrize(
    ("method", "suffix"), [("GET", ""), ("POST", "/imports")], ids=["get-repository", "import"]
)
def test_non_positive_repository_id_is_a_422_with_the_unified_shape(
    client: TestClient, method: str, suffix: str, bad_id: str
) -> None:
    """與其他 repository 子端點一致：`0`、`-1` 在路徑驗證階段就是 422（D30、D37）。"""
    response = client.request(method, f"{URL}/{bad_id}{suffix}")

    _assert_unified_error(response, 422, "validation_error")


@pytest.mark.parametrize("bad_id", [str(2**31), str(2**63), "99999999999999999999"])
@pytest.mark.parametrize(
    ("method", "suffix"), [("GET", ""), ("POST", "/imports")], ids=["get-repository", "import"]
)
def test_out_of_range_repository_id_is_a_422_not_a_server_error(
    client: TestClient, method: str, suffix: str, bad_id: str
) -> None:
    """其他 repository 子端點（refs、commits、overview、DELETE）都以 422 拒絕超出 int32 的 id。"""
    response = client.request(method, f"{URL}/{bad_id}{suffix}")

    _assert_unified_error(response, 422, "validation_error")


@pytest.mark.parametrize(
    "path", ["/999", "/999/imports", "/999/snapshots", "/999/refs", "/999/commits"]
)
def test_missing_repository_is_404_with_the_same_error_shape_on_every_endpoint(
    client: TestClient, path: str
) -> None:
    method = "POST" if path.endswith("imports") else "GET"

    response = client.request(method, f"{URL}{path}")

    _assert_unified_error(response, 404, "repository_not_found")


@pytest.mark.parametrize(
    "path",
    ["/api/v1/snapshots/999", "/api/v1/snapshots/999/files", "/api/v1/snapshots/999/files/1"],
)
def test_missing_snapshot_is_404_with_the_same_error_shape_on_every_endpoint(
    client: TestClient, path: str
) -> None:
    _assert_unified_error(client.get(path), 404, "snapshot_not_found")


def test_error_responses_echo_a_valid_incoming_request_id_and_replace_an_invalid_one(
    client: TestClient,
) -> None:
    echoed = client.get(f"{URL}/999", headers={"X-Request-ID": "trace-me_1.2"})
    replaced = client.get(f"{URL}/999", headers={"X-Request-ID": "bad id with spaces!"})

    assert echoed.headers["X-Request-ID"] == "trace-me_1.2"
    assert echoed.json()["error"]["request_id"] == "trace-me_1.2"
    assert re.fullmatch(r"[0-9a-f]{32}", replaced.headers["X-Request-ID"])
    assert replaced.json()["error"]["request_id"] == replaced.headers["X-Request-ID"]


def test_successful_responses_carry_a_request_id_too(client: TestClient, roots: Path) -> None:
    repo = init_repo(roots / "demo")

    created = client.post(URL, json={"path": str(repo)})
    listing = client.get(URL)

    for response in (created, listing):
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])
    assert created.headers["X-Request-ID"] != listing.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", URL, {"path": 123}),
        ("POST", URL, {"path": ["a"]}),
        ("POST", URL, {"path": "x" * 4097}),
        ("POST", URL, {"path": "/a", "name": "n" * 201}),
        ("POST", f"{URL}/1/imports", {"revision": 5}),
        ("POST", f"{URL}/1/imports", {"revision": ""}),
        ("POST", f"{URL}/1/imports", {"revision": None}),
        ("POST", f"{URL}/1/imports", []),
    ],
    ids=[
        "path-int",
        "path-list",
        "path-too-long",
        "name-too-long",
        "revision-int",
        "revision-empty",
        "revision-null",
        "body-is-a-list",
    ],
)
def test_malformed_bodies_are_422_validation_errors_that_do_not_echo_the_input(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    response = client.request(method, path, json=body)

    _assert_unified_error(response, 422, "validation_error")
    assert "x" * 100 not in response.text


def test_non_json_and_wrongly_typed_bodies_are_422(client: TestClient) -> None:
    not_json = client.post(URL, content=b"{not json", headers={"Content-Type": "application/json"})
    wrong_type = client.post(URL, content=b"path=/a", headers={"Content-Type": "text/plain"})

    _assert_unified_error(not_json, 422, "validation_error")
    _assert_unified_error(wrong_type, 422, "validation_error")


def test_wrong_method_on_a_real_route_is_the_unified_405(client: TestClient) -> None:
    response = client.put(f"{URL}/1")

    _assert_unified_error(response, 405, "method_not_allowed")


# --- 超長路徑（btree 唯一索引的列大小上限） ---


def _long_path_repo(roots: Path) -> Path:
    # 5 個各 1,024 字元、不可壓縮的區段（共約 5,100 字元）；區段少，建立 tree 的 git 呼叫才少
    segments = [
        "".join(hashlib.sha256(f"{i}-{j}".encode()).hexdigest() for j in range(16))
        for i in range(5)
    ]
    repo = init_repo(roots / "longpath")
    commit_raw_paths(repo, {"/".join(segments).encode(): b"x\n", b"ok.txt": b"fine\n"})
    return repo


def test_a_very_long_path_never_leaves_a_half_imported_snapshot(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    """約 5,000 字元的路徑：PostgreSQL 的 btree 唯一索引有列大小上限（約 2.7 KB）。

    不論匯入成功或失敗，都不能留下 completed 但缺檔案、或 failed 卻有檔案列的半套資料。
    """
    repo = _long_path_repo(roots)
    repository_id = client.post(URL, json={"path": str(repo)}).json()["id"]

    response = client.post(f"{URL}/{repository_id}/imports")

    with Session(db_engine) as session:
        snapshot = session.scalars(select(Snapshot)).one()
        files = session.scalar(select(func.count()).select_from(SnapshotFile))
    if response.status_code == 201:
        assert (snapshot.status, files) == ("completed", 2)
    else:
        assert (snapshot.status, files) == ("failed", 0)
        files_response = client.get(f"/api/v1/snapshots/{snapshot.id}/files")
        _assert_unified_error(files_response, 409, "snapshot_not_completed")


def test_a_very_long_path_is_not_reported_as_database_unavailable(
    client: TestClient, roots: Path
) -> None:
    """路徑超過 `MAX_PATH_BYTES` 是輸入造成的失敗：422 `import_limit_exceeded`，不是 503（D37）。"""
    repo = _long_path_repo(roots)
    repository_id = client.post(URL, json={"path": str(repo)}).json()["id"]

    response = client.post(f"{URL}/{repository_id}/imports")

    _assert_unified_error(response, 422, "import_limit_exceeded")
    assert "ok.txt" not in response.text  # 訊息只有數字，不回顯任何路徑
