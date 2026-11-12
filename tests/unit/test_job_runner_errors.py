"""worker 的錯誤分類（規格 5.7）：錯誤碼穩定、摘要固定且不含內部資訊。"""

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

from repopilot.config import Settings
from repopilot.errors import (
    DatabaseUnavailableError,
    GitCommandError,
    GitHubCloneTimeoutError,
    GitTimeoutError,
    ImportLimitExceededError,
    InvalidRepositoryPathError,
    PathNotMappedError,
    ProfileMismatchError,
)
from repopilot.services import job_rules
from repopilot.services.job_runner import GIT_TIMEOUT_SUMMARY, apply_worker_timeouts, classify_error


def test_git_timeout_is_transient_and_mentions_git_gc_and_the_worker_settings() -> None:
    code, summary = classify_error(GitTimeoutError("Git command timed out."))

    assert code == "git_timeout"
    assert job_rules.classify_failure(code) == "transient"
    assert "git gc" in summary
    assert "WORKER_GIT_TIMEOUT_SECONDS" in summary
    assert summary == GIT_TIMEOUT_SUMMARY


def test_clone_timeouts_also_map_to_git_timeout_with_the_hint() -> None:
    assert classify_error(GitHubCloneTimeoutError("x"))[0] == "git_timeout"


def test_git_failures_use_a_fixed_summary_without_stderr() -> None:
    code, summary = classify_error(GitCommandError("git ls-tree failed: fatal: /mnt/repos/secret"))

    assert code == "git_command_failed"
    assert job_rules.classify_failure(code) == "permanent"
    assert "/mnt/repos/secret" not in summary
    assert "fatal" not in summary


@pytest.mark.parametrize(
    ("exc", "code", "kind"),
    [
        (PathNotMappedError("m"), "path_not_mapped", "permanent"),
        (InvalidRepositoryPathError("m"), "invalid_repository_path", "permanent"),
        (ImportLimitExceededError("m"), "import_limit_exceeded", "permanent"),
        (ProfileMismatchError("m"), "profile_mismatch", "permanent"),
        (DatabaseUnavailableError("m"), "database_unavailable", "transient"),
    ],
)
def test_domain_errors_keep_their_code_and_classification(
    exc: Exception, code: str, kind: str
) -> None:
    assert classify_error(exc)[0] == code
    assert job_rules.classify_failure(code) == kind


@pytest.mark.parametrize(
    "exc",
    [OperationalError("SELECT 1", {}, Exception("x")), InterfaceError("s", {}, Exception("x"))],
)
def test_database_connection_errors_are_transient(exc: Exception) -> None:
    code, summary = classify_error(exc)

    assert code == "database_unavailable"
    assert job_rules.classify_failure(code) == "transient"
    assert "SELECT" not in summary


def test_unexpected_errors_are_permanent_and_only_name_the_type() -> None:
    code, summary = classify_error(RuntimeError("password=FAKE_SECRET and a path /mnt/repos/x"))

    assert code == "internal_error"
    assert job_rules.classify_failure(code) == "permanent"
    assert summary == "Unexpected RuntimeError."


def test_worker_timeouts_replace_the_api_timeouts_only_in_the_copy() -> None:
    settings = Settings(
        _env_file=None,
        git_timeout_seconds=5,
        import_git_timeout_seconds=6,
        worker_git_timeout_seconds=111,
        worker_import_git_timeout_seconds=222,
    )

    worker = apply_worker_timeouts(settings)

    assert (worker.git_timeout_seconds, worker.import_git_timeout_seconds) == (111, 222)
    assert (settings.git_timeout_seconds, settings.import_git_timeout_seconds) == (5, 6)
