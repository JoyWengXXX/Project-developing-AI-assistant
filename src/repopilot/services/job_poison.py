"""毒藥檔案（poison unit）框架（第二階段切片 4，規格 5.9，D43）。

問題：某個輸入（解析器遇到的特定檔案、讀取時讓程序崩潰的 blob…）每次都讓 worker 崩潰。租約過期後
復原掃描會重新排隊，下一個 attempt 又在同一處崩潰，直到 `max_attempts` 耗盡，整個 job 失敗。

做法：worker 在處理每個**單位**（`WorkUnit`）之前，把「目前單位」寫進 `jobs.work_state`
（`current_unit`）。worker 被殺時這個指標就留在資料庫裡；下一個 attempt 一開始比對：

- 上一個 attempt 是以 `worker_lost` 結束（`jobs.error_code`）**且** `current_unit` 還在 → 崩潰發生在
  那個單位 → 計入 `work_state.poison.suspect`（同一單位連續崩潰才累加；崩潰換到別的單位就重新
  計數，因為前一個單位顯然撐過去了）。
- 累計達 `SKIP_THRESHOLD`（2）次 → `should_skip` 回傳 True：**可略過的呼叫者**（切片 6 的索引逐檔
  解析）略過該單位並以 `skip_unit` 記錄（`work_state.poison.skipped`），不再讓它耗盡額度；
  **不可略過的呼叫者**（匯入：略過一批 blob 會讓快照殘缺，違反「completed 的快照一定完整」）
  不呼叫 `should_skip`，只留下紀錄，行為仍是「達 `max_attempts` 後 `failed`（`worker_lost`）」。

這個模組只有**純函式與一個很薄的狀態追蹤類別**：沒有資料庫、沒有 Celery。寫入由呼叫端提供的
`persist(patch) -> work_state` 完成（worker 傳入 `update_progress(work_state=patch)` 的包裝，
所以寫入是令牌柵欄的，舊 worker 寫不進來）。`work_state` 淺層合併，所以 patch 以頂層鍵為單位。

只記錄單位的**不透明識別**（批次指紋、之後的檔案 id），不記錄路徑與內容，API 只回傳計數
（`summarize`）。

.NET 對照：像 Polly 的 circuit breaker 狀態，但狀態存在資料庫、跨 worker 行程存活。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

CrashKind = Literal["parser_crash", "worker_crash"]
CRASH_KINDS: tuple[str, ...] = ("parser_crash", "worker_crash")

WORKER_LOST = "worker_lost"
# 同一單位連續崩潰達這個次數就視為毒藥（規格 5.9 寫「前一個 attempt 崩潰就略過」＝1 次；
# 這裡取 2：一次崩潰也可能是 OOM、容器重啟等與單位無關的原因，需要第二次同處崩潰才確認）
SKIP_THRESHOLD = 2
# 略過紀錄最多保留的筆數（work_state 是 JSONB，不能無限成長）；超過時丟棄最舊的
MAX_SKIPPED_RECORDS = 100


@dataclass(frozen=True)
class WorkUnit:
    """worker 正在處理的一個單位。`unit_id` 必須在同樣的輸入下每次都相同（跨 attempt 比對用）。"""

    unit_id: str
    kind: str  # 例如 "blob_batch"（匯入）、"file"（切片 6 的索引）
    crash_kind: CrashKind
    index: int | None = None  # 第幾個（0 起算）；只供顯示進度
    count: int | None = None


@dataclass(frozen=True)
class PoisonSummary:
    """給 API 的安全摘要：只有計數與單位種類，沒有識別、路徑或內容。"""

    current_unit_kind: str | None
    current_unit_index: int | None
    current_unit_count: int | None
    suspect_crashes: int
    skipped_units: int
    total_crashes: int


# --- 讀取（容錯：work_state 是 JSONB，形狀不可信，錯誤的形狀一律當成「沒有」） ---


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _current_unit(work_state: dict[str, Any]) -> dict[str, Any] | None:
    current = work_state.get("current_unit")
    if not isinstance(current, dict):
        return None
    unit_id, kind, crash_kind = current.get("id"), current.get("kind"), current.get("crash_kind")
    if not isinstance(unit_id, str) or not isinstance(kind, str) or crash_kind not in CRASH_KINDS:
        return None
    return {
        "id": unit_id,
        "kind": kind,
        "crash_kind": crash_kind,
        "index": _as_int(current.get("index")),
        "count": _as_int(current.get("count")),
    }


def _poison(work_state: dict[str, Any]) -> dict[str, Any]:
    """正規化後的 poison 狀態：`suspect`（dict 或 None）、`skipped`（list）、`total_crashes`。"""
    raw = work_state.get("poison")
    raw = raw if isinstance(raw, dict) else {}
    suspect = None
    raw_suspect = raw.get("suspect")
    if isinstance(raw_suspect, dict):
        unit, crashes = raw_suspect.get("unit"), _as_int(raw_suspect.get("crashes"))
        crash_kind = raw_suspect.get("crash_kind")
        if isinstance(unit, str) and crashes is not None and crashes >= 1:
            suspect = {
                "unit": unit,
                "kind": str(raw_suspect.get("kind", "")),
                "crash_kind": crash_kind if crash_kind in CRASH_KINDS else "worker_crash",
                "crashes": crashes,
            }
    raw_skipped = raw.get("skipped")
    skipped = [
        item
        for item in (raw_skipped if isinstance(raw_skipped, list) else [])
        if isinstance(item, dict) and isinstance(item.get("unit"), str)
    ]
    total = _as_int(raw.get("total_crashes"))
    return {"suspect": suspect, "skipped": skipped, "total_crashes": max(total or 0, 0)}


# --- 純函式：狀態轉換（回傳要淺層合併進 work_state 的 patch；沒有變化回傳 None） ---


def enter_unit_patch(unit: WorkUnit) -> dict[str, Any]:
    """開始處理某個單位**之前**要寫入的 patch。"""
    return {
        "current_unit": {
            "id": unit.unit_id,
            "kind": unit.kind,
            "crash_kind": unit.crash_kind,
            "index": unit.index,
            "count": unit.count,
        }
    }


def begin_attempt_patch(
    work_state: dict[str, Any], previous_error_code: str | None
) -> dict[str, Any] | None:
    """新 attempt 開始時：把上一個 attempt 留下的 `current_unit` 結算掉。

    - 沒有 `current_unit`（第一次執行，或上次已正常收尾）→ `None`，不必寫入。
    - 上一個 attempt 以 `worker_lost` 結束 → 崩潰發生在該單位：累加 `suspect`（同一單位連續才
      累加，換單位就從 1 重新計數）與 `total_crashes`，並清除 `current_unit`。
    - 其他結束方式（`git_timeout`、取消…）→ 只清除過時的 `current_unit`，不計崩潰：那些路徑是
      worker 自己處理的失敗，不是崩潰。
    """
    if work_state.get("current_unit") is None:
        return None
    patch: dict[str, Any] = {"current_unit": None}
    current = _current_unit(work_state)
    if current is None or previous_error_code != WORKER_LOST:
        return patch
    poison = _poison(work_state)
    suspect = poison["suspect"]
    crashes = suspect["crashes"] + 1 if suspect and suspect["unit"] == current["id"] else 1
    patch["poison"] = {
        "suspect": {
            "unit": current["id"],
            "kind": current["kind"],
            "crash_kind": current["crash_kind"],
            "crashes": crashes,
        },
        "skipped": poison["skipped"],
        "total_crashes": poison["total_crashes"] + 1,
    }
    return patch


def should_skip(
    work_state: dict[str, Any], unit_id: str, *, threshold: int = SKIP_THRESHOLD
) -> bool:
    """這個單位是否該被略過：已被略過過，或同一單位連續崩潰達 `threshold` 次。"""
    poison = _poison(work_state)
    if any(item["unit"] == unit_id for item in poison["skipped"]):
        return True
    suspect = poison["suspect"]
    return bool(suspect and suspect["unit"] == unit_id and suspect["crashes"] >= threshold)


def skip_unit_patch(work_state: dict[str, Any], unit_id: str) -> dict[str, Any] | None:
    """記錄「略過了這個單位」：從 `suspect` 搬到 `skipped`（保留種類與次數）；已記錄則 `None`。"""
    poison = _poison(work_state)
    if any(item["unit"] == unit_id for item in poison["skipped"]):
        return None
    suspect = poison["suspect"]
    matched = suspect is not None and suspect["unit"] == unit_id
    record = {
        "unit": unit_id,
        "crash_kind": suspect["crash_kind"] if matched and suspect else "worker_crash",
        "crashes": suspect["crashes"] if matched and suspect else 0,
    }
    skipped = [*poison["skipped"], record][-MAX_SKIPPED_RECORDS:]
    return {
        "poison": {
            "suspect": None if matched else poison["suspect"],
            "skipped": skipped,
            "total_crashes": poison["total_crashes"],
        }
    }


def summarize(work_state: dict[str, Any]) -> PoisonSummary:
    """給 API 的安全摘要（形狀錯誤的 work_state 回傳全零）。"""
    current = _current_unit(work_state)
    poison = _poison(work_state)
    suspect = poison["suspect"]
    return PoisonSummary(
        current_unit_kind=current["kind"] if current else None,
        current_unit_index=current["index"] if current else None,
        current_unit_count=current["count"] if current else None,
        suspect_crashes=suspect["crashes"] if suspect else 0,
        skipped_units=len(poison["skipped"]),
        total_crashes=poison["total_crashes"],
    )


# --- 薄的狀態追蹤（持有 work_state 的鏡像，寫入交給呼叫端） ---


class PoisonTracker:
    """worker 一個 attempt 內使用：開始時結算上一次的崩潰，之後逐單位記錄。

    `persist(patch)` 把 patch 淺層合併進資料庫的 `work_state`（令牌柵欄）並回傳合併後的整份
    `work_state`；追蹤器用回傳值更新自己的鏡像。沒有要寫的就不呼叫 `persist`。
    """

    def __init__(
        self,
        work_state: dict[str, Any],
        persist: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        threshold: int = SKIP_THRESHOLD,
    ) -> None:
        self._state = dict(work_state)
        self._persist = persist
        self._threshold = threshold

    @property
    def work_state(self) -> dict[str, Any]:
        return dict(self._state)

    @property
    def summary(self) -> PoisonSummary:
        return summarize(self._state)

    def _apply(self, patch: dict[str, Any] | None) -> None:
        if patch is not None:
            self._state = dict(self._persist(patch))

    def begin_attempt(self, previous_error_code: str | None) -> None:
        self._apply(begin_attempt_patch(self._state, previous_error_code))

    def enter(self, unit: WorkUnit) -> None:
        self._apply(enter_unit_patch(unit))

    def should_skip(self, unit_id: str) -> bool:
        return should_skip(self._state, unit_id, threshold=self._threshold)

    def skip(self, unit_id: str) -> None:
        self._apply(skip_unit_patch(self._state, unit_id))
