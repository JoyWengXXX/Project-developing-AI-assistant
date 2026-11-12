"""dispatcher／worker 的存活心跳（C8，D39）：寫入、清理，以及「有沒有存活的服務」的判斷。

介面用它區分「工作在排隊」與「根本沒有 worker／dispatcher 在線」。API 不連 Redis，
只讀這張表（由 dispatcher／worker 自己定期寫入，切片 3 起）。超過 `SERVICE_HEARTBEAT_SECONDS`
的 3 倍沒有更新視為離線。時間預設為資料庫時間，可注入。
"""

import logging
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import CursorResult, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.db.models import Job, ServiceHeartbeat
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import job_rules
from repopilot.services.jobs import db_now

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.service_heartbeats")

SERVICES = ("dispatcher", "worker")
# 超過此倍數的 SERVICE_HEARTBEAT_SECONDS 沒更新就視為離線（規格 5.14）
OFFLINE_AFTER_HEARTBEATS = 3
# 超過一天沒更新的列由 dispatcher 清除（規格 6.1）
PURGE_AFTER_SECONDS = 24 * 3600

ServiceLiveness = Literal["online", "stale", "absent"]


@dataclass(frozen=True)
class ServiceState:
    # online：至少一個實例的心跳未過期；stale：有紀錄但全部過期；absent：沒有任何紀錄
    status: ServiceLiveness
    last_seen_at: datetime | None
    instances_online: int


@dataclass(frozen=True)
class JobsStatus:
    dispatcher: ServiceState
    worker: ServiceState
    queued: int
    running: int
    heartbeat_timeout_seconds: int


def record_heartbeat(
    session: Session,
    service: str,
    instance_id: str,
    *,
    info: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """寫入（或更新）某個實例的心跳：單一 upsert。`info` 只放非秘密資訊（解析器版本等）。"""
    if service not in SERVICES:
        raise ValueError(f"Unknown service: {service!r}.")
    if not 1 <= len(instance_id) <= 100:
        raise ValueError("instance_id must be 1 to 100 characters.")
    info = info or {}
    job_rules.assert_no_secret_keys(info, where="info")
    now_expr = db_now(now)
    statement = pg_insert(ServiceHeartbeat).values(
        service=service, instance_id=instance_id, last_seen_at=now_expr, info=info
    )
    try:
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["service", "instance_id"],
                set_={
                    "last_seen_at": statement.excluded.last_seen_at,
                    "info": statement.excluded.info,
                },
            )
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise


def purge_stale_heartbeats(session: Session, *, now: datetime | None = None) -> int:
    """刪除超過一天沒更新的心跳列（已不存在的容器實例）。"""
    cutoff = db_now(now) - timedelta(seconds=PURGE_AFTER_SECONDS)
    try:
        result = session.execute(
            delete(ServiceHeartbeat).where(ServiceHeartbeat.last_seen_at < cutoff)
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return int(cast(CursorResult[Any], result).rowcount)


def get_jobs_status(
    session: Session, settings: Settings, *, now: datetime | None = None
) -> JobsStatus:
    """`GET /jobs/status` 的資料：兩種服務的存活狀態、queued 與 running 的工作數。唯讀。"""
    timeout = OFFLINE_AFTER_HEARTBEATS * settings.service_heartbeat_seconds
    alive_since = db_now(now) - timedelta(seconds=timeout)
    online_count = func.sum(case((ServiceHeartbeat.last_seen_at >= alive_since, 1), else_=0))
    rows = session.execute(
        select(
            ServiceHeartbeat.service, func.max(ServiceHeartbeat.last_seen_at), online_count
        ).group_by(ServiceHeartbeat.service)
    ).all()
    counts = {
        status: count
        for status, count in session.execute(
            select(Job.status, func.count())
            .where(Job.status.in_(job_rules.ACTIVE_STATUSES))
            .group_by(Job.status)
        )
    }
    session.rollback()  # 唯讀：結束交易

    states: dict[str, ServiceState] = {
        service: ServiceState("absent", None, 0) for service in SERVICES
    }
    for service, last_seen_at, online in rows:
        online_instances = int(online or 0)
        states[service] = ServiceState(
            "online" if online_instances > 0 else "stale", last_seen_at, online_instances
        )
    return JobsStatus(
        dispatcher=states["dispatcher"],
        worker=states["worker"],
        queued=int(counts.get("queued", 0)),
        running=int(counts.get("running", 0)),
        heartbeat_timeout_seconds=timeout,
    )


def default_instance_id() -> str:
    """服務實例的識別：主機名稱（容器內是容器 ID 的前 12 碼，`docker restart` 後不變）。"""
    return socket.gethostname()[:100] or "unknown"


def is_instance_alive(
    session: Session,
    service: str,
    instance_id: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    """某個實例的心跳是否仍新鮮（容器 healthcheck 用，`jobs/healthcheck.py`）。"""
    timeout = OFFLINE_AFTER_HEARTBEATS * settings.service_heartbeat_seconds
    seen = session.scalar(
        select(ServiceHeartbeat.last_seen_at).where(
            ServiceHeartbeat.service == service,
            ServiceHeartbeat.instance_id == instance_id,
            ServiceHeartbeat.last_seen_at >= db_now(now) - timedelta(seconds=timeout),
        )
    )
    session.rollback()
    return seen is not None


class ServiceHeartbeatThread:
    """背景執行緒：啟動時立即、之後每 `SERVICE_HEARTBEAT_SECONDS` 寫一次心跳（C8）。

    用自己的資料庫連線（不占用主流程的 Session）；寫入失敗只記日誌、下一輪再試（資料庫短暫
    中斷不該讓 dispatcher／worker 結束）。`info` 只放非秘密的小資訊。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        service: str,
        instance_id: str,
        settings: Settings,
        *,
        info: dict[str, Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._service = service
        self._instance_id = instance_id
        self._interval = float(settings.service_heartbeat_seconds)
        self._info = info or {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"{service}-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            try:
                with self._session_factory() as session:
                    record_heartbeat(session, self._service, self._instance_id, info=self._info)
            except Exception as exc:
                logger.warning(
                    "service_heartbeat_failed",
                    extra={"service": self._service, "error_type": type(exc).__name__},
                )
            if self._stop.wait(self._interval):
                return
