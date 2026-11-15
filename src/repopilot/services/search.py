"""快照內的三種搜尋：檔名（`files`）、符號（`symbols`）、內容（`text`）（第二階段切片 8，規格 8）。

共通規則：字面子字串、區分大小寫（`%`、`_`、`\\` 一律按字面）、排序固定且穩定；範圍限定在
URL 指定的快照。`files`／`text` 只看 `status='imported'` 的檔案，不依賴結構索引（C7）；`symbols`
只採用目前設定下 `completed` 的索引。每個請求在自己的交易內設定 `statement_timeout`；逾時
一律拋 `SearchTimeoutError`，絕不回成功的空列表。
"""

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg.errors
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import IndexedFile, Snapshot, SnapshotFile, Symbol
from repopilot.errors import SearchTimeoutError
from repopilot.services import index_queries
from repopilot.services.snapshot_queries import get_completed_snapshot

# 內容搜尋在資料庫端分批過濾：第一批小（常見詞很快湊滿一頁就結束），之後每批加倍直到上限，
# 避免掃完整個快照時每批一次往返的成本主導（20,000 檔、每批 20 檔實測約 8.5 秒）。
# 每批同時受檔案數與（依 byte_size 的）位元組數限制，所以同時最多持有約
# TEXT_BATCH_MAX_BYTES 的內容文字（字元數不會超過位元組數）
TEXT_BATCH_FIRST_FILES = 20
TEXT_BATCH_MAX_FILES = 500
TEXT_BATCH_MAX_BYTES = 8 * 1024 * 1024
SNIPPET_MAX_CHARS = 300
SNIPPET_LEAD_CHARS = 100


@dataclass(frozen=True)
class FileMatch:
    file_id: int
    path: str
    byte_size: int | None


@dataclass(frozen=True)
class SymbolMatch:
    symbol_id: int
    kind: str
    language: str
    name: str
    qualified_name: str
    file_id: int
    path: str
    start_line: int
    end_line: int
    has_errors: bool


@dataclass(frozen=True)
class TextMatch:
    file_id: int
    path: str
    line: int
    snippet: str
    snippet_truncated: bool
    snippet_offset: int
    match_offset: int


@dataclass(frozen=True)
class SearchPage[T]:
    snapshot: Snapshot
    items: list[T]
    # 內容搜尋不計總數（成本等同最壞情況的全掃描），為 None
    total: int | None
    has_more: bool


@contextmanager
def _timeout_as_domain_error() -> Iterator[None]:
    """把 PostgreSQL 的取消查詢（SQLSTATE 57014）轉成領域例外。

    它是 `OperationalError` 的子類，不攔截的話會被 `api/errors.py` 當成資料庫不可用（503）。
    """
    try:
        yield
    except OperationalError as exc:
        if isinstance(exc.orig, psycopg.errors.QueryCanceled):
            raise SearchTimeoutError("The search exceeded its time limit.") from None
        raise


def _set_statement_timeout(session: Session, seconds: float) -> None:
    # SET LOCAL 不能綁定參數，改用 set_config(..., is_local => true)；交易結束即還原
    milliseconds = max(1, int(seconds * 1000))
    session.execute(select(func.set_config("statement_timeout", str(milliseconds), True)))


def _begin(session: Session, snapshot_id: int, settings: Settings) -> Snapshot:
    snapshot = get_completed_snapshot(session, snapshot_id)
    _set_statement_timeout(session, settings.search_statement_timeout_seconds)
    return snapshot


def _imported_conditions(snapshot_id: int, path_prefix: str | None) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        SnapshotFile.snapshot_id == snapshot_id,
        SnapshotFile.status == "imported",
    ]
    if path_prefix:
        conditions.append(SnapshotFile.path.startswith(path_prefix, autoescape=True))
    return conditions


def search_files(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    q: str,
    path_prefix: str | None,
    limit: int,
    offset: int,
) -> SearchPage[FileMatch]:
    """路徑含 `q` 的已匯入檔案（依 path 的位元組順序）。"""
    snapshot = _begin(session, snapshot_id, settings)
    conditions = [
        *_imported_conditions(snapshot_id, path_prefix),
        SnapshotFile.path.contains(q, autoescape=True),
    ]
    with _timeout_as_domain_error():
        total = session.scalar(select(func.count()).select_from(SnapshotFile).where(*conditions))
        rows = session.execute(
            select(SnapshotFile.id, SnapshotFile.path, SnapshotFile.byte_size)
            .where(*conditions)
            .order_by(SnapshotFile.path.collate("C"))
            .limit(limit)
            .offset(offset)
        ).all()
    total = total or 0
    items = [FileMatch(file_id, path, size) for file_id, path, size in rows]
    return SearchPage(snapshot, items, total, offset + len(items) < total)


