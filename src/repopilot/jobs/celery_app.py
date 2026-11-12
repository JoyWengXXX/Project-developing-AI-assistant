"""Celery app 的建立（第二階段切片 3，規格 5.13，D40）與 dispatcher 用的發布器。

**這個模組不定義任何任務**：dispatcher 只是 Celery 的「發布端」，以任務名稱 `send_task` 把
job id 丟進 broker，不載入也不執行任務程式碼（規格 9.3）。任務在 `jobs/worker.py`，只有
worker 載入。

設定取捨（逐項說明在 docs/architecture.md D40）：
- broker 是 Redis，**不設 result backend**（`task_ignore_result`）：事實來源是 PostgreSQL。
- `task_acks_late=True`：worker 正常關閉時未完成的訊息會回到佇列；重複交付由資料庫的租約擋下。
- `task_reject_on_worker_lost` 維持預設（停用）：Celery 文件警告啟用可能造成訊息迴圈；
  worker 被殺的復原由資料庫的復原掃描負責，不依賴 broker 重新交付。
- `worker_prefetch_multiplier=1`：長工作不會預取一堆訊息卡在忙碌的 worker。
- `visibility_timeout` 大於 `task_time_limit`：未在期限內確認的訊息才會被重新交付；
  就算重送，也只會被租約丟棄。
- JSON 序列化；訊息只含 `job_id`（不帶路徑、token 或任何內容）。
- 不用 ETA／countdown：重試的退避由資料庫的 `not_before` 控制（Celery 文件：ETA 超過
  visibility timeout 的任務會被重複執行）。
- Redis 鍵前綴 `global_keyprefix` 與佇列名稱可設定，測試堆疊與開發堆疊不共用任何 broker 資料。

.NET 對照：像 MassTransit／NServiceBus 的「只發布端」設定；訊息本身只帶識別碼，內容以資料庫為準。
"""

from typing import Any

from celery import Celery

from repopilot.config import Settings

TASK_NAME = "repopilot.run_job"

# visibility timeout 必須大於 `task_time_limit`（規格 5.13）；多留的緩衝秒數
_VISIBILITY_MARGIN_SECONDS = 600


def create_celery_app(settings: Settings, *, role: str = "worker") -> Celery:
    """建立 Celery app（不連線）。

    `role` 為 `dispatcher` 時使用較短的 socket 逾時，broker 不通時快速失敗。
    `CELERY_BROKER_URL` 未設定時拋 `RuntimeError`（明確失敗，不使用預設位址）。
    """
    broker_url = settings.celery_broker_url.get_secret_value()
    if not broker_url:
        raise RuntimeError("CELERY_BROKER_URL is not configured.")
    transport_options: dict[str, Any] = {
        "visibility_timeout": settings.worker_task_time_limit_seconds + _VISIBILITY_MARGIN_SECONDS,
        "global_keyprefix": settings.celery_global_keyprefix,
        "socket_connect_timeout": 5,
    }
    if role == "dispatcher":
        # broker 被暫停或網路黑洞時，發布不要卡住整個派送迴圈（失敗會保持 pending 並退避重試）
        transport_options["socket_timeout"] = 10
    app = Celery("repopilot", broker=broker_url)
    app.conf.update(
        task_default_queue=settings.celery_queue_name,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_ignore_result=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_time_limit=settings.worker_task_time_limit_seconds,
        task_soft_time_limit=settings.worker_task_time_limit_seconds - 60,
        # 限制長時間執行的記憶體累積（規格 5.13 的例子；未實測）
        worker_max_tasks_per_child=50,
        broker_connection_retry_on_startup=True,
        broker_transport_options=transport_options,
        # 日誌由應用程式的 JSON 日誌負責，不讓 Celery 接管 root logger
        worker_hijack_root_logger=False,
        timezone="UTC",
        enable_utc=True,
    )
    return app


class CeleryPublisher:
    """`services.job_dispatch.JobPublisher` 的 Celery 實作：以 `send_task` 發布，只帶 `job_id`。

    `send_task` 成功返回＝broker（Redis）已接受訊息（`LPUSH` 同步完成）；失敗拋例外，
    `dispatch_pending` 會讓事件保持 `pending` 並記錄失敗，**不會標記為已派送**。
    `retry=False`：不在發布端做 Celery 內建的重試迴圈，重試由 dispatcher 迴圈的退避負責
    （行為可預測）。
    """

    def __init__(self, app: Celery, queue: str) -> None:
        self._app = app
        self._queue = queue

    def publish(self, job_id: int) -> None:
        self._app.send_task(
            TASK_NAME, args=[job_id], queue=self._queue, retry=False, ignore_result=True
        )
