"""跨程序互斥：PostgreSQL advisory lock（第二階段切片 3，規格 5.10，D40）。

為什麼不用行程內的 `threading.Lock`（D32）：API（Windows 主機）與 worker（Linux 容器）是不同
程序，鎖只在各自記憶體裡就互相看不到。advisory lock 存在資料庫伺服器端，所有程序共用。

設計：
- **session 層級**（`pg_try_advisory_lock`）：鎖綁在一條**專用連線**上，持有期間這條連線不做
  別的事；連線結束（正常釋放、程序崩潰、網路中斷）時由伺服器自動釋放，所以不會有過期鎖。
- **只用 `try` 版本**（不等待）：取不到就回報 `None`，由呼叫端決定（API 回 409、worker 延後）。
- 鍵是 SHA-256 前 8 位元組的有號 bigint，並帶命名空間前綴；碰撞只會造成多餘的序列化，
  不會造成錯誤。
- 專用連線**不放回連線池**：結束時一律 `invalidate()`（實際關閉連線），避免鎖留在池裡的
  連線上。代價是每次取鎖多一次連線建立（毫秒級），相對匯入的 Git 工作可忽略。
- 持鎖期間連線若被中斷，鎖會**默默消失**：發布結果前呼叫 `assert_alive()`，確認連線還在
  才算仍持有鎖。

鍵空間（`namespace:` 前綴避免互相碰撞）：
- `repo:local:{repository_id}`、`repo:github:{lower(owner)}/{lower(name)}`：repo 級。worker 的
  匯入取**共享**鎖，移除 repo（D33）取**獨占**鎖：有匯入在跑就取不到。第二階段切片 5（D44）起，
  GitHub 的 clone／註冊／fetch（worker 與同步端點）也取**獨占**鎖，所以註冊階段（資料列還不存在）
  以 owner／name 為鍵，與已註冊 repo 的 `repository_key()` 是同一把鎖；D32 的行程內鎖
  （`threading.Lock`）已被取代——每個持有者各用一條專用連線，同一程序內的多個執行緒也互相排斥。
- `snapshot:{repository_id}:{commit_sha}:{import_profile_hash}`：快照級，獨占。同步
  `POST /imports` 與 worker 的匯入 job 都先取它，所以同一快照不會被兩邊同時匯入；持有它的人
  可以**立即接手**殘留的 `importing` 快照（`takeover`，規格 5.4）——因為活著的匯入者一定
  持有這把鎖。
- `index:{snapshot_id}:{index_profile_hash}`：索引級，獨占（切片 6）。建構同一份結構索引的
  job 互斥；持有者可以立即接手殘留的 `building` 索引。

.NET 對照：像 SQL Server 的 `sp_getapplock`（Session 範圍），但這裡用 `try` 語意、不阻塞。
"""

import hashlib
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from repopilot.errors import DatabaseUnavailableError
from repopilot.logging_config import APP_LOGGER_NAME

logger = logging.getLogger(f"{APP_LOGGER_NAME}.db.locks")


class LockBusyError(Exception):
    """repo 鎖或快照鎖忙碌：worker 延後重排、不消耗重試額度（不是 `RepoPilotError`）。"""


