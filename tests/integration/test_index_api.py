"""結構索引的讀取端點（切片 6，規格 4.7）：索引以服務直接建立，端點走真正的 HTTP 管線。"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.indexing.profile import IndexProfile
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build

PY = {
    "pkg/models.py": (
        "import os\n"
        "from . import util\n"
        "@decorated(1)\n"
        "class User(Base, ext.Mixin):\n"
        "    async def name(self): ...\n"
        "class Base: ...\n"
    ),
    "pkg/util.py": "def helper(): ...\n",
    "pkg/broken.py": "def oops(:\n",
    "a%b/x_y.py": "class Odd: ...\n",
    "tests/test_it.py": "def test_it(): ...\n",
}


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
def snapshot_id(db_engine: Engine, repository_id: int) -> int:
    snapshot = add_snapshot(db_engine, repository_id)
    add_files(db_engine, snapshot, PY)
    return snapshot


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    with Session(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def indexed(db_engine: Engine, db_session: Session, snapshot_id: int, settings: Settings) -> int:
    build(db_engine, db_session, snapshot_id, settings)
    return snapshot_id


def _get(client: TestClient, path: str, **params: Any) -> Any:
    return client.get(path, params=params)


def _symbols(client: TestClient, snapshot_id: int, **params: Any) -> list[dict[str, Any]]:
    response = _get(client, f"/api/v1/snapshots/{snapshot_id}/symbols", limit=200, **params)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


# --- 索引狀態 ---


def test_a_snapshot_without_an_index_reports_none_instead_of_an_error(
    client: TestClient, snapshot_id: int
) -> None:
    response = _get(client, f"/api/v1/snapshots/{snapshot_id}/index")

    assert response.status_code == 200
    assert response.json() == {
        "snapshot_id": snapshot_id,
        "status": "none",
        "index_id": None,
        "started_at": None,
        "completed_at": None,
        "analyzers": {},
        "counts": None,
        "error_summary": None,
    }


def test_the_index_status_has_counts_analyzers_and_no_code_or_error_messages(
    client: TestClient, indexed: int
) -> None:
    body = _get(client, f"/api/v1/snapshots/{indexed}/index").json()

    assert body["status"] == "completed"
    assert body["analyzers"]["python"]["enabled"] is True
    assert body["analyzers"]["csharp"]["enabled"] is True
    assert body["analyzers"]["csharp"]["include_fields"] is False
    counts = body["counts"]
    assert counts["files_by_language"] == {"python": 5}
    assert counts["files_by_parse_status"] == {"parsed": 4, "failed": 1}
    assert counts["symbols_by_kind"]["class"] == 3  # User、Base、Odd
    assert counts["symbols"] == sum(counts["symbols_by_kind"].values())
    assert counts["truncated_files"] == 0
    assert body["error_summary"] is None


@pytest.mark.parametrize("state", ["building", "failed"])
def test_a_building_or_failed_index_is_reported_but_never_used(
    client: TestClient, db_engine: Engine, snapshot_id: int, settings: Settings, state: str
) -> None:
    profile = IndexProfile.from_settings(settings).hash()
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO snapshot_indexes (snapshot_id, index_profile_hash, status, "
                "owner_token) VALUES (:s, :p, :st, CASE WHEN CAST(:st AS varchar) = 'building' "
                "THEN gen_random_uuid() END)"
            ),
            {"s": snapshot_id, "p": profile, "st": state},
        )

    status = _get(client, f"/api/v1/snapshots/{snapshot_id}/index").json()
    assert status["status"] == state and status["counts"] is None
    for path in ("index/files", "symbols", "symbols/1"):
        response = _get(client, f"/api/v1/snapshots/{snapshot_id}/{path}")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "snapshot_index_not_ready"
        assert error["details"] == {"index_status": state}


def test_an_index_built_with_other_settings_is_not_used(
    client: TestClient, indexed: int, db_engine: Engine
) -> None:
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE snapshot_indexes SET index_profile_hash = 'old-profile'"))

    assert _get(client, f"/api/v1/snapshots/{indexed}/index").json()["status"] == "none"
    response = _get(client, f"/api/v1/snapshots/{indexed}/symbols")
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"index_status": "none"}


def test_missing_and_unfinished_snapshots_are_rejected_before_the_index_is_consulted(
    client: TestClient, db_engine: Engine, repository_id: int
) -> None:
    importing = add_snapshot(db_engine, repository_id, status="importing", index=2)

    assert _get(client, "/api/v1/snapshots/999/index").status_code == 404
    for path in ("index", "index/files", "symbols"):
        response = _get(client, f"/api/v1/snapshots/{importing}/{path}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "snapshot_not_completed"


# --- 檔案清單 ---


def test_indexed_files_list_is_sorted_by_path_and_filters_problem_files(
    client: TestClient, indexed: int
) -> None:
    everything = _get(client, f"/api/v1/snapshots/{indexed}/index/files").json()
    assert everything["total"] == 5
    assert [item["path"] for item in everything["items"]] == [
        "a%b/x_y.py",
        "pkg/broken.py",
        "pkg/models.py",
        "pkg/util.py",
        "tests/test_it.py",
    ]

    problems = _get(client, f"/api/v1/snapshots/{indexed}/index/files", has_problems=True).json()
    (broken,) = problems["items"]
    assert problems["total"] == 1
    assert broken["path"] == "pkg/broken.py"
    assert (broken["language"], broken["parse_status"], broken["error_count"]) == (
        "python",
        "failed",
        1,
    )
    assert broken["first_error"] == {"line": 1, "column": broken["first_error"]["column"]}
    assert set(broken) == {
        "indexed_file_id",
        "file_id",
        "path",
        "language",
        "parse_status",
        "error_count",
        "first_error",
        "symbol_count",
        "symbols_truncated",
    }  # 沒有程式碼片段、也沒有錯誤訊息
    assert everything["items"][0]["first_error"] is None


def test_indexed_files_pagination_is_stable_and_validated(client: TestClient, indexed: int) -> None:
    seen: list[str] = []
    for offset in range(0, 5, 2):
        page = _get(client, f"/api/v1/snapshots/{indexed}/index/files", limit=2, offset=offset)
        seen += [item["path"] for item in page.json()["items"]]
    assert len(seen) == len(set(seen)) == 5

    for bad in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"language": "ruby"}):
        assert _get(client, f"/api/v1/snapshots/{indexed}/index/files", **bad).status_code == 422


# --- 符號 ---


def test_symbols_are_listed_in_path_and_line_order_with_all_summary_fields(
    client: TestClient, indexed: int
) -> None:
    symbols = _symbols(client, indexed)

    keys = [(s["path"], s["start_line"]) for s in symbols]
    assert keys == sorted(keys)
    by_name = {s["qualified_name"]: s for s in symbols}
    user = by_name["pkg.models.User"]
    assert user["kind"] == "class" and user["language"] == "python"
    assert (user["start_line"], user["end_line"]) == (4, 5)
    assert user["has_children"] is True
    assert by_name["pkg.models.Base"]["has_children"] is False
    method = by_name["pkg.models.User.name"]
    assert method["kind"] == "method" and method["is_async"] is True
    assert method["parent_id"] == user["symbol_id"]
    assert set(user) == {
        "symbol_id",
        "kind",
        "language",
        "name",
        "qualified_name",
        "file_id",
        "path",
        "start_line",
        "end_line",
        "parent_id",
        "modifiers",
        "is_async",
        "has_errors",
        "conditional",
        "has_children",
    }


def test_the_symbol_tree_loads_level_by_level(client: TestClient, indexed: int) -> None:
    modules = _symbols(client, indexed, top_level=True)
    assert {m["kind"] for m in modules} == {"module"}
    assert len(modules) == 4  # broken.py 失敗，沒有符號
    models = next(m for m in modules if m["qualified_name"] == "pkg.models")
    assert models["has_children"] is True

    children = _symbols(client, indexed, parent_id=models["symbol_id"])
    assert [c["qualified_name"] for c in children] == ["pkg.models.User", "pkg.models.Base"]
    user = children[0]
    assert [c["name"] for c in _symbols(client, indexed, parent_id=user["symbol_id"])] == ["name"]
    assert _symbols(client, indexed, top_level=True, parent_id=user["symbol_id"]) == []


def test_symbol_filters_treat_like_wildcards_as_literals(client: TestClient, indexed: int) -> None:
    assert [s["qualified_name"] for s in _symbols(client, indexed, kind="method")] == [
        "pkg.models.User.name"
    ]
    literal = _symbols(client, indexed, path_prefix="a%b/")
    assert {s["path"] for s in literal} == {"a%b/x_y.py"}
    assert _symbols(client, indexed, path_prefix="a%") != []
    assert _symbols(client, indexed, path_prefix="%") == []  # `%` 不是萬用字元
    assert _symbols(client, indexed, path_prefix="_") == []
    file_id = next(s["file_id"] for s in _symbols(client, indexed) if s["path"] == "pkg/util.py")
    assert {s["path"] for s in _symbols(client, indexed, file_id=file_id)} == {"pkg/util.py"}
    assert _get(client, f"/api/v1/snapshots/{indexed}/symbols", kind="widget").status_code == 422


def test_symbol_pagination_has_a_stable_total(client: TestClient, indexed: int) -> None:
    total = _get(client, f"/api/v1/snapshots/{indexed}/symbols", limit=1).json()["total"]
    names: list[int] = []
    for offset in range(total):
        page = _get(client, f"/api/v1/snapshots/{indexed}/symbols", limit=1, offset=offset).json()
        assert page["total"] == total
        names += [item["symbol_id"] for item in page["items"]]
    assert len(names) == len(set(names)) == total


def test_symbol_detail_has_positions_decorator_line_relations_and_same_file_imports(
    client: TestClient, indexed: int
) -> None:
    user_id = next(
        s["symbol_id"]
        for s in _symbols(client, indexed)
        if s["qualified_name"] == "pkg.models.User"
    )

    detail = _get(client, f"/api/v1/snapshots/{indexed}/symbols/{user_id}").json()

    assert (detail["start_line"], detail["end_line"]) == (4, 5)
    assert (detail["start_column"], detail["end_column"]) == (
        0,
        len("    async def name(self): ..."),
    )
    assert detail["leading_start_line"] == 3  # 裝飾器行
    assert detail["type_parameter_count"] == 0
    relations = {r["target_name"]: r for r in detail["relations"]}
    assert relations["Base"]["resolution"] == "resolved_same_file"
    assert relations["Base"]["target_symbol_id"] is not None
    assert relations["ext.Mixin"] == {
        "relation_type": "base_class",
        "target_name": "ext.Mixin",
        "target_symbol_id": None,
        "resolution": "unresolved",
        "line": 4,
    }
    assert relations["decorated"]["relation_type"] == "decorator"  # 只有名稱，沒有引數
    assert [
        (i["kind"], i["target"], i["imported_name"], i["relative_level"]) for i in detail["imports"]
    ] == [
        ("import", "os", None, None),
        ("from_import", "", "util", 1),
    ]


def test_a_symbol_of_another_snapshots_index_is_not_found(
    client: TestClient,
    db_engine: Engine,
    db_session: Session,
    repository_id: int,
    indexed: int,
    settings: Settings,
) -> None:
    other = add_snapshot(db_engine, repository_id, index=2)
    add_files(db_engine, other, {"o.py": "class Other: ...\n"})
    build(db_engine, db_session, other, settings, token=uuid.uuid4())
    foreign = _symbols(client, other)[0]["symbol_id"]

    assert _get(client, f"/api/v1/snapshots/{indexed}/symbols/{foreign}").status_code == 404
    assert (
        _get(client, f"/api/v1/snapshots/{indexed}/symbols/{foreign}").json()["error"]["code"]
        == "symbol_not_found"
    )
    assert _get(client, f"/api/v1/snapshots/{other}/symbols/{foreign}").status_code == 200
    assert _get(client, f"/api/v1/snapshots/{indexed}/symbols/0").status_code == 422


def test_symbols_never_leak_across_snapshots(
    client: TestClient,
    db_engine: Engine,
    db_session: Session,
    repository_id: int,
    indexed: int,
    settings: Settings,
) -> None:
    other = add_snapshot(db_engine, repository_id, index=2)
    add_files(db_engine, other, {"o.py": "class Other: ...\n"})
    build(db_engine, db_session, other, settings)

    assert "Other" not in {s["name"] for s in _symbols(client, indexed)}
    assert {s["name"] for s in _symbols(client, other)} == {"o", "Other"}


# --- 契約 ---


def test_openapi_documents_the_index_endpoints_and_the_job_submission(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    for path in (
        "/api/v1/snapshots/{snapshot_id}/index-jobs",
        "/api/v1/snapshots/{snapshot_id}/index",
        "/api/v1/snapshots/{snapshot_id}/index/files",
        "/api/v1/snapshots/{snapshot_id}/symbols",
        "/api/v1/snapshots/{snapshot_id}/symbols/{symbol_id}",
    ):
        assert path in paths
    assert "202" in paths["/api/v1/snapshots/{snapshot_id}/index-jobs"]["post"]["responses"]