def search_symbols(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    q: str,
    kind: str | None,
    language: str | None,
    limit: int,
    offset: int,
) -> SearchPage[SymbolMatch]:
    """名稱或完整名稱含 `q` 的符號；需要目前設定下 `completed` 的索引，否則 409。"""
    snapshot = _begin(session, snapshot_id, settings)
    index = index_queries.require_completed_index(session, snapshot_id, settings)
    conditions: list[ColumnElement[bool]] = [
        Symbol.snapshot_index_id == index.id,
        or_(
            Symbol.name.contains(q, autoescape=True),
            Symbol.qualified_name.contains(q, autoescape=True),
        ),
    ]
    if kind is not None:
        conditions.append(Symbol.kind == kind)
    if language is not None:
        conditions.append(Symbol.language == language)
    # 複合條件同 index_queries：符號只透過同一個索引內的 indexed_files 連到檔案
    joined = (
        select(Symbol, SnapshotFile.id, SnapshotFile.path)
        .join(
            IndexedFile,
            (IndexedFile.id == Symbol.indexed_file_id)
            & (IndexedFile.snapshot_index_id == Symbol.snapshot_index_id),
        )
        .join(SnapshotFile, SnapshotFile.id == IndexedFile.snapshot_file_id)
        .where(*conditions)
    )
    with _timeout_as_domain_error():
        total = (
            session.scalar(
                select(func.count()).select_from(
                    joined.with_only_columns(Symbol.id).order_by(None).subquery()
                )
            )
            or 0
        )
        rows = session.execute(
            joined.order_by(SnapshotFile.path.collate("C"), Symbol.start_line, Symbol.id)
            .limit(limit)
            .offset(offset)
        ).all()
    items = [
        SymbolMatch(
            symbol_id=symbol.id,
            kind=symbol.kind,
            language=symbol.language,
            name=symbol.name,
            qualified_name=symbol.qualified_name,
            file_id=file_id,
            path=path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            has_errors=symbol.has_errors,
        )
        for symbol, file_id, path in rows
    ]
    return SearchPage(snapshot, items, total, offset + len(items) < total)


def make_snippet(line: str, match_index: int) -> tuple[str, bool, int, int]:
    """從命中行取最多 300 字元的視窗（最多從命中前 100 字元起）。

    回傳 (片段, 是否被截斷, 視窗在該行中的字元位移, 命中在片段中的字元位移)；位移以 Unicode
    碼點計（Python 字串索引），不是 UTF-8 位元組。
    """
    if len(line) <= SNIPPET_MAX_CHARS:
        return line, False, 0, match_index
    start = max(0, match_index - SNIPPET_LEAD_CHARS)
    return line[start : start + SNIPPET_MAX_CHARS], True, start, match_index - start


def _matching_lines(content: str, q: str) -> Iterator[tuple[int, str, int]]:
    """逐行找出含 `q` 的行：(1-based 行號, 行內容, 第一個命中的字元位置)。

    行以 `\\n`／`\\r\\n` 分隔（行尾的 `\\r` 不屬於該行）；`q` 不含換行，所以命中不會跨行。
    """
    for number, raw in enumerate(content.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        index = line.find(q)
        if index >= 0:
            yield number, line, index


def _text_batches(candidates: Sequence[Any]) -> Iterator[list[Any]]:
    """把候選檔案切成批次：數量從 20 起每批加倍到 500，且每批（依 byte_size）最多約 8 MiB。

    單一檔案超過位元組上限時自成一批（匯入的單檔上限預設 1 MiB，實務上不會發生）。
    """
    position = 0
    batch_files = TEXT_BATCH_FIRST_FILES
    while position < len(candidates):
        batch: list[Any] = []
        batch_bytes = 0
        while position < len(candidates) and len(batch) < batch_files:
            size = candidates[position][2] or 0
            if batch and batch_bytes + size > TEXT_BATCH_MAX_BYTES:
                break
            batch.append(candidates[position])
            batch_bytes += size
            position += 1
        yield batch
        batch_files = min(batch_files * 2, TEXT_BATCH_MAX_FILES)


def search_text(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    q: str,
    path_prefix: str | None,
    limit: int,
    offset: int,
) -> SearchPage[TextMatch]:
    """內容含 `q` 的行（一個命中行一筆；依 path、行號）；不計總數，只回 `has_more`。

    兩步驟（規格 8.5）：先取候選檔案的 (id, path, byte_size)（不含內容，最多一個快照的檔案數）；
    再分批在資料庫端以 `LIKE` 過濾，只有命中的檔案才把內容取回。略過 `offset` 個命中行後湊滿
    `limit + 1` 個就提早結束（多的一個用來判斷 `has_more`）。**不先 ORDER BY 再過濾內容**：
    `COLLATE "C"` 排序用不到預設語系的索引，會迫使資料庫在回傳第一列前處理全部內容。
    多批次的總時間受同一個期限限制。
    """
    snapshot = _begin(session, snapshot_id, settings)
    deadline = time.monotonic() + settings.search_statement_timeout_seconds
    found: list[TextMatch] = []
    skipped = 0

    with _timeout_as_domain_error():
        candidates = session.execute(
            select(SnapshotFile.id, SnapshotFile.path, SnapshotFile.byte_size)
            .where(*_imported_conditions(snapshot_id, path_prefix))
            .order_by(SnapshotFile.path.collate("C"))
        ).all()

        for batch in _text_batches(candidates):
            if len(found) > limit:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SearchTimeoutError("The search exceeded its time limit.")
            _set_statement_timeout(session, remaining)
            contents = {
                file_id: content
                for file_id, content in session.execute(
                    select(SnapshotFile.id, SnapshotFile.content).where(
                        SnapshotFile.snapshot_id == snapshot_id,
                        SnapshotFile.id.in_([row[0] for row in batch]),
                        SnapshotFile.content.contains(q, autoescape=True),
                    )
                )
            }
            for file_id, path, _size in batch:
                content = contents.get(file_id)
                if content is None:
                    continue
                for number, line, index in _matching_lines(content, q):
                    if skipped < offset:
                        skipped += 1
                        continue
                    snippet, truncated, snippet_offset, match_offset = make_snippet(line, index)
                    found.append(
                        TextMatch(
                            file_id, path, number, snippet, truncated, snippet_offset, match_offset
                        )
                    )
                    if len(found) > limit:
                        break
                if len(found) > limit:
                    break

    has_more = len(found) > limit
    return SearchPage(snapshot, found[:limit], None, has_more)
