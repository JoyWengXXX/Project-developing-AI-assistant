"""blob 分批讀取（第二階段切片 4，D43）：結果必須與一次讀完逐位元相同，且進度與記憶體行為符合設計。

「與一次讀完相同」的證據有三層：①不同批次形狀（每批 1 個、2 個、位元組限制…）的結果與單一批次
逐欄位相等；②每個結果都與 Git 物件本身逐位元核對（獨立於分批實作）；③同一個 repo 經 API 匯入
（批次極小 vs 預設）後資料表內容相同。其餘測試驗證：每個 blob 恰好讀一次、讀取前先依大小判斷、
所有批次共用同一個期限、批次之間回報進度且總數先於任何讀取已知、已讀完的批次不會留在記憶體。

直接呼叫 `_build_records`（私有，與既有的失敗注入測試同樣的作法）是因為它是切分發生的地方，
不需要資料庫；與 API／資料庫有關的測試使用真實 PostgreSQL。
"""

import hashlib
import subprocess
import weakref
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.errors import GitCommandError, GitTimeoutError, ImportLimitExceededError
from repopilot.git.objects import TreeEntry
from repopilot.git.objects import list_tree as git_list_tree
from repopilot.main import create_app
from repopilot.services import snapshots as _snapshots_module
from repopilot.services.import_profile import ImportProfile
from repopilot.services.snapshots import import_snapshot
from tests.git_helpers import commit_files, commit_special_entries, git, init_repo

# 以 Any 取用：測試要替換 `read_blobs`／`list_tree`／`time`（mypy 不認為它們是匯出屬性）
snapshot_service: Any = _snapshots_module

BIG = 10**9
MAX_FILE = 1_000

TEXT_FILES = {f"src/f{number:03d}.txt": f"file {number}\n" for number in range(30)}
SPECIAL_BYTES = {
    "data/bin.dat": b"\x00\x01\xff\n",
    "data/bad_utf8.txt": b"\xff\xfe\xfd",
    "data/crlf.txt": b"a\r\nb\r\n",
    "data/empty.txt": b"",
    "data/dup-a.txt": b"same blob\n",
    "data/dup-b.txt": b"same blob\n",  # 與 dup-a 是同一個 blob：只讀一次
    "data/big.txt": b"x" * (MAX_FILE + 1),  # 超過單檔上限：不得讀取
    ".env": b"SECRET=FAKE_VALUE_FOR_TEST\n",  # 排除：不得讀取
}


@pytest.fixture
def repo(roots: Path) -> Path:
    path = init_repo(roots / "mixed", files=TEXT_FILES)
    commit_files(path, SPECIAL_BYTES)
    commit_special_entries(
        path, symlinks={"link": "src/f000.txt"}, gitlinks={"vendor/lib": "a" * 40}
    )
    return path


@pytest.fixture
def sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def _with(settings: Settings, **overrides: Any) -> Settings:
    return settings.model_copy(update={"import_max_file_bytes": MAX_FILE, **overrides})


def _build(
    repo: Path, sha: str, settings: Settings, **callbacks: Any
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    profile = ImportProfile.from_settings(settings)
    records, stats = snapshot_service._build_records(repo, sha, profile, settings, **callbacks)
    return records, stats


def _blob(repo: Path, object_id: str) -> bytes:
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "cat-file", "blob", object_id],  # noqa: S607
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout


