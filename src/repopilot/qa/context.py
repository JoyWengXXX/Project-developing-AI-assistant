"""問答上下文組裝：依相關度加入片段、穩定代號 C1…Cn、token 預算與不可偽造的資料區塊。

純函式，不碰資料庫與供應商。輸入片段對應檢索結果（`RetrievalSearchItem`）的欄位，
由呼叫端補上 repo 名稱。
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import count
from typing import Literal

_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_BOUNDARY_LENGTH = 32
_BOUNDARY_PLACEHOLDER = "0" * _BOUNDARY_LENGTH

SkipReason = Literal["count_cap", "budget_exhausted", "empty_text"]


@dataclass(frozen=True)
class ContextLimits:
    """預設值是暫定值（issue #4 決策記錄），4-5 實測後再調整。"""

    max_input_tokens: int = 40_000
    max_chunks: int = 20
    max_chunk_chars: int = 6_000

    def __post_init__(self) -> None:
        if min(self.max_input_tokens, self.max_chunks, self.max_chunk_chars) < 1:
            raise ValueError("context limits must be positive")


@dataclass(frozen=True)
class RetrievedChunk:
    """一個檢索到的片段；行號為 1-based、含頭尾。呼叫端須依相關度由高到低排序。"""

    chunk_id: int
    repo: str
    snapshot_id: int
    commit_sha: str
    path: str
    start_line: int
    end_line: int
    text: str

    def __post_init__(self) -> None:
        if not _SHA_PATTERN.fullmatch(self.commit_sha):
            raise ValueError("commit_sha must be a full 40-character lowercase hex SHA")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("chunk line range must be 1-based with end_line >= start_line")


@dataclass(frozen=True)
class IncludedChunk:
    """送給模型的片段。截斷時 `end_line` 是實際送出的最後一行，引用只能落在送出的範圍內。"""

    code: str
    chunk_id: int
    repo: str
    snapshot_id: int
    commit_sha: str
    path: str
    start_line: int
    end_line: int
    text: str
    truncated: bool


@dataclass(frozen=True)
class SkippedChunk:
    chunk: RetrievedChunk
    reason: SkipReason


@dataclass(frozen=True)
class ContextResult:
    included: tuple[IncludedChunk, ...]
    skipped: tuple[SkippedChunk, ...]
    # 沒有任何片段時為空字串
    data_block: str
    boundary: str
    estimated_tokens: int

    def get(self, code: str) -> IncludedChunk | None:
        for chunk in self.included:
            if chunk.code == code:
                return chunk
        return None


def estimate_tokens(text: str) -> int:
    """保守估計：UTF-8 位元組數 ÷ 3 取上限；實際用量以供應商回報為準。"""
    return _tokens_for_bytes(_byte_length(text))


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8", errors="surrogatepass"))


