"""快照內三種搜尋的端點（切片 8，規格 8）：資料直接寫入，請求走真正的 HTTP 管線。"""

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.main import create_app
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build

FILES = {
    "src/app.py": "import os\nclass Widget:\n    def render(self): ...\n",
    "src/util.py": "def helper(): ...\nWidget = None\n",
    "src/Widget.cs": "namespace Demo { class Widget { } }\n",
    "docs/readme.md": "Widget docs\r\nsecond Widget Widget line\r\nlast\r\n",
    "a%b/x_y.py": "rate = 100%\npath = 'C:\\\\tmp'\nname_x = 1\n",
    "z.txt": "nothing here\n",
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
    add_files(db_engine, snapshot, FILES)
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO snapshot_files (snapshot_id, path, status, skip_reason) "
                "VALUES (:s, 'src/skipped_Widget.bin', 'skipped', 'binary')"
            ),
            {"s": snapshot},
        )
    return snapshot


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    with Session(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def indexed(db_engine: Engine, db_session: Session, snapshot_id: int, settings: Settings) -> int:
    build(db_engine, db_session, snapshot_id, settings)
    return snapshot_id


def _search(client: TestClient, snapshot_id: int, endpoint: str, **params: Any) -> Any:
    return client.get(f"/api/v1/snapshots/{snapshot_id}/search/{endpoint}", params=params)


def _ok(client: TestClient, snapshot_id: int, endpoint: str, **params: Any) -> dict[str, Any]:
    response = _search(client, snapshot_id, endpoint, **params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- files ---


def test_file_search_matches_paths_and_reports_the_snapshot_envelope(
    client: TestClient, snapshot_id: int, repository_id: int
) -> None:
    body = _ok(client, snapshot_id, "files", q="src/")

    assert [item["path"] for item in body["items"]] == [
        "src/Widget.cs",  # 位元組順序：大寫在小寫之前
        "src/app.py",
        "src/util.py",
    ]
    assert body["snapshot_id"] == snapshot_id
    assert body["repository_id"] == repository_id
    assert body["commit_sha"] == "0" * 39 + "1"
    assert (body["total"], body["limit"], body["offset"], body["has_more"]) == (3, 20, 0, False)
    assert set(body["items"][0]) == {"file_id", "path", "byte_size"}


def test_file_search_is_case_sensitive_and_skips_files_that_were_not_imported(
    client: TestClient, snapshot_id: int
) -> None:
    assert [i["path"] for i in _ok(client, snapshot_id, "files", q="Widget")["items"]] == [
        "src/Widget.cs"
    ]
    assert _ok(client, snapshot_id, "files", q="widget")["items"] == []
    assert _ok(client, snapshot_id, "files", q="skipped")["total"] == 0


@pytest.mark.parametrize(
    ("q", "expected"),
    [("%", ["a%b/x_y.py"]), ("_", ["a%b/x_y.py"]), ("b/x_", ["a%b/x_y.py"]), ("\\", [])],
)
def test_file_search_treats_like_wildcards_as_literal_characters(
    client: TestClient, snapshot_id: int, q: str, expected: list[str]
) -> None:
    body = _ok(client, snapshot_id, "files", q=q)

    assert [item["path"] for item in body["items"]] == expected


def test_file_search_path_prefix_narrows_the_candidates(
    client: TestClient, snapshot_id: int
) -> None:
    body = _ok(client, snapshot_id, "files", q=".", path_prefix="src/")

    assert {item["path"] for item in body["items"]} == {
        "src/Widget.cs",
        "src/app.py",
        "src/util.py",
    }


def test_file_search_paginates_stably_with_a_total_and_has_more(
    client: TestClient, snapshot_id: int
) -> None:
    pages = [
        _ok(client, snapshot_id, "files", q=".", limit=2, offset=offset) for offset in (0, 2, 4)
    ]

    assert [p["has_more"] for p in pages] == [True, True, False]
    assert all(p["total"] == 6 for p in pages)
    paths = [item["path"] for p in pages for item in p["items"]]
    assert paths == sorted(paths) and len(set(paths)) == 6


# --- text ---


def test_text_search_returns_one_hit_per_line_ordered_by_path_then_line(
    client: TestClient, snapshot_id: int
) -> None:
    body = _ok(client, snapshot_id, "text", q="Widget")

    hits = [(item["path"], item["line"]) for item in body["items"]]
    assert hits == [
        ("docs/readme.md", 1),
        ("docs/readme.md", 2),  # 同一行兩處命中只算一筆
        ("src/Widget.cs", 1),
        ("src/app.py", 2),
        ("src/util.py", 2),
    ]
    assert body["total"] is None and body["has_more"] is False
    crlf_line = body["items"][1]
    assert crlf_line["snippet"] == "second Widget Widget line"  # 行尾的 \r 不屬於該行
    assert (crlf_line["snippet_truncated"], crlf_line["snippet_offset"]) == (False, 0)
    assert crlf_line["match_offset"] == len("second ")


def test_text_search_is_case_sensitive_literal_and_ignores_skipped_files(
    client: TestClient, snapshot_id: int
) -> None:
    assert _ok(client, snapshot_id, "text", q="widget")["items"] == []
    assert _ok(client, snapshot_id, "text", q="class Widget:")["items"][0]["line"] == 2
    # 前後空白有意義，不被 strip：第 1 行的 Widget 在行首，沒有前導空白
    spaced = _ok(client, snapshot_id, "text", q=" Widget")["items"]
    assert [(i["path"], i["line"]) for i in spaced if i["path"] == "docs/readme.md"] == [
        ("docs/readme.md", 2)
    ]


@pytest.mark.parametrize(
    ("q", "line"), [("100%", 1), ("e_x", 3), ("'C:\\\\tmp'", 2), ("%", 1), ("\\\\", 2)]
)
def test_text_search_treats_like_wildcards_and_backslashes_as_literals(
    client: TestClient, snapshot_id: int, q: str, line: int
) -> None:
    items = _ok(client, snapshot_id, "text", q=q)["items"]

    assert [(item["path"], item["line"]) for item in items] == [("a%b/x_y.py", line)]


def test_text_search_pagination_skips_offset_hits_and_flags_more(
    client: TestClient, snapshot_id: int
) -> None:
    everything = _ok(client, snapshot_id, "text", q="Widget", limit=100)["items"]

    first = _ok(client, snapshot_id, "text", q="Widget", limit=2)
    second = _ok(client, snapshot_id, "text", q="Widget", limit=2, offset=2)
    third = _ok(client, snapshot_id, "text", q="Widget", limit=2, offset=4)

    assert [p["has_more"] for p in (first, second, third)] == [True, True, False]
    assert first["items"] + second["items"] + third["items"] == everything
    assert _ok(client, snapshot_id, "text", q="Widget", limit=5)["has_more"] is False


def test_text_search_crosses_batches_without_losing_or_repeating_hits(
    client: TestClient, db_engine: Engine, repository_id: int
) -> None:
    snapshot = add_snapshot(db_engine, repository_id)
    add_files(db_engine, snapshot, {f"f/{n:03}.txt": f"needle {n}\nother\n" for n in range(45)})

    body_pages = [
        _ok(client, snapshot, "text", q="needle", limit=10, offset=offset)
        for offset in range(0, 50, 10)
    ]

    paths = [item["path"] for page in body_pages for item in page["items"]]
    assert paths == [f"f/{n:03}.txt" for n in range(45)]
    assert [page["has_more"] for page in body_pages] == [True, True, True, True, False]


def test_text_search_snippet_is_a_300_character_window_with_code_point_offsets(
    client: TestClient, db_engine: Engine, repository_id: int
) -> None:
    snapshot = add_snapshot(db_engine, repository_id)
    line = "😀" * 150 + "TARGET" + "x" * 400
    short = "a" * 40 + "TARGET"
    add_files(db_engine, snapshot, {"long.txt": line + "\n", "short.txt": short + "\n"})

    items = _ok(client, snapshot, "text", q="TARGET")["items"]

    long_hit, short_hit = items
    assert long_hit["snippet_truncated"] is True
    assert (long_hit["snippet_offset"], long_hit["match_offset"]) == (50, 100)
    assert len(long_hit["snippet"]) == 300
    start = long_hit["match_offset"]
    assert long_hit["snippet"][start : start + 6] == "TARGET"
    assert (short_hit["snippet"], short_hit["snippet_truncated"]) == (short, False)
    assert short_hit["match_offset"] == 40


def test_text_search_path_prefix_limits_the_scanned_files(
    client: TestClient, snapshot_id: int
) -> None:
    items = _ok(client, snapshot_id, "text", q="Widget", path_prefix="src/")["items"]

    assert {item["path"] for item in items} == {"src/Widget.cs", "src/app.py", "src/util.py"}


def test_text_search_works_without_a_structure_index_and_stays_inside_the_snapshot(
    client: TestClient, db_engine: Engine, repository_id: int, snapshot_id: int
) -> None:
    other = add_snapshot(db_engine, repository_id, index=2)
    add_files(db_engine, other, {"only-in-other.txt": "Widget in another snapshot\n"})

    items = _ok(client, snapshot_id, "text", q="Widget")["items"]

    assert "only-in-other.txt" not in {item["path"] for item in items}
    assert _ok(client, other, "text", q="Widget")["items"][0]["path"] == "only-in-other.txt"


# --- symbols ---


def test_symbol_search_requires_a_completed_index(client: TestClient, snapshot_id: int) -> None:
    response = _search(client, snapshot_id, "symbols", q="Widget")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "snapshot_index_not_ready"
    assert error["details"] == {"index_status": "none"}


def test_symbol_search_matches_name_or_qualified_name_in_a_stable_order(
    client: TestClient, indexed: int
) -> None:
    body = _ok(client, indexed, "symbols", q="Widget")

    assert [(i["path"], i["name"], i["kind"]) for i in body["items"]] == [
        ("src/Widget.cs", "Widget", "class"),
        ("src/app.py", "Widget", "class"),
        ("src/app.py", "render", "method"),  # 完整名稱 Widget.render 含 q
    ]
    assert body["total"] == 3 and body["has_more"] is False
    assert set(body["items"][0]) == {
        "symbol_id",
        "kind",
        "language",
        "name",
        "qualified_name",
        "file_id",
        "path",
        "start_line",
        "end_line",
        "has_errors",
    }
    by_qualified = _ok(client, indexed, "symbols", q="Widget.render")["items"]
    assert [(i["name"], i["kind"]) for i in by_qualified] == [("render", "method")]
    assert _ok(client, indexed, "symbols", q="render")["total"] == 1


def test_symbol_search_filters_by_kind_and_language_and_is_case_sensitive(
    client: TestClient, indexed: int
) -> None:
    assert (
        _ok(client, indexed, "symbols", q="Widget", kind="class", language="python")["total"] == 1
    )
    assert _ok(client, indexed, "symbols", q="Widget", language="csharp")["total"] == 1
    assert _ok(client, indexed, "symbols", q="Widget", language="python")["total"] == 2
    assert [
        i["name"] for i in _ok(client, indexed, "symbols", q="Widget", kind="method")["items"]
    ] == ["render"]
    assert _ok(client, indexed, "symbols", q="widget")["items"] == []


def test_symbol_search_paginates_with_total_and_has_more(client: TestClient, indexed: int) -> None:
    first = _ok(client, indexed, "symbols", q="e", limit=1)
    last = _ok(client, indexed, "symbols", q="e", limit=1, offset=first["total"] - 1)

    assert first["has_more"] is True and last["has_more"] is False
    assert len(first["items"]) == 1 and len(last["items"]) == 1


def test_symbol_search_treats_wildcards_as_literals(client: TestClient, indexed: int) -> None:
    assert _ok(client, indexed, "symbols", q="%")["items"] == []
    assert _ok(client, indexed, "symbols", q="_")["items"] == []


# --- 共通：驗證、狀態、逾時 ---


@pytest.mark.parametrize("kind", ["files", "symbols", "text"])
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": ""},
        {"q": "   "},
        {"q": "a\nb"},
        {"q": "a\rb"},
        {"q": "a\x00b"},
        {"q": "x" * 201},
        {"q": "a", "limit": 0},
        {"q": "a", "limit": 101},
        {"q": "a", "offset": -1},
        {"q": "a", "offset": 10_001},
    ],
)
def test_invalid_search_parameters_are_rejected_with_422(
    client: TestClient, snapshot_id: int, kind: str, params: dict[str, Any]
) -> None:
    response = _search(client, snapshot_id, kind, **params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_search_boundaries_are_accepted(client: TestClient, snapshot_id: int) -> None:
    body = _ok(client, snapshot_id, "files", q="x" * 200, limit=100, offset=10_000)

    assert body["items"] == [] and body["has_more"] is False


@pytest.mark.parametrize("kind", ["files", "symbols", "text"])
def test_search_on_a_snapshot_that_is_not_completed_is_409_and_unknown_is_404(
    client: TestClient, db_engine: Engine, repository_id: int, kind: str
) -> None:
    importing = add_snapshot(db_engine, repository_id, status="importing")

    not_completed = _search(client, importing, kind, q="a")
    unknown = _search(client, 999_999, kind, q="a")

    assert (not_completed.status_code, not_completed.json()["error"]["code"]) == (
        409,
        "snapshot_not_completed",
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "snapshot_not_found")


@pytest.mark.parametrize("kind", ["files", "symbols", "text"])
def test_a_search_that_exceeds_the_time_limit_is_504_not_an_empty_list(
    db_engine: Engine, settings: Settings, indexed: int, kind: str
) -> None:
    # 另一個連線持有 ACCESS EXCLUSIVE 鎖：搜尋等鎖直到 statement_timeout 取消查詢
    slow = settings.model_copy(update={"search_statement_timeout_seconds": 0.3})
    release = threading.Event()
    locked = threading.Event()

    def hold_lock() -> None:
        with db_engine.begin() as connection:
            connection.execute(text("LOCK TABLE snapshot_files, symbols IN ACCESS EXCLUSIVE MODE"))
            locked.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert locked.wait(timeout=10)
        with TestClient(create_app(slow)) as client:
            response = _search(client, indexed, kind, q="Widget")
    finally:
        release.set()
        holder.join(timeout=10)

    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "search_timeout"
