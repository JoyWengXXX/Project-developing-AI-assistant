"""毒藥單位框架的純邏輯（規格 5.9，D43）：狀態轉換、略過門檻、摘要與容錯。沒有資料庫。"""

from typing import Any

import pytest

from repopilot.services import job_poison
from repopilot.services.job_poison import (
    SKIP_THRESHOLD,
    PoisonTracker,
    WorkUnit,
    begin_attempt_patch,
    enter_unit_patch,
    should_skip,
    skip_unit_patch,
    summarize,
)
from repopilot.services.job_rules import assert_no_secret_keys

FILE_A = WorkUnit("file-a", "file", "parser_crash", 0, 10)
FILE_B = WorkUnit("file-b", "file", "parser_crash", 1, 10)
BATCH = WorkUnit("blobs-1", "blob_batch", "worker_crash", 2, 7)


def _merge(state: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
    """與 `update_progress` 相同的淺層合併。"""
    return {**state, **patch} if patch is not None else state


def _crash_at(state: dict[str, Any], unit: WorkUnit) -> dict[str, Any]:
    """模擬一次完整的崩潰循環：進入單位 → worker 被殺（以 worker_lost 結束）→ 新 attempt 結算。"""
    entered = _merge(state, enter_unit_patch(unit))
    return _merge(entered, begin_attempt_patch(entered, "worker_lost"))


# --- begin_attempt：結算上一個 attempt ---


def test_a_first_attempt_has_nothing_to_settle_and_writes_nothing() -> None:
    assert begin_attempt_patch({}, None) is None
    assert begin_attempt_patch({"temp_dir": "x"}, "worker_lost") is None
    assert begin_attempt_patch({"current_unit": None}, "worker_lost") is None


def test_a_worker_lost_attempt_with_a_current_unit_counts_one_crash_at_that_unit() -> None:
    state = _merge({}, enter_unit_patch(FILE_A))

    patch = begin_attempt_patch(state, "worker_lost")

    assert patch is not None
    assert patch["current_unit"] is None  # 指標已結算，不會被下一次重複計算
    assert patch["poison"]["suspect"] == {
        "unit": "file-a",
        "kind": "file",
        "crash_kind": "parser_crash",
        "crashes": 1,
    }
    assert patch["poison"]["total_crashes"] == 1


@pytest.mark.parametrize(
    "previous", [None, "git_timeout", "database_unavailable", "internal_error"]
)
def test_an_attempt_that_ended_any_other_way_is_not_a_crash(previous: str | None) -> None:
    state = _merge({}, enter_unit_patch(FILE_A))

    patch = begin_attempt_patch(state, previous)

    assert patch == {"current_unit": None}  # 只清掉過時的指標，不計崩潰
    assert summarize(_merge(state, patch)).total_crashes == 0


def test_the_same_unit_crashing_again_accumulates() -> None:
    state: dict[str, Any] = {}
    for expected in (1, 2, 3):
        state = _crash_at(state, FILE_A)
        assert summarize(state).suspect_crashes == expected
        assert summarize(state).total_crashes == expected


def test_a_crash_at_a_different_unit_restarts_the_count_because_the_old_one_survived() -> None:
    state = _crash_at({}, FILE_A)
    state = _crash_at(state, FILE_A)
    assert summarize(state).suspect_crashes == 2

    state = _crash_at(state, FILE_B)

    summary = summarize(state)
    assert summary.suspect_crashes == 1  # 換到別的單位：FILE_A 顯然撐過去了
    assert summary.total_crashes == 3  # 總崩潰次數仍累計
    assert not should_skip(state, "file-a")


def test_the_crash_kind_comes_from_the_unit_that_was_running() -> None:
    parser = _crash_at({}, FILE_A)
    worker = _crash_at({}, BATCH)

    assert parser["poison"]["suspect"]["crash_kind"] == "parser_crash"
    assert worker["poison"]["suspect"]["crash_kind"] == "worker_crash"


# --- should_skip：門檻 ---


def test_a_unit_is_skipped_only_after_it_crashed_the_worker_twice_in_a_row() -> None:
    assert SKIP_THRESHOLD == 2
    state = _crash_at({}, FILE_A)

    assert not should_skip(state, "file-a")  # 一次崩潰可能是 OOM 或容器重啟，不是單位的錯

    state = _crash_at(state, FILE_A)
    assert should_skip(state, "file-a")
    assert not should_skip(state, "file-b")  # 只影響那個單位


def test_the_threshold_is_a_parameter() -> None:
    once = _crash_at({}, FILE_A)

    assert should_skip(once, "file-a", threshold=1)
    assert not should_skip(once, "file-a", threshold=3)


def test_a_unit_without_any_record_is_never_skipped() -> None:
    assert not should_skip({}, "file-a")
    assert not should_skip({"poison": "garbage"}, "file-a")


# --- skip_unit：記錄略過 ---


def test_skipping_moves_the_suspect_to_the_skipped_list_and_keeps_the_kind() -> None:
    state = _crash_at(_crash_at({}, FILE_A), FILE_A)

    state = _merge(state, skip_unit_patch(state, "file-a"))

    assert state["poison"]["suspect"] is None
    assert state["poison"]["skipped"] == [
        {"unit": "file-a", "crash_kind": "parser_crash", "crashes": 2}
    ]
    assert should_skip(state, "file-a")  # 之後的 attempt 也繼續略過
    summary = summarize(state)
    assert (summary.skipped_units, summary.suspect_crashes, summary.total_crashes) == (1, 0, 2)


def test_a_skipped_unit_stays_skipped_even_after_a_crash_elsewhere() -> None:
    state = _crash_at(_crash_at({}, FILE_A), FILE_A)
    state = _merge(state, skip_unit_patch(state, "file-a"))

    state = _crash_at(state, FILE_B)

    assert should_skip(state, "file-a")
    assert summarize(state).skipped_units == 1
    assert summarize(state).suspect_crashes == 1


def test_skipping_twice_is_a_no_op() -> None:
    state = _crash_at(_crash_at({}, FILE_A), FILE_A)
    state = _merge(state, skip_unit_patch(state, "file-a"))

    assert skip_unit_patch(state, "file-a") is None


def test_skip_records_are_bounded() -> None:
    state: dict[str, Any] = {}
    for number in range(job_poison.MAX_SKIPPED_RECORDS + 20):
        state = _merge(state, skip_unit_patch(state, f"unit-{number}"))

    assert len(state["poison"]["skipped"]) == job_poison.MAX_SKIPPED_RECORDS
    assert not should_skip(state, "unit-0")  # 最舊的被丟棄
    assert should_skip(state, f"unit-{job_poison.MAX_SKIPPED_RECORDS + 19}")


# --- summarize：容錯與安全 ---


def test_the_summary_of_an_empty_work_state_is_all_zero() -> None:
    summary = summarize({})

    assert summary == job_poison.PoisonSummary(None, None, None, 0, 0, 0)


@pytest.mark.parametrize(
    "garbage",
    [
        {"current_unit": "x", "poison": 5},
        {
            "current_unit": {"id": 1},
            "poison": {"suspect": [], "skipped": "no", "total_crashes": "9"},
        },
        {"poison": {"suspect": {"unit": "u", "crashes": True}, "total_crashes": -3}},
        {"poison": {"suspect": {"unit": "u", "crashes": 0}}},
        {"temp_dir": "internal", "current_file_id": 7},
    ],
)
def test_a_work_state_with_an_unexpected_shape_is_treated_as_empty(garbage: dict[str, Any]) -> None:
    summary = summarize(garbage)

    assert summary == job_poison.PoisonSummary(None, None, None, 0, 0, 0)
    assert begin_attempt_patch(garbage, "worker_lost") in (None, {"current_unit": None})


def test_the_summary_reports_the_running_unit_by_kind_and_position_only() -> None:
    state = _merge({}, enter_unit_patch(BATCH))

    summary = summarize(state)

    assert (summary.current_unit_kind, summary.current_unit_index, summary.current_unit_count) == (
        "blob_batch",
        2,
        7,
    )
    assert "blobs-1" not in repr(summary)  # 單位的識別不外露


def test_patches_never_contain_secret_looking_keys() -> None:
    state = _crash_at(_crash_at({}, FILE_A), FILE_A)
    for patch in (
        enter_unit_patch(FILE_A),
        begin_attempt_patch(_merge({}, enter_unit_patch(FILE_A)), "worker_lost"),
        skip_unit_patch(state, "file-a"),
    ):
        assert_no_secret_keys(patch, where="work_state")


# --- PoisonTracker：追蹤一個 attempt ---


class _Store:
    """假的持久化：記錄每次 patch，並像 `update_progress` 一樣淺層合併後回傳整份 work_state。"""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = dict(state or {})
        self.patches: list[dict[str, Any]] = []

    def persist(self, patch: dict[str, Any]) -> dict[str, Any]:
        self.patches.append(patch)
        self.state = {**self.state, **patch}
        return self.state


def test_the_tracker_writes_nothing_when_there_is_nothing_to_settle() -> None:
    store = _Store()
    tracker = PoisonTracker(store.state, store.persist)

    tracker.begin_attempt(None)

    assert store.patches == []


def test_the_tracker_follows_a_unit_that_keeps_crashing_across_attempts() -> None:
    store = _Store()

    # attempt 1：進入 FILE_A 後被殺
    first = PoisonTracker(store.state, store.persist)
    first.begin_attempt(None)
    first.enter(FILE_A)
    assert not first.should_skip("file-a")

    # attempt 2：結算（1 次崩潰），重試 FILE_A，再次被殺
    second = PoisonTracker(store.state, store.persist)
    second.begin_attempt("worker_lost")
    assert second.summary.suspect_crashes == 1
    assert not second.should_skip("file-a")
    second.enter(FILE_A)

    # attempt 3：結算（2 次崩潰）→ 略過 FILE_A，繼續處理其他單位
    third = PoisonTracker(store.state, store.persist)
    third.begin_attempt("worker_lost")
    assert third.should_skip("file-a")
    third.skip("file-a")
    third.enter(FILE_B)

    summary = third.summary
    assert (summary.skipped_units, summary.suspect_crashes, summary.total_crashes) == (1, 0, 2)
    assert summary.current_unit_kind == "file"
    assert PoisonTracker(store.state, store.persist).should_skip("file-a")


def test_the_tracker_exposes_a_copy_of_its_state() -> None:
    store = _Store()
    tracker = PoisonTracker(store.state, store.persist)
    tracker.enter(BATCH)

    snapshot = tracker.work_state
    snapshot["poison"] = "tampered"

    assert tracker.work_state.get("poison") != "tampered"
