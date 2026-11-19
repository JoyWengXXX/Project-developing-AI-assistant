"""問答流程的純規則與暫定常數（不碰資料庫與供應商）；數值是暫定值，4-5 實測後再調整。"""

import json
import re
from typing import Final

from repopilot.qa.context import ContextLimits, estimate_tokens
from repopilot.qa.prompts import SYSTEM_PROMPT, answer_json_schema
from repopilot.qa.provider import Effort

MAX_QUESTION_CHARS: Final = 2000
MAX_SNAPSHOTS: Final = 20
DEFAULT_MAX_TOKENS: Final = 4096
MAX_TOKENS_CEILING: Final = 16_384
# 供應商請求：effort 與逾時是固定值；Haiku 可關閉思考時關閉，Sonnet／Opus 只能調 effort
GENERATE_EFFORT: Final[Effort] = "medium"
GENERATE_TIMEOUT_SECONDS: Final = 120.0
# 混合檢索取回的候選數（上下文最多收 `ContextLimits.max_chunks` 個，多取是為了補空片段）
RETRIEVAL_CANDIDATES: Final = 30
# 回答文字的字元上限＝max_tokens × 此值（粗上限，只用來擋明顯失控的輸出）
ANSWER_CHARS_PER_TOKEN_CAP: Final = 16
# 提問時上限估計的餘裕：資料區塊以外的固定說明文字（含分隔 token 的重複敘述）
_WRAPPER_SLACK_TOKENS: Final = 512

_MAX_QUERY_TERMS: Final = 16
_MAX_QUERY_CHARS: Final = 512


def retrieval_query(question: str) -> str:
    """問題 → 混合檢索用的查詢。

    關鍵字比對最多接受 16 個不同詞、embedding 最多 512 token，自然語言問題常常超過：
    超過時保留較長（通常較有資訊量）的詞，維持原本順序；整體不超過 512 字元。
    """
    terms: dict[str, str] = {}
    for word in re.findall(r"\w+", question):
        terms.setdefault(word.lower(), word)
    words = list(terms.values())
    if len(words) > _MAX_QUERY_TERMS:
        longest = sorted(range(len(words)), key=lambda index: (-len(words[index]), index))
        keep = set(longest[:_MAX_QUERY_TERMS])
        words = [word for index, word in enumerate(words) if index in keep]
    chosen: list[str] = []
    length = 0
    for word in words:
        length += len(word) + (1 if chosen else 0)
        if length > _MAX_QUERY_CHARS:
            if not chosen:
                chosen.append(word[:_MAX_QUERY_CHARS])
            break
        chosen.append(word)
    return " ".join(chosen)


def prompt_overhead_tokens() -> int:
    """系統提示與 JSON schema 的估計 token（每次請求都會送）。"""
    return estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(json.dumps(answer_json_schema()))


def estimate_request_input_tokens(user_message: str) -> int:
    """實際請求的輸入估計：系統提示＋schema＋完整使用者訊息（含資料區塊與問題）。"""
    return prompt_overhead_tokens() + estimate_tokens(user_message)


def input_tokens_upper_bound(question: str, limits: ContextLimits | None = None) -> int:
    """提問時（還沒檢索）的輸入上限估計：資料區塊最多 `max_input_tokens`，外加固定部分。"""
    limits = limits or ContextLimits()
    return (
        limits.max_input_tokens
        + prompt_overhead_tokens()
        + estimate_tokens(question)
        + _WRAPPER_SLACK_TOKENS
    )


def answer_max_chars(max_tokens: int) -> int:
    return max_tokens * ANSWER_CHARS_PER_TOKEN_CAP
