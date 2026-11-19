"""Celery worker（第二階段切片 3，規格 5.5、5.13，D40）。

啟動：`celery -A repopilot.jobs.worker worker`。這個模組在 **Linux 容器**內由 Celery 載入
（Celery 官方不支援 Windows）；API 程序與 dispatcher 都不匯入它。任務 `repopilot.run_job`
只收 `job_id`，其餘（repo、SHA、設定雜湊）一律從資料庫讀；真正的流程在
`services/job_runner.py`，這裡只負責 Celery 與程序生命週期：

- 匯入此模組時只讀設定、建立 Celery app（不連線；沒有 I/O）。
- 資料庫 Engine 在**第一次執行任務時**於子行程內建立，以 pid 判斷（prefork 會 fork 子行程，
  Engine 不可跨 fork 共用）。
- `worker_ready`（主行程）啟動 `service_heartbeats("worker", instance_id)` 的背景心跳；
  `worker_shutdown` 停止。
"""

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from celery.signals import worker_ready, worker_shutdown
from sqlalchemy import Engine

from repopilot.config import Settings, get_settings
from repopilot.db.session import create_db_engine, create_session_factory
from repopilot.jobs.celery_app import TASK_NAME, create_celery_app
from repopilot.logging_config import APP_LOGGER_NAME, configure_logging
from repopilot.services import github_job_runner, job_runner, service_heartbeats

logger = logging.getLogger(f"{APP_LOGGER_NAME}.jobs.worker")

_settings: Settings = get_settings()
configure_logging(_settings.log_level)
app = create_celery_app(_settings, role="worker")
_worker_settings = job_runner.apply_worker_timeouts(_settings)


@dataclass
class _Runtime:
    pid: int
    engine: Engine


_runtime: _Runtime | None = None
_runtime_lock = threading.Lock()


def _get_engine() -> Engine:
    """每個行程（含 prefork 子行程）一個 Engine；fork 後 pid 不同就重建，不沿用父行程的連線。"""
    global _runtime
    with _runtime_lock:
        if _runtime is None or _runtime.pid != os.getpid():
            _runtime = _Runtime(os.getpid(), create_db_engine(_worker_settings))
        return _runtime.engine


@app.task(name=TASK_NAME, ignore_result=True)  # type: ignore[untyped-decorator]
def run_job(job_id: int) -> str:
    """執行一則 job 訊息；流程與錯誤處理見 `services/job_runner.py`。回傳值只供日誌。"""
    outcome: str = job_runner.run_job(job_id, _get_engine(), _worker_settings)
    logger.info("job_message_handled", extra={"job_id": job_id, "outcome": outcome})
    return outcome


_service_heartbeat: service_heartbeats.ServiceHeartbeatThread | None = None
_service_engine: Engine | None = None


@worker_ready.connect  # type: ignore[untyped-decorator]
def _start_service_heartbeat(**_: Any) -> None:
    global _service_heartbeat, _service_engine
    # 主行程自己的 Engine（子行程不共用）；心跳寫入失敗只記日誌，不影響 worker
    _service_engine = create_db_engine(_worker_settings)
    _service_heartbeat = service_heartbeats.ServiceHeartbeatThread(
        create_session_factory(_service_engine),
        "worker",
        service_heartbeats.default_instance_id(),
        _worker_settings,
        info={"queue": _worker_settings.celery_queue_name},
    )
    _service_heartbeat.start()
    # 規格 5.12：被 SIGKILL 的 worker 可能留下 `.tmp/<hex>`；啟動時清理逾時且沒有進行中 job 記錄者。
    # 背景執行緒（掃描目錄可能在掛載上較慢），失敗只記日誌
    threading.Thread(
        target=github_job_runner.sweep_orphans,
        args=(_service_engine, _worker_settings),
        name="temp-orphan-sweep",
        daemon=True,
    ).start()
    logger.info("worker_ready", extra={"queue": _worker_settings.celery_queue_name})


@worker_shutdown.connect  # type: ignore[untyped-decorator]
def _stop_service_heartbeat(**_: Any) -> None:
    if _service_heartbeat is not None:
        _service_heartbeat.stop()
    if _service_engine is not None:
        _service_engine.dispose()