def lock_key(namespace: str, name: str) -> int:
    """`namespace:name` 的 SHA-256 前 8 位元組，轉成 PostgreSQL bigint（有號）。"""
    digest = hashlib.sha256(f"{namespace}:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def repo_lock_key(repo_key: str) -> int:
    """repo 級鎖的鍵；`repo_key` 為 `local:{id}` 或 `github:{lower(owner)}/{lower(name)}`。

    GitHub 來源以 owner／name 為鍵（不是 id）：註冊階段資料列尚不存在，clone 就要取鎖。
    """
    return lock_key("repo", repo_key)


def snapshot_lock_key(repository_id: int, commit_sha: str, import_profile_hash: str) -> int:
    return lock_key("snapshot", f"{repository_id}:{commit_sha}:{import_profile_hash}")


def index_lock_key(snapshot_id: int, index_profile_hash: str) -> int:
    """索引級鎖（切片 6）：同一快照在同一索引設定下只會有一個建構者。

    匯入 job（`import:…`）與 `index-jobs`（`index:…`）的 `dedupe_key` 不同，兩個 job 可能同時
    想建立同一份索引；持有這把鎖的人才可以接手殘留的 `building` 索引。
    """
    return lock_key("index", f"{snapshot_id}:{index_profile_hash}")


class AdvisoryLock:
    """已取得的 advisory lock（持有一條專用連線）。用 `acquire_advisory_lock` 取得。"""

    def __init__(self, connection: Connection, key: int, *, shared: bool) -> None:
        self._connection: Connection | None = connection
        self.key = key
        self.shared = shared

    def assert_alive(self) -> None:
        """確認鎖連線仍存活。

        連線已斷代表伺服器已釋放鎖，拋 `DatabaseUnavailableError`（暫時性，job 會重試）。
        """
        if self._connection is None:
            raise DatabaseUnavailableError("The advisory lock was already released.")
        try:
            self._connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            # DBAPIError：連線剛被伺服器關閉；PendingRollbackError：先前已偵測到失敗、連線已失效
            logger.error("advisory_lock_connection_lost")
            raise DatabaseUnavailableError(
                "The database connection holding the repository lock was lost."
            ) from None

    def release(self) -> None:
        """解鎖並關閉連線；解鎖失敗時照樣關閉連線（伺服器會因連線結束而釋放鎖）。"""
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            function = "pg_advisory_unlock_shared" if self.shared else "pg_advisory_unlock"
            connection.execute(text(f"SELECT {function}(CAST(:key AS bigint))"), {"key": self.key})
        except SQLAlchemyError:
            logger.warning("advisory_unlock_failed")
        finally:
            try:
                connection.invalidate()
            except Exception:
                logger.warning("advisory_lock_invalidate_failed")
            connection.close()


def engine_of(session: Session) -> Engine:
    bind = session.get_bind()
    return bind if isinstance(bind, Engine) else bind.engine


def acquire_advisory_lock(engine: Engine, key: int, *, shared: bool = False) -> AdvisoryLock | None:
    """嘗試取得鎖；取不到回傳 `None`。資料庫連不上拋 `DatabaseUnavailableError`。"""
    try:
        connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    except DBAPIError:
        raise DatabaseUnavailableError("Could not connect to the database for locking.") from None
    try:
        function = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
        granted = connection.execute(
            text(f"SELECT {function}(CAST(:key AS bigint))"), {"key": key}
        ).scalar()
    except BaseException:
        connection.invalidate()
        connection.close()
        raise
    if not granted:
        connection.close()  # 沒有持有任何鎖，連線可以安全放回連線池
        return None
    return AdvisoryLock(connection, key, shared=shared)


def acquire_advisory_lock_wait(
    engine: Engine,
    key: int,
    *,
    timeout: float,
    interval: float = 0.2,
    shared: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AdvisoryLock | None:
    """反覆 `try` 直到取得或逾時（回傳 `None`）；仍然只用 `try` 版本，不在資料庫端阻塞。

    給同步端點（D32 的註冊、fetch 原本就是「等前一個做完」）使用：等待期間不持有任何連線（每次嘗試
    失敗就把連線還給連線池），逾時就由呼叫端回 409。`sleep`／`monotonic` 可注入，測試不必真的等。
    """
    deadline = monotonic() + timeout
    while True:
        lock = acquire_advisory_lock(engine, key, shared=shared)
        if lock is not None:
            return lock
        if monotonic() >= deadline:
            return None
        sleep(interval)


@contextmanager
def try_advisory_lock(
    engine: Engine, key: int, *, shared: bool = False
) -> Iterator[AdvisoryLock | None]:
    """`with` 版本：取得時 yield 鎖、離開時釋放；取不到 yield `None`（呼叫端必須檢查）。"""
    lock = acquire_advisory_lock(engine, key, shared=shared)
    try:
        yield lock
    finally:
        if lock is not None:
            lock.release()
