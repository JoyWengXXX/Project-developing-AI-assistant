"""測試用：把快照鎖換成「永遠取得、不互斥」的空鎖。

第二階段切片 3 起，同步匯入與 worker 匯入以 advisory lock 互斥同一快照。第一階段有幾個測試模擬
「原擁有者匯入到一半、`started_at` 逾時，另一個請求接手」——真實環境中活著的匯入者持有鎖，
接手者會被擋下（這正是新的、更強的保證）；但這些測試要驗證的是**資料面**的柵欄
（`snapshots.started_at`，規格 5.4：「即使租約與鎖有缺陷，舊寫入者也無法覆寫新擁有者」），
所以在這些測試裡把鎖關掉，等於模擬「鎖失效或鎖連線被中斷」時資料面仍然安全。
.NET 對照：像在單元測試中以 `NullLock` 取代分散式鎖，單獨驗證下一道防線。
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Engine

from repopilot.services import snapshots as snapshot_service


class NullAdvisoryLock:
    """符合 `AdvisoryLock` 介面（`key`、`shared`、`assert_alive`、`release`）的空鎖。"""

    shared = False

    def __init__(self, key: int) -> None:
        self.key = key

    def assert_alive(self) -> None:
        return None

    def release(self) -> None:
        return None


@contextmanager
def _always_granted(
    engine: Engine, key: int, *, shared: bool = False
) -> Iterator[NullAdvisoryLock]:
    yield NullAdvisoryLock(key)


def without_snapshot_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步匯入路徑的快照鎖改為永遠取得（只影響 `snapshots.try_advisory_lock`）。"""
    monkeypatch.setattr(snapshot_service, "try_advisory_lock", _always_granted)
