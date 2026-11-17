"""檢索索引工作的進度摘要：由 `work_state` 推估剩餘時間（只是估計）。"""

from typing import Any

# 太早的估計很不準：至少跑滿這段時間、且至少處理了一些字元才提供
MIN_RUN_SECONDS = 30.0
EMBEDDING_KEY = "embedding"


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def embedding_summary(work_state: dict[str, Any]) -> tuple[int | None, int | None]:
    """回傳 (已寫出的向量數, 估計剩餘秒數)；資料缺漏或不合理時對應值為 None，不偽造。

    估計以「本次執行已處理字元數 / 本次執行秒數」的速率線性外推剩餘字元數；片段大小不一，
    模型速度也會變，所以只是估計。
    """
    state = work_state.get(EMBEDDING_KEY)
    if not isinstance(state, dict):
        return None, None
    vectors = _count(state.get("vectors"))
    done = _count(state.get("chars_done"))
    total = _count(state.get("chars_total"))
    base = _count(state.get("run_base_chars"))
    run_seconds = _seconds(state.get("run_seconds"))
    if done is None or total is None or base is None or run_seconds is None:
        return vectors, None
    processed = done - base
    if processed <= 0 or run_seconds < MIN_RUN_SECONDS or total < done:
        return vectors, None
    return vectors, round((total - done) * run_seconds / processed)


def saved_chunks(work_state: dict[str, Any]) -> tuple[int, int] | None:
    """已保存的片段進度 (已完成, 總數)；佇列中的工作進度欄位會歸零，用它顯示「分段執行」的進度。"""
    state = work_state.get(EMBEDDING_KEY)
    if not isinstance(state, dict):
        return None
    done, total = _count(state.get("chunks_done")), _count(state.get("chunks_total"))
    if done is None or total is None or done > total:
        return None
    return done, total


def reused_chunks(work_state: dict[str, Any]) -> int | None:
    """本次執行以既有相同內容的向量複製完成的片段數；缺漏或不合理時為 None。"""
    state = work_state.get(EMBEDDING_KEY)
    return _count(state.get("reused_chunks")) if isinstance(state, dict) else None
