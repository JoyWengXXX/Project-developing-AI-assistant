"""blob 分批規劃（D43）與其設定：純函式，不需要 Git 或資料庫。

分批的正確性前提是「切分不遺漏、不重複、不改順序」，所以除了固定案例之外，也以大量的隨機輸入
（固定亂數種子，可重現）驗證不變量。
"""

import itertools
import random
from typing import Any

import pytest
from pydantic import ValidationError

from repopilot.config import Settings
from repopilot.git.objects import plan_blob_batches
from repopilot.services.import_profile import ImportProfile


def _ids(count: int) -> list[str]:
    return [f"{number:040x}" for number in range(count)]


def test_an_empty_input_plans_no_batch() -> None:
    assert plan_blob_batches({}, max_files=10, max_bytes=100) == []


def test_everything_fits_in_one_batch_when_below_both_limits() -> None:
    sizes = {oid: 10 for oid in _ids(5)}

    assert plan_blob_batches(sizes, max_files=5, max_bytes=50) == [sizes]


def test_the_file_count_limit_splits_batches_in_order() -> None:
    sizes = {oid: 1 for oid in _ids(7)}

    batches = plan_blob_batches(sizes, max_files=3, max_bytes=10_000)

    assert [list(batch) for batch in batches] == [_ids(7)[0:3], _ids(7)[3:6], _ids(7)[6:7]]


def test_the_byte_limit_splits_batches_and_the_limit_itself_is_allowed() -> None:
    ids = _ids(4)
    sizes = dict(zip(ids, (40, 60, 50, 50), strict=True))

    batches = plan_blob_batches(sizes, max_files=100, max_bytes=100)

    # 40+60 恰好等於上限：留在同一批；再加 50 就超過 → 換批
    assert [list(batch) for batch in batches] == [ids[0:2], ids[2:4]]


def test_a_blob_larger_than_the_byte_limit_gets_a_batch_of_its_own_and_is_not_dropped() -> None:
    ids = _ids(3)
    sizes = dict(zip(ids, (5, 1_000, 5), strict=True))

    batches = plan_blob_batches(sizes, max_files=100, max_bytes=100)

    assert [list(batch) for batch in batches] == [[ids[0]], [ids[1]], [ids[2]]]
    assert batches[1] == {ids[1]: 1_000}


def test_zero_sized_blobs_are_limited_by_the_file_count_only() -> None:
    sizes = {oid: 0 for oid in _ids(10)}

    batches = plan_blob_batches(sizes, max_files=4, max_bytes=1)

    assert [len(batch) for batch in batches] == [4, 4, 2]


def test_batches_carry_the_original_sizes() -> None:
    sizes = {oid: index * 7 for index, oid in enumerate(_ids(6))}

    batches = plan_blob_batches(sizes, max_files=2, max_bytes=10_000)

    merged: dict[str, int] = {}
    for batch in batches:
        merged.update(batch)
    assert merged == sizes


@pytest.mark.parametrize("limits", [(0, 10), (10, 0), (-1, 10), (10, -5)])
def test_non_positive_limits_are_programming_errors(limits: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        plan_blob_batches({"a" * 40: 1}, max_files=limits[0], max_bytes=limits[1])


@pytest.mark.parametrize("seed", range(40))
def test_random_inputs_keep_every_invariant(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - 固定種子的測試資料，非密碼學用途
    count = rng.randint(0, 300)
    sizes = {oid: rng.choice([0, 1, 7, 100, 4096, 50_000]) for oid in _ids(count)}
    max_files = rng.randint(1, 50)
    max_bytes = rng.choice([1, 100, 4096, 100_000, 10_000_000])

    batches = plan_blob_batches(sizes, max_files=max_files, max_bytes=max_bytes)

    flat = [object_id for batch in batches for object_id in batch]
    assert flat == list(sizes)  # 每個 ID 恰好一次，順序不變
    for batch in batches:
        assert batch  # 沒有空批
        assert len(batch) <= max_files
        total = sum(batch.values())
        assert total <= max_bytes or len(batch) == 1  # 只有「單一超大 blob」可以超過位元組上限
        assert all(sizes[oid] == size for oid, size in batch.items())
    # 貪婪切分：不會把還放得下的下一個 blob 無謂地推到下一批
    for current, following in itertools.pairwise(batches):
        first = next(iter(following.items()))
        assert len(current) >= max_files or sum(current.values()) + first[1] > max_bytes


# --- 設定 ---


def test_defaults_are_two_hundred_blobs_or_four_mebibytes() -> None:
    settings = Settings(_env_file=None)

    assert settings.import_blob_batch_max_files == 200
    assert settings.import_blob_batch_max_bytes == 4 * 1024 * 1024


def test_batch_settings_load_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPORT_BLOB_BATCH_MAX_FILES", "7")
    monkeypatch.setenv("IMPORT_BLOB_BATCH_MAX_BYTES", "12345")

    settings = Settings(_env_file=None)

    assert (settings.import_blob_batch_max_files, settings.import_blob_batch_max_bytes) == (
        7,
        12345,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"import_blob_batch_max_files": 0},
        {"import_blob_batch_max_files": -1},
        {"import_blob_batch_max_files": 100_001},
        {"import_blob_batch_max_bytes": 0},
        {"import_blob_batch_max_bytes": -5},
    ],
)
def test_invalid_batch_settings_are_rejected(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_batch_settings_do_not_change_the_import_profile_hash() -> None:
    """怎麼切批不影響匯入結果，所以不得讓既有快照因此被視為「過期設定」。"""
    default = ImportProfile.from_settings(Settings(_env_file=None)).hash()
    tuned = ImportProfile.from_settings(
        Settings(_env_file=None, import_blob_batch_max_files=1, import_blob_batch_max_bytes=1)
    ).hash()

    assert default == tuned
