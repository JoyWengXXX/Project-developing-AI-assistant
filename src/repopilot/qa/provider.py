"""生成模型供應商的小介面：一次 `generate`、一次請求前 `count_tokens`。"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from repopilot.qa.errors import ProviderMalformedResponseError, UnsupportedModelError

Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class ModelInfo:
    # Sonnet／Opus 不能關閉思考（送 disabled 會被拒絕），只能調整 effort
    can_disable_thinking: bool


SUPPORTED_MODELS: dict[str, ModelInfo] = {
    "claude-haiku-5-5": ModelInfo(can_disable_thinking=True),
    "claude-sonnet-5-5": ModelInfo(can_disable_thinking=False),
    "claude-opus-5-5": ModelInfo(can_disable_thinking=False),
}


def get_model_info(model: str) -> ModelInfo:
    try:
        return SUPPORTED_MODELS[model]
    except KeyError:
        raise UnsupportedModelError("model is not in the supported list") from None


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class GenerateRequest:
    model: str
    system: str
    messages: tuple[ChatMessage, ...]
    max_tokens: int
    effort: Effort
    timeout_seconds: float
    json_schema: dict[str, object] | None = None
    # 只是「希望關閉思考」；模型或 effort 不允許時轉接層不送出
    disable_thinking: bool = False


@dataclass(frozen=True)
class Usage:
    """一次呼叫的用量。`input_tokens` 已含快取建立與快取讀取；`output_tokens` 已含思考。"""

    input_tokens: int
    output_tokens: int
    thinking_tokens: int | None = None
    known: bool = True

    @classmethod
    def unknown(cls) -> "Usage":
        return cls(input_tokens=0, output_tokens=0, known=False)


class GenerationOutcome(StrEnum):
    COMPLETED = "completed"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    CONTEXT_WINDOW_EXCEEDED = "model_context_window_exceeded"


@dataclass(frozen=True)
class GenerateResponse:
    model: str
    outcome: GenerationOutcome
    # 非 COMPLETED 時可能是不完整或空字串，呼叫端不得當成答案
    text: str
    usage: Usage
    request_id: str | None = None


class QAProvider(Protocol):
    def generate(self, request: GenerateRequest) -> GenerateResponse: ...

    def count_tokens(self, request: GenerateRequest) -> int:
        """請求前的輸入 token 估計值；結算一律以 `generate` 回報的用量為準。"""
        ...


def validate_json_output(text: str) -> None:
    """要求結構化輸出必須是 JSON 物件；錯誤訊息不含內容。"""
    try:
        parsed = json.loads(text)
    except ValueError:
        raise ProviderMalformedResponseError("response text is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise ProviderMalformedResponseError("response JSON is not an object")
