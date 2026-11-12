"""dispatcher 程序（第二階段切片 3，規格 5.3、5.9、9.3，D40）。

啟動：`python -m repopilot.jobs.dispatcher`。獨立程序，一個迴圈做三件事：

1. **派送**：`dispatch_pending` 取待派送事件（`FOR UPDATE SKIP LOCKED`，可同時跑多個實例），
   以 `CeleryPublisher`（`send_task`）發布，**broker 接受後**才標記 `sent`。只帶 `job_id`。
2. **復原掃描**（每 `RECOVERY_SCAN_SECONDS`）：租約過期的 running 工作重排或標記失敗、`sent`
   太久沒被接收的事件重送、終態工作的殘留事件清除。每小時順便清除過期的心跳列。
3. **心跳**：背景執行緒每 `SERVICE_HEARTBEAT_SECONDS` 寫
   `service_heartbeats("dispatcher", instance_id)`。

dispatcher **只載入 Celery 的發布端**（`jobs/celery_app.py`），不載入任務程式碼，也不碰
任何 repo 檔案。失敗（資料庫或 broker 不可用）不會結束程序：記日誌、指數退避（上限 30 秒）
後繼續；事件保持 `pending`，不會假裝已派送。SIGTERM／SIGINT 優雅結束：做完手上這一輪就離開。

.NET 對照：像一個 `BackgroundService`，`ExecuteAsync` 迴圈輪詢 outbox 資料表並發布到訊息匯流排。
"""

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings, get_settings
from repopilot.db.session import create_db_engine, create_session_factory
from repopilot.jobs.celery_app import CeleryPublisher, create_celery_app
from repopilot.logging_config import APP_LOGGER_NAME, configure_logging
from repopilot.services import job_dispatch, service_heartbeats

logger = logging.getLogger(f"{APP_LOGGER_NAME}.jobs.dispatcher")

MAX_BACKOFF_SECONDS = 30.0
PURGE_HEARTBEATS_EVERY_SECONDS = 3600.0


@dataclass(frozen=True)
class TickResult:
    sent: int
    failed: int
    scanned: bool
    error: bool  # 這一輪發生例外（例如資料庫中斷）


class Dispatcher:
    """一輪一輪執行的派送器；`tick()` 可直接在測試中呼叫，`run()` 是程序的主迴圈。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: job_dispatch.JobPublisher,
        settings: Settings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._settings = settings
        self._monotonic = monotonic
        self._next_scan_at = 0.0  # 第一輪就掃描：重啟後立刻處理停機期間過期的租約
        self._next_purge_at = 0.0
        self._failure_streak = 0

    def tick(self) -> TickResult:
        sent = failed = 0
        scanned = False
        try:
            with self._session_factory() as session:
                dispatched = job_dispatch.dispatch_pending(session, self._publisher)
                sent, failed = dispatched.sent, dispatched.failed
                now = self._monotonic()
                if now >= self._next_scan_at:
                    scan = job_dispatch.run_recovery_scan(session, self._settings)
                    scanned = True
                    self._next_scan_at = now + self._settings.recovery_scan_seconds
                    if scan.requeued or scan.failed or scan.cancelled or scan.resent or scan.healed:
                        logger.info(
                            "recovery_scan",
                            extra={
                                "requeued": scan.requeued,
                                "failed": scan.failed,
                                "cancelled": scan.cancelled,
                                "resent": scan.resent,
                                "healed": scan.healed,
                            },
                        )
                if now >= self._next_purge_at:
                    service_heartbeats.purge_stale_heartbeats(session)
                    self._next_purge_at = now + PURGE_HEARTBEATS_EVERY_SECONDS
        except Exception as exc:
            # 只記錄例外型別：連線字串與 SQL 參數不進日誌
            logger.error("dispatcher_tick_failed", extra={"error_type": type(exc).__name__})
            self._failure_streak += 1
            return TickResult(sent, failed, scanned, error=True)
        if sent:
            logger.info("dispatched", extra={"sent": sent, "failed": failed})
        # 全部發布失敗（broker 不可用）也算失敗一輪：退避，避免每秒對壞掉的 broker 連線
        self._failure_streak = self._failure_streak + 1 if failed and not sent else 0
        return TickResult(sent, failed, scanned, error=False)

    def next_delay(self) -> float:
        """下一輪前的等待秒數：平時為 `DISPATCH_POLL_SECONDS`，連續失敗時指數退避（上限 30 秒）。"""
        poll = float(self._settings.dispatch_poll_seconds)
        if self._failure_streak == 0:
            return poll
        return float(min(MAX_BACKOFF_SECONDS, max(poll, 1.0) * 2 ** (self._failure_streak - 1)))

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(self.next_delay())


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = create_celery_app(settings, role="dispatcher")  # broker 位址未設定時在此明確失敗
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    instance_id = service_heartbeats.default_instance_id()
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("dispatcher_stopping", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    heartbeat = service_heartbeats.ServiceHeartbeatThread(
        session_factory,
        "dispatcher",
        instance_id,
        settings,
        info={"queue": settings.celery_queue_name},
    )
    logger.info(
        "dispatcher_started",
        extra={"instance_id": instance_id, "queue": settings.celery_queue_name},
    )
    heartbeat.start()
    publisher = CeleryPublisher(app, settings.celery_queue_name)
    try:
        Dispatcher(session_factory, publisher, settings).run(stop)
    finally:
        heartbeat.stop()
        app.close()
        engine.dispose()
        logger.info("dispatcher_stopped")


if __name__ == "__main__":
    main()
