"""背景工作設定（規格 5.14／9.8）：預設值、環境變數載入與驗證。預設值是未實測的起點。"""

import pytest
from pydantic import ValidationError

from repopilot.config import Settings

JOB_ENV_NAMES = [
    "JOB_LEASE_SECONDS",
    "JOB_HEARTBEAT_SECONDS",
    "JOB_MAX_ATTEMPTS",
    "JOB_RETRY_BASE_SECONDS",
    "JOB_RETRY_MAX_SECONDS",
    "DISPATCH_POLL_SECONDS",
    "DISPATCH_RESEND_AFTER_SECONDS",
    "RECOVERY_SCAN_SECONDS",
    "SERVICE_HEARTBEAT_SECONDS",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in JOB_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_defaults_match_the_spec_starting_points() -> None:
    settings = _settings()

    assert settings.job_lease_seconds == 60
    assert settings.job_heartbeat_seconds == 15
    assert settings.job_max_attempts == 3
    assert settings.job_retry_base_seconds == 30
    assert settings.job_retry_max_seconds == 600
    assert settings.dispatch_poll_seconds == 1
    assert settings.dispatch_resend_after_seconds == 300
    assert settings.recovery_scan_seconds == 5
    assert settings.service_heartbeat_seconds == 15


def test_values_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_LEASE_SECONDS", "120")
    monkeypatch.setenv("JOB_HEARTBEAT_SECONDS", "20")
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("DISPATCH_POLL_SECONDS", "0.5")

    settings = _settings()

    assert (settings.job_lease_seconds, settings.job_heartbeat_seconds) == (120, 20)
    assert settings.job_max_attempts == 5
    assert settings.dispatch_poll_seconds == 0.5


@pytest.mark.parametrize(
    "values",
    [
        {"job_lease_seconds": 0},
        {"job_lease_seconds": -5},
        {"job_heartbeat_seconds": 0},
        {"job_max_attempts": 0},
        {"job_retry_base_seconds": 0},
        {"job_retry_max_seconds": 0},
        {"dispatch_poll_seconds": 0},
        {"dispatch_poll_seconds": -1},
        {"recovery_scan_seconds": 0},
        {"dispatch_resend_after_seconds": 0},
        {"service_heartbeat_seconds": 0},
    ],
)
def test_non_positive_values_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_heartbeat_must_be_shorter_than_the_lease() -> None:
    with pytest.raises(ValidationError, match="JOB_HEARTBEAT_SECONDS"):
        _settings(job_lease_seconds=30, job_heartbeat_seconds=30)


def test_retry_base_must_not_exceed_retry_max() -> None:
    with pytest.raises(ValidationError, match="JOB_RETRY_BASE_SECONDS"):
        _settings(job_retry_base_seconds=700, job_retry_max_seconds=600)
