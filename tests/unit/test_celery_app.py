"""Celery app 設定（規格 5.13）與 `CeleryPublisher`：只帶 job_id、失敗不吞掉。

不連線、不需要 Redis。
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from repopilot.config import Settings
from repopilot.jobs.celery_app import TASK_NAME, CeleryPublisher, create_celery_app


def _settings(**values: Any) -> Settings:
    return Settings(
        _env_file=None,
        celery_broker_url=SecretStr("redis://redis.invalid:6379/0"),
        **values,
    )


def test_celery_app_follows_the_spec_settings() -> None:
    settings = _settings(
        celery_queue_name="q.test",
        celery_global_keyprefix="p-test:",
        worker_task_time_limit_seconds=2000,
    )

    conf = create_celery_app(settings).conf

    assert conf.task_acks_late is True
    # 維持預設（停用）：worker 被殺的復原由資料庫的復原掃描負責，不靠 broker 重新交付
    assert conf.task_reject_on_worker_lost in (None, False)
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_serializer == "json"
    assert list(conf.accept_content) == ["json"]
    assert conf.task_ignore_result is True
    assert conf.result_backend is None  # 不使用 result backend 作為事實來源
    assert conf.task_default_queue == "q.test"
    assert conf.task_time_limit == 2000
    assert conf.task_soft_time_limit == 1940
    options = conf.broker_transport_options
    assert options["global_keyprefix"] == "p-test:"
    # visibility timeout 必須大於任務時間上限（規格 5.13）
    assert options["visibility_timeout"] > conf.task_time_limit


def test_the_dispatcher_role_adds_a_socket_timeout_so_a_dead_broker_fails_fast() -> None:
    settings = _settings()

    assert (
        "socket_timeout"
        in create_celery_app(settings, role="dispatcher").conf.broker_transport_options
    )
    assert (
        "socket_timeout"
        not in create_celery_app(settings, role="worker").conf.broker_transport_options
    )


def test_celery_app_refuses_to_start_without_a_broker_url() -> None:
    with pytest.raises(RuntimeError, match="CELERY_BROKER_URL"):
        create_celery_app(Settings(_env_file=None))


def test_the_app_defines_no_tasks_so_a_dispatcher_never_loads_task_code() -> None:
    app = create_celery_app(_settings())

    assert [name for name in app.tasks if not name.startswith("celery.")] == []


def test_publisher_sends_only_the_job_id_by_task_name() -> None:
    app = MagicMock()
    CeleryPublisher(app, "q.test").publish(42)

    app.send_task.assert_called_once()
    args, kwargs = app.send_task.call_args
    assert args == (TASK_NAME,)
    assert kwargs["args"] == [42]  # 沒有 kwargs 內容、路徑、token
    assert kwargs["queue"] == "q.test"
    assert kwargs.get("retry") is False
    assert set(kwargs) <= {"args", "queue", "retry", "ignore_result"}
    assert "countdown" not in kwargs
    assert "eta" not in kwargs  # 退避由資料庫的 not_before 控制，不用 broker 的 ETA


def test_publisher_lets_broker_errors_propagate_so_the_event_stays_pending() -> None:
    app = MagicMock()
    app.send_task.side_effect = ConnectionError("broker down")

    with pytest.raises(ConnectionError):
        CeleryPublisher(app, "q.test").publish(1)