class _Spy:
    """包住 `read_blobs`：記錄每次呼叫的 (object ID 清單, timeout)，其餘行為不變。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.sizes: list[dict[str, int]] = []
        self.timeouts: list[float] = []
        self.real = snapshot_service.read_blobs
        monkeypatch.setattr(snapshot_service, "read_blobs", self)

    def __call__(self, repo: Path, expected_sizes: dict[str, int], *, timeout: float) -> Any:
        self.calls.append(list(expected_sizes))
        self.sizes.append(dict(expected_sizes))
        self.timeouts.append(timeout)
        return self.real(repo, expected_sizes, timeout=timeout)


# --- ①：任何批次形狀的結果都與單一批次相同 ---

BATCH_SHAPES = [
    (1, BIG),
    (2, BIG),
    (3, BIG),
    (7, BIG),
    (BIG, 1),
    (BIG, 10),
    (BIG, 100),
    (5, 25),
    (200, 4 * 1024 * 1024),  # 預設值
    (100_000, BIG),  # 單一批次（等同切片 3 之前的行為）
]


@pytest.mark.parametrize("shape", BATCH_SHAPES, ids=[f"{f}files-{b}bytes" for f, b in BATCH_SHAPES])
def test_every_batch_shape_yields_the_same_records_as_a_single_batch(
    settings: Settings, repo: Path, sha: str, shape: tuple[int, int]
) -> None:
    baseline = _build(
        repo,
        sha,
        _with(settings, import_blob_batch_max_files=100_000, import_blob_batch_max_bytes=BIG),
    )

    batched = _build(
        repo,
        sha,
        _with(settings, import_blob_batch_max_files=shape[0], import_blob_batch_max_bytes=shape[1]),
    )

    assert batched == baseline  # 順序、每個欄位（內容、SHA-256、狀態、跳過原因）、統計都相同


# --- ②：與 Git 物件逐位元核對（獨立於分批實作） ---


def test_the_records_match_the_git_objects_byte_for_byte_even_with_one_blob_per_batch(
    settings: Settings, repo: Path, sha: str
) -> None:
    records, stats = _build(
        repo,
        sha,
        _with(settings, import_blob_batch_max_files=1, import_blob_batch_max_bytes=BIG),
    )

    by_path = {record["path"]: record for record in records}
    assert len(by_path) == len(records)
    assert [r["path"] for r in records] == sorted(by_path)  # 仍依路徑排序
    for path, record in by_path.items():
        if record["status"] == "imported" or record["skip_reason"] in ("binary", "invalid_utf8"):
            data = _blob(repo, record["git_blob_id"])
            assert record["sha256"] == hashlib.sha256(data).hexdigest(), path
            assert record["byte_size"] == len(data), path
            if record["status"] == "imported":
                assert record["content"] == data.decode("utf-8"), path
            else:
                assert record["content"] is None, path
    # 分類結果（與第一階段的規則相同）
    assert by_path["data/bin.dat"]["skip_reason"] == "binary"
    assert by_path["data/bad_utf8.txt"]["skip_reason"] == "invalid_utf8"
    assert by_path["data/crlf.txt"]["content"] == "a\r\nb\r\n"  # CRLF 原樣保留
    assert by_path["data/empty.txt"]["status"] == "imported"
    assert by_path["data/big.txt"]["skip_reason"] == "too_large"
    assert by_path[".env"]["skip_reason"] == "excluded"
    assert by_path["link"]["skip_reason"] == "symlink"
    assert by_path["vendor/lib"]["skip_reason"] == "submodule"
    assert by_path["data/dup-a.txt"]["sha256"] == by_path["data/dup-b.txt"]["sha256"]
    assert stats["file_count"] == sum(1 for r in records if r["status"] == "imported")
    assert stats["skipped_count"] == len(records) - stats["file_count"]


# --- 每個 blob 恰好讀一次、上限先於讀取 ---


def test_each_batch_respects_the_limits_and_each_needed_blob_is_read_exactly_once(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(monkeypatch)
    max_files, max_bytes = 4, 40

    records, _ = _build(
        repo,
        sha,
        _with(
            settings, import_blob_batch_max_files=max_files, import_blob_batch_max_bytes=max_bytes
        ),
    )

    wanted = {
        r["git_blob_id"]
        for r in records
        if r["sha256"] is not None  # 讀過內容的才有 SHA-256
    }
    read = [object_id for call in spy.calls for object_id in call]
    assert sorted(read) == sorted(wanted)  # 恰好一次：沒有遺漏、沒有重複（dup-a／dup-b 只讀一次）
    assert len(spy.calls) > 5  # 確實切成多批
    for sizes in spy.sizes:
        assert len(sizes) <= max_files
        assert sum(sizes.values()) <= max_bytes or len(sizes) == 1


def test_blobs_that_are_excluded_or_too_large_are_never_read_in_batch_mode(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(monkeypatch)
    entries = {e.path: e for e in git_list_tree(repo, sha, timeout=30)}

    _build(repo, sha, _with(settings, import_blob_batch_max_files=1))

    read = {object_id for call in spy.calls for object_id in call}
    assert entries["data/big.txt"].object_id not in read  # 讀 blob 前先依大小判斷
    assert entries[".env"].object_id not in read  # 秘密檔不讀內容
    assert entries["link"].object_id not in read


def test_exceeding_the_total_size_limit_reads_no_blob_at_all_in_batch_mode(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(monkeypatch)

    with pytest.raises(ImportLimitExceededError):
        _build(
            repo, sha, _with(settings, import_blob_batch_max_files=1, import_max_snapshot_bytes=8)
        )

    assert spy.calls == []


# --- 驗證：大小不一致（每一批都檢查） ---


def test_a_size_mismatch_in_a_late_batch_fails_the_whole_import_without_partial_data(
    settings: Settings,
    repo: Path,
    sha: str,
    db_engine: Engine,
    clean_repositories: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small = _with(settings, import_blob_batch_max_files=3)
    with TestClient(create_app(small)) as client:
        repository_id = client.post("/api/v1/repositories", json={"path": str(repo)}).json()["id"]

    def last_entry_lies_about_its_size(*args: Any, **kwargs: Any) -> list[TreeEntry]:
        entries = git_list_tree(*args, **kwargs)
        readable = [e for e in entries if e.path.startswith("src/")]
        liar = max(readable, key=lambda e: e.path)  # 排序後最後一批才會讀到它
        return [
            TreeEntry(e.mode, e.object_type, e.object_id, (e.size or 0) + 1, e.path)
            if e is liar
            else e
            for e in entries
        ]

    spy = _Spy(monkeypatch)
    monkeypatch.setattr(snapshot_service, "list_tree", last_entry_lies_about_its_size)
    with Session(db_engine) as session, pytest.raises(GitCommandError, match="unexpected size"):
        import_snapshot(session, repository_id, "HEAD", small)

    assert len(spy.calls) >= 2  # 前面的批次讀成功了，失敗發生在後面的批次
    with db_engine.connect() as connection:
        status = connection.execute(text("SELECT status FROM snapshots")).scalar_one()
        files = connection.execute(text("SELECT count(*) FROM snapshot_files")).scalar_one()
    assert (status, files) == ("failed", 0)  # 沒有半套資料


# --- 進度、批次回呼、期限 ---


def test_the_total_is_reported_before_any_blob_is_read_and_progress_grows_with_each_batch(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, Any]] = []
    spy = _Spy(monkeypatch)
    real = spy.real

    def traced(repo_path: Path, expected: dict[str, int], *, timeout: float) -> Any:
        events.append(("read", len(expected)))
        return real(repo_path, expected, timeout=timeout)

    spy.real = traced

    records, _ = _build(
        repo,
        sha,
        _with(settings, import_blob_batch_max_files=4),
        on_progress=lambda processed, total: events.append(("progress", (processed, total))),
    )

    total = len(records)
    progress = [value for kind, value in events if kind == "progress"]
    assert events[0] == ("progress", (0, total))  # 讀任何 blob 之前就知道總數
    first_read = next(i for i, (kind, _) in enumerate(events) if kind == "read")
    classified = events[1]
    assert classified[0] == "progress" and 0 < classified[1][0] < total
    assert first_read == 2  # 順序：總數 → 不需讀取者分類完成 → 第一批
    processed = [p for p, _ in progress]
    assert processed == sorted(processed) and len(set(processed)) == len(processed)  # 嚴格遞增
    assert progress[-1] == (total, total)
    batches = len(spy.calls)
    assert batches >= 5
    assert len(progress) == 2 + batches  # 2 個起始點＋每批一次
    # 每一批讀完後，進度確實反映了「到目前為止處理了多少檔案」
    reads_before_each_progress = [kind for kind, _ in events[2:]]
    assert reads_before_each_progress == ["read", "progress"] * batches


def test_on_batch_runs_before_each_read_with_a_stable_fingerprint(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    spy = _Spy(monkeypatch)
    real = spy.real

    def traced(repo_path: Path, expected: dict[str, int], *, timeout: float) -> Any:
        order.append("read")
        return real(repo_path, expected, timeout=timeout)

    spy.real = traced
    batches_a: list[tuple[str, int, int]] = []
    batches_b: list[tuple[str, int, int]] = []
    options = _with(settings, import_blob_batch_max_files=5)

    def record_a(batch_id: str, number: int, count: int) -> None:
        order.append("batch")
        batches_a.append((batch_id, number, count))

    def record_b(batch_id: str, number: int, count: int) -> None:
        batches_b.append((batch_id, number, count))

    _build(repo, sha, options, on_batch=record_a)
    _build(repo, sha, options, on_batch=record_b)

    count = len(batches_a)
    assert count >= 4
    assert order[: 2 * count] == ["batch", "read"] * count  # 每次讀取前先回報單位
    assert [number for _, number, _ in batches_a] == list(range(count))
    assert {total for _, _, total in batches_a} == {count}
    assert batches_a == batches_b  # 同樣的輸入每次都得到相同的指紋（跨 attempt 比對的前提）
    assert len({batch_id for batch_id, _, _ in batches_a}) == count  # 不同批次指紋不同


def test_a_callback_that_raises_stops_before_the_next_blob_is_read(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(monkeypatch)

    class StopImportError(Exception):
        pass

    seen: list[int] = []

    def stop_after_the_second_batch(processed: int, total: int | None) -> None:
        seen.append(processed)
        if len(spy.calls) == 2:
            raise StopImportError

    with pytest.raises(StopImportError):
        _build(
            repo,
            sha,
            _with(settings, import_blob_batch_max_files=2),
            on_progress=stop_after_the_second_batch,
        )

    assert len(spy.calls) == 2  # 回呼拋例外後，不再讀取任何 blob


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def test_all_batches_share_one_deadline_and_an_exhausted_one_stops_the_import(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(snapshot_service, "time", clock)
    spy = _Spy(monkeypatch)
    real = spy.real

    def slow_batches(repo_path: Path, expected: dict[str, int], *, timeout: float) -> Any:
        result = real(repo_path, expected, timeout=timeout)
        clock.now += 6.0  # 每一批「花了」6 秒
        return result

    spy.real = slow_batches

    with pytest.raises(GitTimeoutError):
        _build(
            repo,
            sha,
            _with(settings, import_blob_batch_max_files=1, import_git_timeout_seconds=10),
        )

    # 期限是 10 秒、每批 6 秒：第 1 批（剩 10）、第 2 批（剩 4）可以進行，第 3 批前期限已過
    assert spy.timeouts == [10.0, 4.0]
    assert len(spy.calls) == 2


def test_the_per_batch_timeout_never_exceeds_the_configured_total(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(monkeypatch)

    _build(
        repo, sha, _with(settings, import_blob_batch_max_files=3, import_git_timeout_seconds=500)
    )

    assert len(spy.timeouts) >= 3
    assert all(0 < timeout <= 500 for timeout in spy.timeouts)
    assert spy.timeouts == sorted(spy.timeouts, reverse=True)  # 剩餘時間只會減少


# --- 記憶體：已讀完的批次不留在記憶體 ---


class _TrackedBlobs(dict[str, bytes]):
    """可被 weakref 追蹤的 dict（內建 dict 不能建立弱參照）。"""


def test_the_raw_bytes_of_a_finished_batch_are_released_before_the_next_batch_is_read(
    settings: Settings, repo: Path, sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = snapshot_service.read_blobs
    previous: list[weakref.ref[_TrackedBlobs]] = []
    alive_when_next_batch_starts: list[bool] = []

    def tracking_read(repo_path: Path, expected: dict[str, int], *, timeout: float) -> Any:
        if previous:
            alive_when_next_batch_starts.append(previous[-1]() is not None)
        blobs = _TrackedBlobs(real(repo_path, expected, timeout=timeout))
        previous.append(weakref.ref(blobs))
        return blobs

    monkeypatch.setattr(snapshot_service, "read_blobs", tracking_read)

    _build(repo, sha, _with(settings, import_blob_batch_max_files=2))

    assert len(previous) >= 5
    # 下一批開始時，上一批的位元組已被釋放：同時存活的 blob 位元組不超過一批
    assert alive_when_next_batch_starts and not any(alive_when_next_batch_starts)


# --- ③：經 API 匯入（真實 PostgreSQL），批次極小與預設的資料表內容相同 ---


def _rows(engine: Engine, snapshot_id: int) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        result = connection.execute(
            text(
                "SELECT path, git_blob_id, sha256, byte_size, content, status, skip_reason "
                'FROM snapshot_files WHERE snapshot_id = :i ORDER BY path COLLATE "C"'
            ),
            {"i": snapshot_id},
        )
        return [tuple(row) for row in result]


def test_an_import_through_the_api_stores_the_same_rows_with_tiny_batches_and_with_the_default(
    settings: Settings, roots: Path, repo: Path, db_engine: Engine, clean_repositories: None
) -> None:
    twin = init_repo(roots / "twin", files=TEXT_FILES)
    commit_files(twin, SPECIAL_BYTES)
    commit_special_entries(
        twin, symlinks={"link": "src/f000.txt"}, gitlinks={"vendor/lib": "a" * 40}
    )
    tiny = _with(settings, import_blob_batch_max_files=1, import_blob_batch_max_bytes=1)
    default = _with(settings)

    with TestClient(create_app(tiny)) as tiny_client:
        repo_id = tiny_client.post("/api/v1/repositories", json={"path": str(repo)}).json()["id"]
        first = tiny_client.post(f"/api/v1/repositories/{repo_id}/imports", json={})
    with TestClient(create_app(default)) as default_client:
        twin_id = default_client.post("/api/v1/repositories", json={"path": str(twin)}).json()["id"]
        second = default_client.post(f"/api/v1/repositories/{twin_id}/imports", json={})

    assert (first.status_code, second.status_code) == (201, 201)
    a, b = first.json(), second.json()
    assert (a["file_count"], a["skipped_count"], a["total_bytes"]) == (
        b["file_count"],
        b["skipped_count"],
        b["total_bytes"],
    )
    rows = _rows(db_engine, a["id"])
    assert rows == _rows(db_engine, b["id"])  # 兩個 repo 內容相同 → 每一列（含 SHA-256）完全相同
    assert len(rows) == len(TEXT_FILES) + len(SPECIAL_BYTES) + 2
