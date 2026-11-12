"""背景工作的純規則：失敗分類、退避、重送間隔、階段順序、秘密鍵名檢查（無資料庫）。"""

import pytest

from repopilot.services import job_rules


@pytest.mark.parametrize(
    "code",
    [
        "database_unavailable",
        "github_unreachable",
        "github_timeout",
        "github_clone_timeout",
        "github_fetch_timeout",
        "git_timeout",
        "worker_lost",
    ],
)
def test_transient_infrastructure_errors_are_retryable(code: str) -> None:
    assert job_rules.classify_failure(code) == "transient"


@pytest.mark.parametrize(
    "code",
    [
        "invalid_repository_path",
        "invalid_revision",
        "import_limit_exceeded",
        "validation_error",
        "github_token_invalid",
        "github_repo_not_found_or_private",
        "repository_not_found",
        "managed_storage_error",
        "profile_mismatch",
        "path_not_mapped",
        "internal_error",
        "some_unknown_code",  # 未知錯誤碼一律視為永久性
    ],
)
def test_permanent_and_unknown_errors_are_not_retried(code: str) -> None:
    assert job_rules.classify_failure(code) == "permanent"


@pytest.mark.parametrize(
    ("attempts_used", "expected"),
    [(1, 30), (2, 60), (3, 120), (4, 240), (5, 480), (6, 600), (7, 600), (1000, 600)],
)
def test_backoff_doubles_and_is_capped_without_jitter(attempts_used: int, expected: float) -> None:
    # rand=0.5 → 抖動係數剛好為 1
    result = job_rules.backoff_seconds(
        attempts_used, base_seconds=30, max_seconds=600, rand=lambda: 0.5
    )
    assert result == pytest.approx(expected)


def test_backoff_jitter_is_within_twenty_percent_and_never_exceeds_cap() -> None:
    low = job_rules.backoff_seconds(2, base_seconds=30, max_seconds=600, rand=lambda: 0.0)
    high = job_rules.backoff_seconds(2, base_seconds=30, max_seconds=600, rand=lambda: 0.999999)
    assert low == pytest.approx(60 * 0.8)
    assert high == pytest.approx(60 * 1.2, rel=1e-4)
    # 已達上限時，向上的抖動也不得超過上限
    capped = job_rules.backoff_seconds(10, base_seconds=30, max_seconds=600, rand=lambda: 0.999999)
    assert capped == 600


def test_backoff_rejects_non_positive_attempt_count() -> None:
    with pytest.raises(ValueError, match="attempts_used"):
        job_rules.backoff_seconds(0, base_seconds=30, max_seconds=600)


def test_defer_seconds_range_is_five_to_fifteen() -> None:
    assert job_rules.defer_seconds(lambda: 0.0) == 5
    assert job_rules.defer_seconds(lambda: 0.5) == pytest.approx(10)
    assert job_rules.defer_seconds(lambda: 0.999999) == pytest.approx(15, rel=1e-4)


@pytest.mark.parametrize(
    ("send_count", "expected"),
    [(0, 300), (1, 300), (2, 600), (3, 1200), (4, 2400), (5, 3600), (50, 3600)],
)
def test_resend_interval_doubles_per_send_and_is_capped_at_one_hour(
    send_count: int, expected: float
) -> None:
    assert job_rules.resend_interval_seconds(300, send_count) == expected


def test_phase_order_follows_the_spec_per_kind() -> None:
    assert job_rules.phase_rank("snapshot_import", "pending") == 0
    assert job_rules.phase_rank("snapshot_import", "importing") < job_rules.phase_rank(
        "snapshot_import", "indexing"
    )
    assert job_rules.phase_rank("github_register", "cloning") < job_rules.phase_rank(
        "github_register", "registering"
    )


def test_phase_rank_rejects_a_phase_that_belongs_to_another_kind() -> None:
    with pytest.raises(ValueError, match="not valid"):
        job_rules.phase_rank("github_fetch", "cloning")
    with pytest.raises(ValueError, match="Unknown job kind"):
        job_rules.phase_rank("nope", "pending")


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "x"},
        {"github_token": "x"},
        {"Authorization": "x"},
        {"nested": {"api_key": "x"}},
        {"items": [{"password": "x"}]},
        {"client_secret": "x"},
        {"lease_token": "x"},
    ],
)
def test_secret_looking_keys_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="secret-looking"):
        job_rules.assert_no_secret_keys(payload, where="params")


def test_ordinary_keys_are_accepted() -> None:
    job_rules.assert_no_secret_keys(
        {"requested_revision": "main", "commit_sha": "a" * 40, "github_repo": "o/r", "n": [1, 2]},
        where="params",
    )


def test_summary_is_truncated_to_500_characters() -> None:
    assert job_rules.truncate_summary("x" * 900) == "x" * 500
    assert job_rules.truncate_summary(None) is None
