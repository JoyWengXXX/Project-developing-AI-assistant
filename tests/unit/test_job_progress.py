"""檢索工作進度摘要：估計剩餘時間只在資料足夠且合理時提供。"""

from typing import Any

import pytest

from repopilot.services.job_progress import embedding_summary, saved_chunks


def state(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "vectors": 40,
        "chars_done": 1000,
        "chars_total": 4000,
        "run_base_chars": 0,
        "run_seconds": 60.0,
    }
    values.update(overrides)
    return {"embedding": values}


def test_estimate_extrapolates_the_rate_of_this_run() -> None:
    # 60 秒處理 1000 字元，剩 3000 字元 → 180 秒
    assert embedding_summary(state()) == (40, 180)


def test_estimate_excludes_characters_done_before_this_run() -> None:
    # 本次只處理了 500 字元（其餘是先前已完成），速率以本次為準：剩 3000 字元 → 360 秒
    assert embedding_summary(state(run_base_chars=500)) == (40, 360)


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_seconds": 29.9},  # 太早，估計不準
        {"chars_done": 0},  # 還沒處理任何字元
        {"run_base_chars": 1000},  # 本次尚未處理
        {"chars_total": 500},  # 已完成多於總量：資料不合理
        {"run_seconds": -1},
        {"chars_done": True},
        {"chars_total": "4000"},
        {"run_seconds": None},
    ],
)
def test_estimate_is_missing_when_data_is_insufficient_or_invalid(
    overrides: dict[str, Any],
) -> None:
    assert embedding_summary(state(**overrides))[1] is None


def test_vectors_are_reported_even_without_an_estimate() -> None:
    assert embedding_summary(state(run_seconds=1.0)) == (40, None)


@pytest.mark.parametrize("work_state", [{}, {"embedding": "x"}, {"embedding": None}])
def test_missing_or_malformed_state_yields_nothing(work_state: dict[str, Any]) -> None:
    assert embedding_summary(work_state) == (None, None)


def test_saved_chunks_report_progress_kept_across_slices() -> None:
    assert saved_chunks({"embedding": {"chunks_done": 3, "chunks_total": 9}}) == (3, 9)


@pytest.mark.parametrize(
    "work_state",
    [
        {},
        {"embedding": {"chunks_done": 3}},
        {"embedding": {"chunks_done": 10, "chunks_total": 9}},
        {"embedding": {"chunks_done": True, "chunks_total": 9}},
        {"embedding": {"chunks_done": -1, "chunks_total": 9}},
    ],
)
def test_saved_chunks_ignore_missing_or_invalid_state(work_state: dict[str, Any]) -> None:
    assert saved_chunks(work_state) is None
