"""worker／dispatcher 設定（第二階段切片 3）：broker、佇列、路徑映射、worker 的 Git 逾時。"""

import json

import pytest
from pydantic import ValidationError

from repopilot.config import Settings

ENV_NAMES = [
    "CELERY_BROKER_URL",
    "CELERY_QUEUE_NAME",
    "CELERY_GLOBAL_KEYPREFIX",
    "WORKER_PATH_MAPPINGS",
    "WORKER_GIT_TIMEOUT_SECONDS",
    "WORKER_IMPORT_GIT_TIMEOUT_SECONDS",
    "WORKER_TASK_TIME_LIMIT_SECONDS",
    "GIT_POLL_SECONDS",
    "CLONE_PROGRESS_SECONDS",
    "MANAGED_TEMP_ORPHAN_SECONDS",
    "GITHUB_CLONE_TIMEOUT_SECONDS",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_defaults() -> None:
    settings = _settings()

    assert settings.celery_broker_url.get_secret_value() == ""  # 未設定：API 不需要它
    assert settings.celery_queue_name == "repopilot.jobs"
    assert settings.celery_global_keyprefix == "repopilot:"
    assert settings.worker_path_mappings == []
    assert settings.worker_git_timeout_seconds == 120
    assert settings.worker_import_git_timeout_seconds == 900
    assert settings.worker_task_time_limit_seconds == 3600


def test_the_broker_url_is_a_secret_and_never_shown_in_repr() -> None:
    settings = _settings(celery_broker_url="redis://:FAKE_PASSWORD_FOR_TEST@redis:6379/0")

    assert "FAKE_PASSWORD_FOR_TEST" not in repr(settings)
    assert "FAKE_PASSWORD_FOR_TEST" not in str(settings.celery_broker_url)
    assert "FAKE_PASSWORD_FOR_TEST" in settings.celery_broker_url.get_secret_value()


def test_path_mappings_load_from_a_json_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mappings = [
        {"host": "D:/repos", "container": "/mnt/repos", "mode": "ro"},
        {"host": r"D:\managed", "container": "/mnt/managed", "mode": "rw"},
    ]
    monkeypatch.setenv("WORKER_PATH_MAPPINGS", json.dumps(mappings))

    settings = _settings()

    assert [(m.host, m.container, m.mode) for m in settings.worker_path_mappings] == [
        ("D:/repos", "/mnt/repos", "ro"),
        (r"D:\managed", "/mnt/managed", "rw"),
    ]


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_path_mappings_mean_no_mappings(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("WORKER_PATH_MAPPINGS", blank)

    assert _settings().worker_path_mappings == []


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"host": "D:/repos"}',  # 不是陣列
        '[{"host": "relative", "container": "/mnt/repos", "mode": "ro"}]',
        '[{"host": "D:/repos", "container": "/mnt/repos", "mode": "bad"}]',
    ],
)
def test_invalid_path_mappings_are_rejected_at_startup(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("WORKER_PATH_MAPPINGS", raw)

    with pytest.raises(ValidationError):
        _settings()


def test_a_non_json_value_is_rejected_with_a_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_PATH_MAPPINGS", "FAKE_TEXT")

    with pytest.raises(ValidationError, match="must be a JSON array"):
        _settings()


@pytest.mark.parametrize(
    "values",
    [
        {"celery_queue_name": ""},
        {"celery_queue_name": "has space"},
        {"celery_queue_name": "a/b"},
        {"celery_global_keyprefix": "bad prefix"},
        {"worker_git_timeout_seconds": 0},
        {"worker_import_git_timeout_seconds": -1},
        {"worker_git_timeout_seconds": 4000},
        {"worker_task_time_limit_seconds": 60},
    ],
)
def test_invalid_values_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_the_task_time_limit_must_cover_the_worst_case_git_time() -> None:
    # 900 + 4 × 120 = 1380；時間上限不夠大時，Git 還沒逾時任務就先被殺
    with pytest.raises(ValidationError, match="WORKER_TASK_TIME_LIMIT_SECONDS"):
        _settings(worker_task_time_limit_seconds=1380)
    assert _settings(worker_task_time_limit_seconds=1381).worker_task_time_limit_seconds == 1381


def test_worker_git_timeouts_are_independent_of_the_api_git_timeouts() -> None:
    settings = _settings(
        git_timeout_seconds=7, import_git_timeout_seconds=11, worker_git_timeout_seconds=200
    )

    assert settings.git_timeout_seconds == 7
    assert settings.import_git_timeout_seconds == 11
    assert settings.worker_git_timeout_seconds == 200


# --- GitHub 背景化的設定（第二階段切片 5，D44）---


def test_github_job_defaults() -> None:
    settings = _settings()

    assert settings.git_poll_seconds == 1.0
    assert settings.clone_progress_seconds == 2.0
    assert settings.managed_temp_orphan_seconds == 7200


@pytest.mark.parametrize(
    "values",
    [
        {"git_poll_seconds": 0},
        {"git_poll_seconds": -1},
        {"git_poll_seconds": 61},
        {"clone_progress_seconds": 0},
        {"clone_progress_seconds": 601},
        {"managed_temp_orphan_seconds": 59},
    ],
)
def test_github_job_setting_ranges(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_the_orphan_threshold_must_exceed_the_clone_timeout() -> None:
    # 同步端點的 clone 沒有 job 記錄：門檻若小於 clone 逾時，進行中的 clone 會被誤判為孤兒
    with pytest.raises(ValidationError, match="MANAGED_TEMP_ORPHAN_SECONDS"):
        _settings(github_clone_timeout_seconds=300, managed_temp_orphan_seconds=300)
    assert (
        _settings(
            github_clone_timeout_seconds=300, managed_temp_orphan_seconds=301
        ).managed_temp_orphan_seconds
        == 301
    )


def test_the_task_time_limit_must_cover_the_worst_case_registration() -> None:
    # 300（clone）＋10（GitHub API）＋2×120（本機 Git）＝550
    with pytest.raises(ValidationError, match="GITHUB_CLONE_TIMEOUT_SECONDS"):
        _settings(worker_task_time_limit_seconds=1400, github_clone_timeout_seconds=1200)
    ok = _settings(worker_task_time_limit_seconds=1500, github_clone_timeout_seconds=1200)
    assert ok.github_clone_timeout_seconds == 1200
