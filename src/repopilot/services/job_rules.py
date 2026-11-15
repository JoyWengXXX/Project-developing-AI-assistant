"""背景工作的純規則（無資料庫、無 I/O，可直接單元測試）：失敗分類、退避、階段順序、秘密鍵名檢查。

規格 5.2（階段）、5.7（重試與退避）、5.9（重送間隔）。所有數值來自呼叫端傳入的設定，
這裡只放計算；預設值與驗證在 `config.Settings`。
"""

import random
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal

JOB_KINDS = (
    "snapshot_import",
    "snapshot_index",
    "github_register",
    "github_fetch",
    "snapshot_retrieval",
)
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

# 每種工作依序經過的階段（規格 5.2）；階段只描述進度，生命週期由 status 決定
PHASES_BY_KIND: dict[str, tuple[str, ...]] = {
    "snapshot_import": ("pending", "importing", "indexing", "completed"),
    "snapshot_index": ("pending", "indexing", "completed"),
    "snapshot_retrieval": ("pending", "embedding", "completed"),
    "github_register": ("pending", "resolving", "cloning", "registering", "completed"),
    "github_fetch": ("pending", "fetching", "completed"),
}

# 自動重試的暫時性基礎設施錯誤（規格 5.7）；其餘（含未預期例外）一律視為永久性，
# 直接 failed，由使用者決定是否手動 retry。`worker_lost`（租約過期）視為暫時性。
TRANSIENT_ERROR_CODES = frozenset(
    {
        "database_unavailable",
        "github_unreachable",
        "github_timeout",
        # GitHub API 回 5xx（只在 worker 的錯誤分類出現；同步端點的回應碼仍是 github_api_error）
        "github_server_error",
        "github_clone_timeout",
        "github_fetch_timeout",
        "git_timeout",
        "worker_lost",
    }
)

FailureClass = Literal["transient", "permanent"]

# 退避抖動：±20%（規格只寫「± 抖動」，比例是本專案的選擇，未實測）；抖動後仍不超過上限
JITTER_RATIO = 0.2
# 重送間隔上限 60 分鐘（規格 5.9）
MAX_RESEND_INTERVAL_SECONDS = 3600
# error_summary 的截斷長度（同 D28）
MAX_SUMMARY_CHARS = 500
# 取得 repo 鎖失敗而延後時的等待（規格 5.5：5～15 秒含抖動）
DEFER_MIN_SECONDS = 5.0
DEFER_MAX_SECONDS = 15.0

_SECRET_KEY = re.compile(
    r"token|secret|passw|authorization|api[_-]?key|credential|private[_-]?key", re.IGNORECASE
)


def classify_failure(error_code: str) -> FailureClass:
    """暫時性（可自動重試）或永久性；未知錯誤碼一律視為永久性（寧可讓使用者看到失敗）。"""
    return "transient" if error_code in TRANSIENT_ERROR_CODES else "permanent"


def backoff_seconds(
    attempts_used: int,
    *,
    base_seconds: float,
    max_seconds: float,
    rand: Callable[[], float] = random.random,
) -> float:
    """第 n 次失敗後的等待秒數：`min(max, base × 2^(n-1))`，再乘上 ±20% 抖動，結果不超過 max。

    `attempts_used` 是已消耗的執行次數（第一次失敗後為 1，等待 base 秒）。`rand` 回傳 [0, 1)，
    可注入以便確定性測試。
    """
    if attempts_used < 1:
        raise ValueError("attempts_used must be >= 1.")
    exponent = min(attempts_used - 1, 62)  # 避免超大指數；cap 早就生效
    delay = min(float(max_seconds), float(base_seconds) * float(2**exponent))
    jitter = 1 + JITTER_RATIO * (2 * rand() - 1)
    return min(float(max_seconds), max(0.0, delay * jitter))


def defer_seconds(rand: Callable[[], float] = random.random) -> float:
    """repo 鎖忙碌時延後的秒數：5～15 秒（含抖動）。"""
    return DEFER_MIN_SECONDS + (DEFER_MAX_SECONDS - DEFER_MIN_SECONDS) * rand()


def resend_interval_seconds(base_seconds: float, send_count: int) -> float:
    """已派送但未被接收的事件，第 n 次送出後要等多久才重送：逐次加倍，上限 60 分鐘。"""
    exponent = min(max(send_count - 1, 0), 62)
    return min(float(MAX_RESEND_INTERVAL_SECONDS), float(base_seconds) * float(2**exponent))


def phase_rank(kind: str, phase: str) -> int:
    """階段在該種工作中的順序（0 起算）；不屬於該種工作的階段拋 ValueError。"""
    phases = PHASES_BY_KIND.get(kind)
    if phases is None:
        raise ValueError(f"Unknown job kind: {kind!r}.")
    try:
        return phases.index(phase)
    except ValueError:
        raise ValueError(f"Phase {phase!r} is not valid for job kind {kind!r}.") from None


def truncate_summary(summary: str | None) -> str | None:
    if summary is None:
        return None
    return summary[:MAX_SUMMARY_CHARS]


def assert_no_secret_keys(value: Any, *, where: str) -> None:
    """拒絕鍵名看起來像秘密（token、password、secret…）的 JSON 資料。

    `params`、`work_state`、`result`、心跳 `info` 都會被持久化，其中 `params`／`result` 還會經 API
    回傳；這是「不得含 token 等秘密」的最後一道防線（只檢查鍵名，值由呼叫端負責）。
    違反是程式錯誤，拋 `ValueError`（不是使用者輸入問題）。
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ValueError(f"{where} must not contain secret-looking keys (found {key!r}).")
            assert_no_secret_keys(item, where=where)
    elif isinstance(value, list | tuple):
        for item in value:
            assert_no_secret_keys(item, where=where)
