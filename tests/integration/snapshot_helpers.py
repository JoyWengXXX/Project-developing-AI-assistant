"""參考版本與移除快照（第二階段切片 3b）整合測試共用的資料庫輔助函式。

直接以 SQL／ORM 建立快照與 job 列：這些測試要精確控制狀態（importing 的 `started_at`、
進行中的 job、20,000 個檔案列），不經過會執行 Git 的匯入流程。
"""

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, insert, text
from sqlalchemy.orm import Session

from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.services.import_jobs import import_dedupe_key

TEST_PROFILE = "test-profile"


def add_snapshot(
    engine: Engine,
    repository_id: int,
    *,
    status: str = "completed",
    started_at: datetime | None = None,
    files: int = 0,
    index: int = 1,
    profile_hash: str = TEST_PROFILE,
) -> int:
    """直接寫入快照（與可選的大量檔案列）；`index` 讓同一 repo 的 commit_sha 不重複。"""
    with Session(engine) as session:
        snapshot = Snapshot(
            repository_id=repository_id,
            commit_sha=commit_for(index),
            import_profile_hash=profile_hash,
            status=status,
            file_count=files if status == "completed" else 0,
            started_at=started_at or datetime.now(UTC),
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id
        if files:
            session.execute(
                insert(SnapshotFile),
                [
                    {
                        "snapshot_id": snapshot_id,
                        "path": f"dir/file-{number}.txt",
                        "content": "x" * 100,
                        "byte_size": 100,
                        "status": "imported",
                    }
                    for number in range(files)
                ],
            )
        session.commit()
    return snapshot_id


def commit_for(index: int) -> str:
    return f"{index:040x}"


def import_key_for(repository_id: int, index: int = 1, profile_hash: str = TEST_PROFILE) -> str:
    """`add_snapshot(..., index=index)` 建立的快照對應的匯入 job `dedupe_key`。"""
    return import_dedupe_key(repository_id, commit_for(index), profile_hash)


def insert_job(engine: Engine, status: str = "queued", **overrides: Any) -> int:
    """直接寫入 job 列（滿足約束：running 要有令牌與租約，終態要有 finished_at）。"""
    values: dict[str, Any] = {
        "kind": "snapshot_import",
        "dedupe_key": f"k-{uuid.uuid4().hex}",
        "status": status,
        "max_attempts": 3,
    }
    now = datetime.now(UTC)
    if status == "running":
        values.update(lease_token=uuid.uuid4(), lease_expires_at=now)
    if status in ("succeeded", "failed", "cancelled"):
        values["finished_at"] = now
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) RETURNING id"),  # noqa: S608
                values,
            ).scalar_one()
        )


def count(engine: Engine, table: str, where: str = "TRUE", **params: Any) -> int:
    """資料表的列數（獨立連線；`table`、`where` 只由測試碼提供，不是使用者輸入）。"""
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE {where}"),  # noqa: S608
                params,
            ).scalar_one()
        )


def wait_until_a_query_is_blocked(engine: Engine, timeout: float = 15.0) -> None:
    """輪詢 pg_stat_activity，直到有查詢在等待鎖（觀察資料庫狀態，不是用 sleep 判斷成功）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            blocked = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar()
        if blocked:
            return
        time.sleep(0.02)
    raise AssertionError("沒有觀察到被鎖擋住的查詢")
