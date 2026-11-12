"""切片 6：匯入的邊界與惡意輸入（PostgreSQL ＋ 真實暫存 Git repo）。

極端路徑以 `mktree` 直接寫入 Git 物件（Windows 的工作目錄與 `git add` 建不出 `..`、`.git`、
保留名、尾端空白、非 UTF-8 位元組），其餘以真實的 git 指令建立。測試用的秘密一律是明顯的假值。
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.main import create_app
from repopilot.services.snapshots import MAX_PATH_BYTES
from tests.git_helpers import (
    commit_files,
    commit_raw_paths,
    commit_special_entries,
    git,
    init_repo,
)

URL = "/api/v1/repositories"
SNAPSHOTS = "/api/v1/snapshots"
FAKE_SECRET = "FAKE_KEY_FOR_TEST_do_not_store"


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


def _client_with(settings: Settings, **overrides: Any) -> TestClient:
    return TestClient(create_app(settings.model_copy(update=overrides)))


# --- 路徑：不安全的區段、原樣保留的怪名稱、非 UTF-8 ---


def test_unsafe_path_entries_are_skipped_without_blob_or_content(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "evil")
    unsafe = [
        b"a/../b.txt",
        b"../escape.txt",
        b".git/config",
        b"sub/.git/hooks/pre-commit",
        b"other/.git",
        b"./x.txt",
        b"a/./b.txt",
    ]
    commit_raw_paths(repo, {**dict.fromkeys(unsafe, FAKE_SECRET.encode()), b"ok.txt": b"fine\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 201
    files = _files(db_engine, response.json()["id"])
    assert set(files) == {path.decode() for path in unsafe} | {"ok.txt"}
    for path in unsafe:
        row = files[path.decode()]
        assert (row.status, row.skip_reason) == ("skipped", "unsafe_path"), path
        assert (row.content, row.sha256, row.git_blob_id, row.byte_size) == (None,) * 4, path
    assert files["ok.txt"].status == "imported"
    # 不安全路徑的 blob 不讀取，因此不計入 total_bytes
    assert response.json()["total_bytes"] == len(b"fine\n")
    assert (response.json()["file_count"], response.json()["skipped_count"]) == (1, len(unsafe))


def test_odd_but_safe_path_names_are_stored_verbatim_and_imported(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    names = [
        "CON",  # Windows 保留名
        "aux/COM1.txt",
        "nul.txt",
        "trailing space ",
        "trailing.dot.",
        " leading space.txt",
        "😀 emoji/表情.md",
        "e\u0301.txt",  # NFD
        "\u00e9.txt",  # NFC：與上一個是不同的路徑
        "A.txt",
        "a.txt",  # 只差大小寫：在不分大小寫的檔案系統上無法同時簽出，但 Git 物件可以
        "tab\there.txt",
        "new\nline.txt",
        'quote"s.txt',
        "back\\slash.txt",
        "...",
        ".gitignore",
        ".gitmodules",
        ".github/workflows/ci.yml",
        "a.git/x",
        ".git.txt",
        "x" * 255,
    ]
    repo = init_repo(roots / "odd")
    commit_raw_paths(repo, {name.encode(): f"content of {name!r}\n".encode() for name in names})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 201
    files = _files(db_engine, response.json()["id"])
    assert set(files) == set(names)
    for name in names:
        assert files[name].status == "imported", name
        assert files[name].content == f"content of {name!r}\n", name
    listing = client.get(f"{SNAPSHOTS}/{response.json()['id']}/files?limit=200").json()
    assert {item["path"] for item in listing["items"]} == set(names)


def test_non_utf8_path_is_kept_as_backslash_escape_and_content_is_imported(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "latin1")
    commit_raw_paths(repo, {b"caf\xe9/na\xefve.txt": b"latin-1 name\n", b"ok.txt": b"x\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 201
    files = _files(db_engine, response.json()["id"])
    assert set(files) == {"caf\\xe9/na\\xefve.txt", "ok.txt"}
    assert files["caf\\xe9/na\\xefve.txt"].content == "latin-1 name\n"


def test_non_utf8_path_colliding_with_its_escaped_spelling_fails_the_whole_import(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    """D27 的已知取捨：兩個 Git 路徑經 backslashreplace 後相同，整個匯入失敗且不留半套資料。"""
    repo = init_repo(roots / "collide")
    commit_raw_paths(repo, {b"\xff.txt": b"raw byte name\n", b"\\xff.txt": b"literal name\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    with Session(db_engine) as session:
        snapshot = session.scalars(select(Snapshot)).one()
    assert snapshot.status == "failed"
    assert _file_count(db_engine) == 0
    files = client.get(f"{SNAPSHOTS}/{snapshot.id}/files")
    assert files.status_code == 409
    assert files.json()["error"]["code"] == "snapshot_not_completed"


def test_tree_with_an_empty_entry_name_is_refused_by_git_and_leaves_no_partial_data(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    """`a//b.txt` 在 tree 內是空名稱的項目：Git 的 `ls-tree` 拒絕，匯入失敗且不留半套資料。"""
    repo = init_repo(roots / "empty-name")
    commit_raw_paths(repo, {b"a//b.txt": b"x\n", b"ok.txt": b"fine\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "git_command_failed"
    with Session(db_engine) as session:
        snapshot = session.scalars(select(Snapshot)).one()
    assert (snapshot.status, snapshot.file_count) == ("failed", 0)
    assert _file_count(db_engine) == 0


def test_deeply_nested_path_is_imported(client: TestClient, roots: Path, db_engine: Engine) -> None:
    deep = "/".join(f"d{i}" for i in range(40)) + "/leaf.txt"  # 約 200 字元，40 層
    repo = init_repo(roots / "deep")
    commit_raw_paths(repo, {deep.encode(): b"leaf\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 201
    assert _files(db_engine, response.json()["id"])[deep].content == "leaf\n"


# --- 路徑長度上限（UTF-8 位元組；D37）：剛好等於上限成功、上限 + 1 整個匯入以 422 失敗 ---


def _incompressible_path(size_bytes: int, seed: str = "p") -> str:
    """ASCII、不可壓縮、恰好 `size_bytes` 位元組的巢狀路徑（每 200 字元一個 `/`，不在頭尾）。"""
    text = ""
    counter = 0
    while len(text) < size_bytes:
        text += hashlib.sha256(f"{seed}{counter}".encode()).hexdigest()
        counter += 1
    chars = list(text[:size_bytes])
    for position in range(199, size_bytes - 1, 200):
        chars[position] = "/"
    return "".join(chars)


def _cjk_path(char_count: int) -> str:
    """`char_count` 個不重複的 3 位元組 CJK 字元 + `/f`：位元組數 = 3 * char_count + 2。"""
    return "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(char_count)) + "/f"


@pytest.mark.parametrize(
    ("delta", "expected"), [(0, 201), (1, 422)], ids=["exactly-at-limit", "one-byte-over"]
)
def test_path_length_limit_is_exact_for_ascii_paths(
    client: TestClient, roots: Path, db_engine: Engine, delta: int, expected: int
) -> None:
    path = _incompressible_path(MAX_PATH_BYTES + delta)
    assert len(path.encode()) == MAX_PATH_BYTES + delta
    repo = init_repo(roots / "path-ascii")
    commit_raw_paths(repo, {path.encode(): b"x\n", b"ok.txt": b"fine\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == expected
    if expected == 201:  # 不可壓縮的最長合法路徑能實際寫進唯一索引
        assert set(_files(db_engine, response.json()["id"])) == {path, "ok.txt"}
    else:
        assert response.json()["error"]["code"] == "import_limit_exceeded"
        assert path[:40] not in response.text  # 訊息只有數字，不回顯路徑
        assert _file_count(db_engine) == 0
        with Session(db_engine) as session:
            snapshot = session.scalars(select(Snapshot)).one()
        assert snapshot.status == "failed"
        assert path[:40] not in (snapshot.error_summary or "")


@pytest.mark.parametrize(
    ("char_count", "expected"), [(682, 201), (683, 422)], ids=["2048-bytes", "2051-bytes"]
)
def test_path_length_limit_counts_utf8_bytes_not_characters(
    client: TestClient, roots: Path, db_engine: Engine, char_count: int, expected: int
) -> None:
    """683 個 CJK 字元只有 685 個字元（遠小於 2,048），但有 2,051 位元組，必須被拒絕。"""
    path = _cjk_path(char_count)
    assert len(path.encode()) == 3 * char_count + 2
    assert (len(path.encode()) <= MAX_PATH_BYTES) == (expected == 201)
    repo = init_repo(roots / "path-cjk")
    commit_raw_paths(repo, {path.encode(): b"x\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == expected
    if expected == 201:
        assert set(_files(db_engine, response.json()["id"])) == {path}
    else:
        assert response.json()["error"]["code"] == "import_limit_exceeded"
        assert _file_count(db_engine) == 0


def test_an_overlong_path_that_would_be_skipped_still_fails_the_import(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    """不安全路徑（`..` 區段）本來是 skipped，但 skipped 列同樣要把路徑寫進唯一索引，所以也受限。"""
    path = "../" + _incompressible_path(MAX_PATH_BYTES, seed="unsafe")
    repo = init_repo(roots / "path-unsafe")
    commit_raw_paths(repo, {path.encode(): b"x\n", b"ok.txt": b"fine\n"})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "import_limit_exceeded"
    assert _file_count(db_engine) == 0


def test_the_path_limit_is_below_the_btree_row_limit_measured_on_postgresql(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    """記錄實測依據：8 KiB 區塊下，不可壓縮的路徑到 2,688 位元組仍可寫入唯一索引、2,689 失敗。

    若資料庫版本或區塊大小改變使此測試失敗，需重新檢視 `MAX_PATH_BYTES`。
    """
    repo = init_repo(roots / "path-db-limit", files={"a.txt": "a\n"})
    repository_id = _register(client, repo)
    snapshot_id = _import(client, repository_id).json()["id"]

    def insert(size: int) -> bool:
        path = _incompressible_path(size, seed="db")
        try:
            with db_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO snapshot_files (snapshot_id, path, status) "
                        "VALUES (:snapshot_id, :path, 'skipped')"
                    ),
                    {"snapshot_id": snapshot_id, "path": path},
                )
        except DBAPIError:
            return False
        return True

    assert MAX_PATH_BYTES < 2688
    assert insert(2688) is True
    assert insert(2689) is False


# --- 檔案大小、檔案數、總大小：剛好等於上限與上限 + 1 ---


def test_file_exactly_at_the_size_limit_is_imported_and_one_byte_more_is_too_large(
    client: TestClient, settings: Settings, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "limit", files={"seed.txt": "s"})
    commit_files(repo, {"at_limit.txt": "a" * 100, "over_limit.txt": "b" * 101})
    repository_id = _register(client, repo)

    with _client_with(settings, import_max_file_bytes=100) as limited:
        response = _import(limited, repository_id)

    assert response.status_code == 201
    files = _files(db_engine, response.json()["id"])
    assert (files["at_limit.txt"].status, files["at_limit.txt"].content) == ("imported", "a" * 100)
    assert (files["over_limit.txt"].status, files["over_limit.txt"].skip_reason) == (
        "skipped",
        "too_large",
    )
    assert files["over_limit.txt"].content is None
    assert files["over_limit.txt"].byte_size == 101
    assert response.json()["total_bytes"] == len("s") + 100  # 超限的檔案未讀取


@pytest.mark.parametrize(("extra_files", "expected"), [(0, 201), (1, 422)])
def test_file_count_exactly_at_the_limit_succeeds_and_one_more_fails(
    client: TestClient,
    settings: Settings,
    roots: Path,
    db_engine: Engine,
    extra_files: int,
    expected: int,
) -> None:
    limit = 5
    repo = init_repo(
        roots / "count", files={f"f{i}.txt": f"{i}\n" for i in range(limit + extra_files)}
    )
    repository_id = _register(client, repo)

    with _client_with(settings, import_max_files=limit) as limited:
        response = _import(limited, repository_id)

    assert response.status_code == expected
    if expected == 201:
        assert _file_count(db_engine) == limit
    else:
        assert response.json()["error"]["code"] == "import_limit_exceeded"
        assert _file_count(db_engine) == 0
        with Session(db_engine) as session:
            assert session.scalars(select(Snapshot)).one().status == "failed"


def test_skipped_entries_count_toward_the_file_count_limit(
    client: TestClient, settings: Settings, roots: Path, db_engine: Engine
) -> None:
    """上限算的是 tree 內所有項目（含 symlink、submodule、不安全路徑），不只是會被讀取的檔案。"""
    repo = init_repo(roots / "count-skipped", files={"a.txt": "a\n", "b.txt": "b\n"})
    commit_raw_paths(repo, {b"a.txt": b"a\n", b"b.txt": b"b\n", b"../c.txt": b"c\n"})
    repository_id = _register(client, repo)

    with _client_with(settings, import_max_files=2) as limited:
        response = _import(limited, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "import_limit_exceeded"
    assert _file_count(db_engine) == 0


@pytest.mark.parametrize(("second_size", "expected"), [(5, 201), (6, 422)])
def test_total_size_exactly_at_the_limit_succeeds_and_one_byte_more_fails(
    client: TestClient,
    settings: Settings,
    roots: Path,
    db_engine: Engine,
    second_size: int,
    expected: int,
) -> None:
    repo = init_repo(roots / "total", files={"a.txt": "a" * 5, "b.txt": "b" * second_size})
    repository_id = _register(client, repo)

    with _client_with(settings, import_max_snapshot_bytes=10) as limited:
        response = _import(limited, repository_id)

    assert response.status_code == expected
    if expected == 201:
        assert response.json()["total_bytes"] == 10
        assert _file_count(db_engine) == 2
    else:
        assert response.json()["error"]["code"] == "import_limit_exceeded"
        assert _file_count(db_engine) == 0


# --- repo 的形狀：空 repo、detached HEAD、shallow、alternates ---


def test_importing_an_empty_repository_returns_422_and_creates_no_snapshot(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = roots / "empty"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_revision"
    assert _snapshot_count(db_engine) == 0


def test_detached_head_imports_the_detached_commit_not_the_branch_tip(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "detached", files={"a.txt": "branch tip\n"})
    branch_tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--quiet", "--detach")
    detached = commit_files(repo, {"a.txt": "detached work\n"})
    repository_id = _register(client, repo)

    head = _import(client, repository_id).json()
    branch = _import(client, repository_id, revision="main").json()

    assert git(repo, "rev-parse", "main") == branch_tip
    assert head["commit_sha"] == detached
    assert branch["commit_sha"] == branch_tip
    assert _files(db_engine, head["id"])["a.txt"].content == "detached work\n"
    assert _files(db_engine, branch["id"])["a.txt"].content == "branch tip\n"


def test_shallow_clone_imports_the_available_commit_and_rejects_the_missing_parent(
    client: TestClient, roots: Path, tmp_path: Path, db_engine: Engine
) -> None:
    origin = init_repo(tmp_path / "origin", files={"a.txt": "one\n"})
    first = git(origin, "rev-parse", "HEAD")
    second = commit_files(origin, {"a.txt": "two\n"})
    shallow = roots / "shallow"
    git(tmp_path, "clone", "--quiet", "--depth", "1", origin.as_uri(), str(shallow))
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    repository_id = _register(client, shallow)

    head = _import(client, repository_id)
    parent_by_expression = _import(client, repository_id, revision="HEAD~1")
    parent_by_sha = _import(client, repository_id, revision=first)

    assert head.status_code == 201
    assert head.json()["commit_sha"] == second
    assert _files(db_engine, head.json()["id"])["a.txt"].content == "two\n"
    assert parent_by_expression.status_code == 422
    assert parent_by_sha.status_code == 422
    assert parent_by_sha.json()["error"]["code"] == "invalid_revision"
    assert _snapshot_count(db_engine) == 1


def test_repository_whose_objects_live_in_alternates_can_be_imported(
    client: TestClient, roots: Path, tmp_path: Path, db_engine: Engine
) -> None:
    origin = init_repo(tmp_path / "origin", files={"a.txt": "shared object\n"})
    shared = roots / "shared"
    git(tmp_path, "clone", "--quiet", "--shared", str(origin), str(shared))
    assert (shared / ".git" / "objects" / "info" / "alternates").exists()
    blob_id = git(shared, "rev-parse", "HEAD:a.txt")
    loose = shared / ".git" / "objects" / blob_id[:2] / blob_id[2:]
    assert not loose.exists()  # 這個 blob 只存在於 alternates 指向的物件庫
    repository_id = _register(client, shared)

    response = _import(client, repository_id)

    assert response.status_code == 201
    assert _files(db_engine, response.json()["id"])["a.txt"].content == "shared object\n"


# --- revision：tag 指向 tree／blob、特殊字元、長度 ---


@pytest.mark.parametrize("target", ["HEAD^{tree}", "HEAD:README.md"])
@pytest.mark.parametrize("annotated", [False, True])
def test_tag_pointing_to_a_tree_or_blob_is_rejected_with_422(
    client: TestClient, roots: Path, db_engine: Engine, target: str, annotated: bool
) -> None:
    repo = init_repo(roots / "tags")
    if annotated:
        git(repo, "tag", "-a", "bad-tag", target, "-m", "points to a non-commit")
    else:
        git(repo, "tag", "bad-tag", target)
    repository_id = _register(client, repo)

    response = _import(client, repository_id, revision="bad-tag")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_revision"
    assert _snapshot_count(db_engine) == 0


def test_chain_of_annotated_tags_is_peeled_to_the_commit(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "tagchain")
    git(repo, "tag", "-a", "inner", "-m", "inner")
    git(repo, "tag", "-a", "outer", "inner", "-m", "tag of a tag")
    repository_id = _register(client, repo)

    response = _import(client, repository_id, revision="outer")

    assert response.status_code == 201
    assert response.json()["commit_sha"] == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "revision",
    [
        "\x00",
        "HEAD\x00",
        "HEAD\x1b[31m",
        "HEAD\x7f",
        "HEAD\tmain",
        "HEAD\r",
        " HEAD",
        "HEAD ",
        "a" * 256,
    ],
    ids=[
        "nul",
        "trailing-nul",
        "esc",
        "del",
        "tab",
        "cr",
        "leading-space",
        "trailing-space",
        "len-256",
    ],
)
def test_revision_with_control_characters_or_spaces_is_rejected_without_a_snapshot(
    client: TestClient, roots: Path, db_engine: Engine, revision: str
) -> None:
    repo = init_repo(roots / "revs")
    repository_id = _register(client, repo)

    response = _import(client, repository_id, revision=revision)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_revision"
    assert _snapshot_count(db_engine) == 0


@pytest.mark.parametrize("revision", ["a" * 257, "x" * 10_000], ids=["257-chars", "10000-chars"])
def test_overlong_revision_is_a_validation_error_before_git_runs(
    client: TestClient,
    roots: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    revision: str,
) -> None:
    repo = init_repo(roots / "long")
    repository_id = _register(client, repo)

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("git must not run for a revision that fails validation")

    monkeypatch.setattr(subprocess, "run", forbidden)

    response = _import(client, repository_id, revision=revision)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert _snapshot_count(db_engine) == 0


def test_branch_name_with_unicode_and_slashes_can_be_imported(
    client: TestClient, roots: Path
) -> None:
    repo = init_repo(roots / "unicode-branch")
    git(repo, "branch", "功能/中文-😀")
    repository_id = _register(client, repo)

    response = _import(client, repository_id, revision="功能/中文-😀")

    assert response.status_code == 201
    assert response.json()["commit_sha"] == git(repo, "rev-parse", "HEAD")


# --- 秘密檔案：整組預設排除規則走完整匯入管線 ---


def test_every_default_secret_pattern_is_excluded_and_content_never_reaches_the_database(
    client: TestClient, settings: Settings, roots: Path, db_engine: Engine
) -> None:
    secrets = [
        ".env",
        ".ENV",
        "config/.env.production",
        "id_rsa",
        "keys/Id_Rsa.pub",
        "id_ed25519",
        "server.pem",
        "deep/er/private.KEY",
        "cert.p12",
        "cert.pfx",
        "credentials.json",
        "aws/credentials-prod.json",
        ".npmrc",
        "sub/.pypirc",
        ".netrc",
    ]
    # 以 mktree 建立：Windows 的工作目錄不分大小寫，`.env` 與 `.ENV` 無法同時存在
    tree = {name.encode(): FAKE_SECRET.encode() for name in secrets}
    # 超過單檔上限的秘密檔仍應標為 excluded（而不是 too_large），且不讀取
    tree[b"huge/.env"] = (FAKE_SECRET + "x" * 2000).encode()
    tree[b"app.py"] = b"x = 1\n"
    tree[b".env.example"] = b"K=\n"
    repo = init_repo(roots / "secrets-all")
    commit_raw_paths(repo, tree)
    repository_id = _register(client, repo)

    with _client_with(settings, import_max_file_bytes=1000) as limited:
        response = _import(limited, repository_id)
        snapshot_id = response.json()["id"]
        listing = limited.get(f"{SNAPSHOTS}/{snapshot_id}/files?limit=200")
        details = [
            limited.get(f"{SNAPSHOTS}/{snapshot_id}/files/{item['id']}").text
            for item in listing.json()["items"]
        ]

    assert response.status_code == 201
    files = _files(db_engine, snapshot_id)
    for name in [*secrets, "huge/.env"]:
        row = files[name]
        assert (row.status, row.skip_reason) == ("skipped", "excluded"), name
        assert (row.content, row.sha256) == (None, None), name
    assert files[".env.example"].status == "imported"
    assert files["app.py"].status == "imported"
    # 整個資料庫與所有 API 回應都不含秘密內容
    with db_engine.connect() as connection:
        leaked = connection.execute(
            text("SELECT count(*) FROM snapshot_files WHERE content LIKE :needle"),
            {"needle": f"%{FAKE_SECRET}%"},
        ).scalar_one()
    assert leaked == 0
    assert FAKE_SECRET not in listing.text
    assert all(FAKE_SECRET not in body for body in details)
    expected_bytes = len("x = 1\n") + len("K=\n")
    assert response.json()["total_bytes"] == expected_bytes


def test_secret_named_symlink_and_gitlink_are_skipped_not_read(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "secret-links", files={"real.txt": FAKE_SECRET})
    commit_special_entries(repo, symlinks={".env": "real.txt"}, gitlinks={"id_rsa": "d" * 40})
    repository_id = _register(client, repo)

    response = _import(client, repository_id)

    files = _files(db_engine, response.json()["id"])
    assert files[".env"].content is None
    assert files["id_rsa"].content is None
    assert files["id_rsa"].skip_reason == "submodule"
    assert files[".env"].skip_reason in ("symlink", "excluded")
    assert files["real.txt"].sha256 == hashlib.sha256(FAKE_SECRET.encode()).hexdigest()


# --- 註冊後路徑被竄改、設定被改：匯入前重驗 ---


@pytest.mark.parametrize("kind", ["outside_roots", "subdirectory", "not_a_repo", "roots_changed"])
def test_import_revalidates_the_registered_path(
    client: TestClient,
    settings: Settings,
    roots: Path,
    tmp_path: Path,
    db_engine: Engine,
    kind: str,
) -> None:
    repo = init_repo(roots / "demo", files={"src/a.py": "x\n"})
    outside = init_repo(tmp_path / "outside")
    plain = roots / "plain"
    plain.mkdir()
    repository_id = _register(client, repo)
    tampered = {
        "outside_roots": outside,
        "subdirectory": repo / "src",
        "not_a_repo": plain,
        "roots_changed": repo,
    }[kind]
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE repositories SET canonical_local_path = :p WHERE id = :id"),
            {"p": str(tampered), "id": repository_id},
        )
    other_roots = [tmp_path / "somewhere-else"]
    other_roots[0].mkdir()
    runner = (
        _client_with(settings, repository_roots=other_roots) if kind == "roots_changed" else client
    )

    try:
        response = _import(runner, repository_id)
    finally:
        if runner is not client:
            runner.close()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"
    assert _snapshot_count(db_engine) == 0
