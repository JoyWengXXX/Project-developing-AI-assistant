"""C# 結構索引（切片 7）：真實 PostgreSQL，索引服務＋讀取端點。"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.services import job_poison
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build, count, index_row, scalar

CS = {
    "src/Orders/Order.cs": (
        "using System;\nnamespace Shop.Orders;\n\n"
        "[Serializable]\npublic partial class Order : EntityBase, IAuditable\n{\n"
        "    public int Id { get; set; }\n    private int _count;\n    public void Ship() { }\n}\n"
        "public class EntityBase { }\n"
    ),
    "src/Orders/Order.Part2.cs": (
        "namespace Shop.Orders\n{\n    public partial class Order\n    {\n"
        "        public async Task<bool> Pay() => true;\n    }\n}\n"
    ),
    "src/Legacy/Legacy.cs": (
        "class Legacy\n{\n#if NET8_0\n    void Modern() { }\n#else\n    void Old() { }\n#endif\n}\n"
    ),
    "src/Broken.cs": "class Ok { void Fine() { } }\nclass Broken {\n    void M( { }\n}\n",
    "src/日本語/ファイル.cs": "class 顧客 { int 数() => 1; }\n",
    "tools/helper.py": "def helper(): ...\n",
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
    add_files(db_engine, snapshot, CS)
    return snapshot


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    with Session(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def indexed(db_engine: Engine, db_session: Session, snapshot_id: int, settings: Settings) -> int:
    build(db_engine, db_session, snapshot_id, settings)
    return snapshot_id


def _symbols(client: TestClient, snapshot_id: int, **params: Any) -> list[dict[str, Any]]:
    response = client.get(
        f"/api/v1/snapshots/{snapshot_id}/symbols", params={"limit": 200, **params}
    )
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def test_csharp_and_python_files_are_indexed_together_with_their_languages(
    client: TestClient, indexed: int
) -> None:
    counts = client.get(f"/api/v1/snapshots/{indexed}/index").json()["counts"]

    assert counts["files_by_language"] == {"csharp": 5, "python": 1}
    assert counts["files_by_parse_status"] == {"parsed": 5, "parsed_with_errors": 1}
    by_language = {s["language"] for s in _symbols(client, indexed)}
    assert by_language == {"csharp", "python"}
    assert {s["qualified_name"] for s in _symbols(client, indexed, kind="namespace")} == {
        "Shop.Orders"
    }


def test_partial_classes_are_recorded_per_file_and_never_merged(
    client: TestClient, indexed: int
) -> None:
    orders = [s for s in _symbols(client, indexed, kind="class") if s["name"] == "Order"]

    assert len(orders) == 2
    assert {s["path"] for s in orders} == {"src/Orders/Order.cs", "src/Orders/Order.Part2.cs"}
    assert all("partial" in (s["modifiers"] or "") for s in orders)
    assert {s["qualified_name"] for s in orders} == {"Shop.Orders.Order"}  # 相同名稱，由位置區分


def test_members_attributes_base_types_and_async_via_the_symbol_endpoints(
    client: TestClient, indexed: int
) -> None:
    symbols = _symbols(client, indexed)
    order = next(
        s
        for s in symbols
        if s["qualified_name"] == "Shop.Orders.Order" and s["path"].endswith("Order.cs")
    )
    pay = next(s for s in symbols if s["qualified_name"] == "Shop.Orders.Order.Pay")

    detail = client.get(f"/api/v1/snapshots/{indexed}/symbols/{order['symbol_id']}").json()
    relations = {(r["relation_type"], r["target_name"]): r for r in detail["relations"]}

    assert detail["leading_start_line"] == 4  # 屬性清單的起始行
    assert relations[("attribute", "Serializable")]["resolution"] == "unresolved"
    assert relations[("base_type", "EntityBase")]["resolution"] == "resolved_same_file"
    assert relations[("base_type", "EntityBase")]["target_symbol_id"] is not None
    assert relations[("base_type", "IAuditable")]["resolution"] == "unresolved"
    assert [i["target"] for i in detail["imports"]] == ["System"]
    assert pay["kind"] == "method" and pay["is_async"] is True
    assert not [s for s in symbols if s["kind"] == "field"]  # 欄位預設不抽取


def test_ifdef_branches_both_appear_marked_conditional(client: TestClient, indexed: int) -> None:
    legacy = {
        s["name"]: s["conditional"]
        for s in _symbols(client, indexed, path_prefix="src/Legacy/")
        if s["kind"] in ("class", "method")
    }

    assert legacy == {"Legacy": False, "Modern": True, "Old": True}


def test_syntax_errors_are_partial_parses_listed_with_positions_and_flagged_symbols(
    client: TestClient, indexed: int
) -> None:
    problems = client.get(
        f"/api/v1/snapshots/{indexed}/index/files", params={"has_problems": True}
    ).json()
    (broken,) = problems["items"]
    symbols = {s["name"]: s for s in _symbols(client, indexed, path_prefix="src/Broken.cs")}

    assert broken["path"] == "src/Broken.cs" and broken["language"] == "csharp"
    assert broken["parse_status"] == "parsed_with_errors"
    assert broken["error_count"] >= 1 and broken["first_error"]["line"] == 3
    assert symbols["Broken"]["has_errors"] is True  # 可能不正確，標示而不隱藏
    assert symbols["Ok"]["has_errors"] is False
    assert "Fine" in symbols  # 其他宣告仍被抽取


def test_unicode_names_and_byte_columns_survive_the_database_round_trip(
    client: TestClient, indexed: int
) -> None:
    symbols = _symbols(client, indexed, path_prefix="src/日本語/")
    cls = next(s for s in symbols if s["kind"] == "class")
    detail = client.get(f"/api/v1/snapshots/{indexed}/symbols/{cls['symbol_id']}").json()

    assert cls["name"] == "顧客"
    assert detail["end_column"] == len("class 顧客 { int 数() => 1; }".encode())


def test_the_language_filter_and_other_languages_do_not_mix(
    client: TestClient, indexed: int
) -> None:
    csharp = client.get(f"/api/v1/snapshots/{indexed}/index/files", params={"language": "csharp"})
    python = client.get(f"/api/v1/snapshots/{indexed}/index/files", params={"language": "python"})

    assert csharp.json()["total"] == 5
    assert [f["path"] for f in python.json()["items"]] == ["tools/helper.py"]


def test_the_field_switch_changes_the_profile_so_a_new_index_is_built_and_the_old_one_cleaned(
    db_engine: Engine,
    db_session: Session,
    snapshot_id: int,
    test_database_url: str,
    settings: Settings,
) -> None:
    build(db_engine, db_session, snapshot_id, settings)
    with_fields = Settings(
        database_url=SecretStr(test_database_url), index_csharp_include_fields=True
    )

    summary = build(db_engine, db_session, snapshot_id, with_fields)

    assert not summary.reused  # 設定不同：不是重用
    assert count(db_engine, "snapshot_indexes") == 2
    fields = scalar(db_engine, "SELECT count(*) FROM symbols WHERE kind = 'field'")
    assert fields == 1  # _count
    from repopilot.indexing.profile import IndexProfile
    from repopilot.services import snapshot_indexing

    keep = IndexProfile.from_settings(with_fields).hash()
    assert snapshot_indexing.delete_outdated_indexes(db_session, snapshot_id, keep) == 1
    assert index_row(db_engine, snapshot_id)["analyzers"]["csharp"]["include_fields"] is True


def test_a_csharp_file_that_keeps_crashing_the_worker_is_skipped_as_a_csharp_parser_crash(
    db_engine: Engine, db_session: Session, snapshot_id: int, settings: Settings
) -> None:
    crashing = scalar(
        db_engine,
        "SELECT id FROM snapshot_files WHERE snapshot_id = :s AND path = 'src/Broken.cs'",
        s=snapshot_id,
    )
    state: dict[str, Any] = {
        "poison": {
            "suspect": {
                "unit": str(crashing),
                "kind": "file",
                "crash_kind": "parser_crash",
                "crashes": job_poison.SKIP_THRESHOLD,
            },
            "skipped": [],
            "total_crashes": 2,
        }
    }

    def persist(patch: dict[str, Any]) -> dict[str, Any]:
        state.update(patch)
        return state

    summary = build(
        db_engine,
        db_session,
        snapshot_id,
        settings,
        tracker=job_poison.PoisonTracker(state, persist),
    )

    assert summary.skipped_file_count == 1
    row = scalar(
        db_engine,
        "SELECT i.language || ':' || i.parse_status || ':' || i.error_kind FROM indexed_files i "
        "JOIN snapshot_files f ON f.id = i.snapshot_file_id WHERE f.path = 'src/Broken.cs'",
    )
    assert row == "csharp:failed:parser_crash"