def _tokens_for_bytes(byte_count: int) -> int:
    return -(-byte_count // 3)


def _boundary_candidate(seed: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{seed}\0{attempt}".encode("utf-8", errors="surrogatepass"))
    return digest.hexdigest()[:_BOUNDARY_LENGTH]


def _begin_line(boundary: str) -> str:
    return f"<<<REPOPILOT_DATA {boundary} BEGIN>>>"


def _end_line(boundary: str) -> str:
    return f"<<<REPOPILOT_DATA {boundary} END>>>"


def _envelope_bytes() -> int:
    # begin 行與 end 行；區塊以換行串接，n 個片段共 n + 1 個換行（每個片段另計 1 個）
    return (
        _byte_length(_begin_line(_BOUNDARY_PLACEHOLDER))
        + _byte_length(_end_line(_BOUNDARY_PLACEHOLDER))
        + 1
    )


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    body = text.removesuffix("\n")
    if len(body) <= max_chars:
        return body, False
    cut = body[:max_chars]
    if body[max_chars] != "\n":
        newline = cut.rfind("\n")
        if newline > 0:
            cut = cut[:newline]
    return cut, True


def _render_chunk(chunk: IncludedChunk, boundary: str) -> str:
    # repo 與 path 來自 repo 內容，以 JSON 字串輸出以跳脫換行與引號
    header = (
        f"<<<CHUNK {boundary} code={chunk.code} repo={_quote(chunk.repo)} "
        f"snapshot={chunk.snapshot_id} commit={chunk.commit_sha} path={_quote(chunk.path)} "
        f"lines={chunk.start_line}-{chunk.end_line}>>>"
    )
    numbered = (
        f"{chunk.start_line + offset}: {line}" for offset, line in enumerate(chunk.text.split("\n"))
    )
    return "\n".join((header, *numbered))


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_context(chunks: Sequence[RetrievedChunk], limits: ContextLimits) -> ContextResult:
    """依輸入順序（相關度由高到低）加入片段。

    - 超過每片段字元上限者截斷到完整行並標記 `truncated`。
    - 下一個片段放不進預算時停止；它與其後的片段都記為 `budget_exhausted`
      （不跳過大片段去塞後面較不相關的，維持相關度順序）。
    - 超過片段數上限者記為 `count_cap`。
    - `estimated_tokens` 是完整資料區塊的估計值，保證不超過 `max_input_tokens`。
    """
    included: list[IncludedChunk] = []
    skipped: list[SkippedChunk] = []
    used_bytes = _envelope_bytes()
    budget_exhausted = False

    for chunk in chunks:
        if not chunk.text.strip():
            skipped.append(SkippedChunk(chunk, "empty_text"))
            continue
        if len(included) >= limits.max_chunks:
            skipped.append(SkippedChunk(chunk, "count_cap"))
            continue
        if budget_exhausted:
            skipped.append(SkippedChunk(chunk, "budget_exhausted"))
            continue
        candidate = _shown_chunk(chunk, f"C{len(included) + 1}", limits.max_chunk_chars)
        rendered = _render_chunk(candidate, _BOUNDARY_PLACEHOLDER)
        needed = used_bytes + _byte_length(rendered) + 1
        if _tokens_for_bytes(needed) > limits.max_input_tokens:
            budget_exhausted = True
            skipped.append(SkippedChunk(chunk, "budget_exhausted"))
            continue
        used_bytes = needed
        included.append(candidate)

    if not included:
        return ContextResult((), tuple(skipped), "", "", 0)

    boundary = _pick_boundary(included)
    data_block = "\n".join(
        (
            _begin_line(boundary),
            *(_render_chunk(item, boundary) for item in included),
            _end_line(boundary),
        )
    )
    return ContextResult(
        tuple(included), tuple(skipped), data_block, boundary, estimate_tokens(data_block)
    )


def _shown_chunk(chunk: RetrievedChunk, code: str, max_chars: int) -> IncludedChunk:
    shown, truncated = _truncate(chunk.text, max_chars)
    end_line = chunk.end_line
    if truncated:
        end_line = min(chunk.end_line, chunk.start_line + shown.count("\n"))
    return IncludedChunk(
        code=code,
        chunk_id=chunk.chunk_id,
        repo=chunk.repo,
        snapshot_id=chunk.snapshot_id,
        commit_sha=chunk.commit_sha,
        path=chunk.path,
        start_line=chunk.start_line,
        end_line=end_line,
        text=shown,
        truncated=truncated,
    )


def _pick_boundary(included: Sequence[IncludedChunk]) -> str:
    """由內容雜湊導出分隔 token；若恰好出現在任何片段內容就換下一個，因此片段無法預先偽造。"""
    searchable = [f"{item.text}\n{item.repo}\n{item.path}" for item in included]
    seed = hashlib.sha256("\0".join(searchable).encode("utf-8", errors="surrogatepass")).hexdigest()
    for attempt in count():
        candidate = _boundary_candidate(seed, attempt)
        if not any(candidate in text for text in searchable):
            return candidate
    raise AssertionError("unreachable")
