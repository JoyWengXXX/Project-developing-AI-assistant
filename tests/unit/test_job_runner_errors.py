"""worker 的錯誤分類（規格 5.7）：錯誤碼穩定、摘要固定且不含內部資訊。"""

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import InterfaceError, OperationalError

from repopilot.config import Settings
from repopilot.errors import (
    DatabaseUnavailableError,
    GitCommandError,
    GitHubApiError,
    GitHubCloneError,
    GitHubCloneTimeoutError,
    GitHubFetchError,
    GitHubFetchTimeoutError,
    GitHubRegistrationDisabledError,
    GitHubRepoNotFoundError,
    GitHubTimeoutError,
    GitHubTokenInvalidError,
    GitHubUnreachableError,
    GitTimeoutError,
    ImportLimitExceededError,
    InvalidGitHubRepoError,
    InvalidRepositoryPathError,
    ManagedStorageError,
    PathNotMappedError,
    ProfileMismatchError,
    RepositoryNotGitHubError,
    TokenEncryptionNotConfiguredError,
)
from repopilot.services import job_rules
from repopilot.services.job_runner import (
    GIT_TIMEOUT_SUMMARY,
    GITHUB_SERVER_ERROR_SUMMARY,
    NETWORK_TIMEOUT_SUMMARY,
    TIME_LIMIT_SUMMARY,
    apply_worker_timeouts,
    classify_error,
)


def test_git_timeout_is_transient_and_mentions_git_gc_and_the_worker_settings() -> None:
    code, summary = classify_error(GitTimeoutError("Git command timed out."))

    assert code == "git_timeout"
    assert job_rules.classify_failure(code) == "transient"
    assert "git gc" in summary
    assert "WORKER_GIT_TIMEOUT_SECONDS" in summary
    assert summary == GIT_TIMEOUT_SUMMARY


def test_clone_timeouts_also_map_to_git_timeout_with_the_hint() -> None:
    assert classify_error(GitHubCloneTimeoutError("x"))[0] == "git_timeout"


def test_worker_time_limit_has_its_own_permanent_code_and_a_fixed_summary() -> None:
    code, summary = classify_error(SoftTimeLimitExceeded())

    assert code == "job_time_limit_exceeded"
    assert job_rules.classify_failure(code) == "permanent"  # 重試只會從頭再撞同一個時限
    assert summary == TIME_LIMIT_SUMMARY
    assert "WORKER_TASK_TIME_LIMIT_SECONDS" in summary


# --- GitHub 註冊與 fetch 的錯誤分類（第二階段切片 5，D44）---


@pytest.mark.parametrize("exc", [GitHubCloneTimeoutError("x"), GitHubFetchTimeoutError("x")])
def test_network_timeouts_are_transient_and_point_at_the_clone_timeout_setting(
    exc: Exception,
) -> None:
    code, summary = classify_error(exc)

    assert code == "git_timeout"
    assert job_rules.classify_failure(code) == "transient"
    assert summary == NETWORK_TIMEOUT_SUMMARY
    assert "GITHUB_CLONE_TIMEOUT_SECONDS" in summary
    assert "git gc" not in summary  # R1 的提示只適用於讀取使用者 repo


@pytest.mark.parametrize(
    ("exc", "code", "kind"),
    [
        (GitHubUnreachableError("m"), "github_unreachable", "transient"),
        (GitHubTimeoutError("m"), "github_timeout", "transient"),
        (GitHubApiError("m", status=500), "github_server_error", "transient"),
        (GitHubApiError("m", status=502), "github_server_error", "transient"),
        (GitHubApiError("m", status=503), "github_server_error", "transient"),
        (GitHubApiError("m", status=403), "github_api_error", "permanent"),
        (GitHubApiError("m", status=429), "github_api_error", "permanent"),
        (GitHubApiError("m"), "github_api_error", "permanent"),  # 非預期內容（沒有狀態碼）
        (GitHubRepoNotFoundError("m"), "github_repo_not_found_or_private", "permanent"),
        (GitHubTokenInvalidError("m"), "github_token_invalid", "permanent"),
        (InvalidGitHubRepoError("m"), "invalid_github_repo", "permanent"),
        (TokenEncryptionNotConfiguredError("m"), "token_encryption_not_configured", "permanent"),
        (GitHubCloneError("m"), "github_clone_failed", "permanent"),
        (GitHubFetchError("m"), "github_fetch_failed", "permanent"),
        (ManagedStorageError("m"), "managed_storage_error", "permanent"),
        (RepositoryNotGitHubError("m"), "repository_not_github", "permanent"),
        (GitHubRegistrationDisabledError("m"), "github_registration_disabled", "permanent"),
    ],
)
def test_github_errors_are_classified_transient_or_permanent(
    exc: Exception, code: str, kind: str
) -> None:
    assert classify_error(exc)[0] == code
    assert job_rules.classify_failure(code) == kind


def test_github_server_error_summary_is_fixed_and_hides_the_upstream_message() -> None:
    code, summary = classify_error(GitHubApiError("GitHub returned 503: SECRET-BODY", status=503))

    assert code == "github_server_error"
    assert summary == GITHUB_SERVER_ERROR_SUMMARY
    assert "SECRET-BODY" not in summary


def test_clone_and_fetch_failures_keep_their_fixed_messages_without_stderr() -> None:
    for exc in (GitHubCloneError("git clone failed (fixed)."), GitHubFetchError("fixed fetch.")):
        code, summary = classify_error(exc)
        assert summary == exc.message
        assert code == exc.code


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
