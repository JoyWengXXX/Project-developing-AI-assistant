"""結構索引的唯讀查詢（第二階段切片 6，規格 4.7）。

只採用**目前設定**下 `completed` 的索引（`building`／`failed`／其他設定的索引一律不採用）：
所有查詢先經過 `require_completed_index`，並以 `snapshot_index_id` 限定範圍（歸屬檢查），所以
符號永遠不會跨索引、跨快照出現。與建構索引的 `snapshot_indexing.py` 分開（同 `snapshot_queries` 與
`snapshots` 的分法）。

排序固定為 `path COLLATE "C"`、`start_line`、`id`：分頁穩定，且不受資料庫語系影響。
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Select, case, exists, func, select
from sqlalchemy.orm import Session, aliased

from repopilot.config import Settings
from repopilot.db.models import (
    ImportReference,
    IndexedFile,
    SnapshotFile,
    SnapshotIndex,
    Symbol,
    SymbolRelation,
)
from repopilot.errors import SnapshotIndexNotReadyError, SymbolNotFoundError
from repopilot.indexing.profile import IndexProfile
from repopilot.services import snapshot_indexing
from repopilot.services.snapshot_queries import get_completed_snapshot

MAX_DETAIL_IMPORTS = 200


@dataclass(frozen=True)
class IndexCounts:
    files_by_language: dict[str, int]
    files_by_parse_status: dict[str, int]
    symbols: int
    symbols_by_kind: dict[str, int]
    truncated_files: int


@dataclass(frozen=True)
class SymbolRow:
    symbol: Symbol
    file_id: int
    path: str
    has_children: bool


def get_current_index(
    session: Session, snapshot_id: int, settings: Settings
) -> SnapshotIndex | None:
    """快照（須 completed）在目前設定下的索引；沒有則 `None`（任何狀態都回傳，由呼叫端判斷）。"""
    get_completed_snapshot(session, snapshot_id)
    return snapshot_indexing.find_index(
        session, snapshot_id, IndexProfile.from_settings(settings).hash()
    )


def require_completed_index(
    session: Session, snapshot_id: int, settings: Settings
) -> SnapshotIndex:
    index = get_current_index(session, snapshot_id, settings)
    if index is None or index.status != "completed":
        raise SnapshotIndexNotReadyError(
            "The snapshot has no completed structure index.",
            index_status="none" if index is None else index.status,
        )
    return index


def count_index(session: Session, index: SnapshotIndex) -> IndexCounts:
    file_rows = session.execute(
        select(
            IndexedFile.language,
            IndexedFile.parse_status,
            func.count(),
            func.count().filter(IndexedFile.symbols_truncated),
        )
        .where(IndexedFile.snapshot_index_id == index.id)
        .group_by(IndexedFile.language, IndexedFile.parse_status)
    ).all()
    by_language: dict[str, int] = {}
    by_status: dict[str, int] = {}
    truncated = 0
    for language, parse_status, count, truncated_count in file_rows:
        by_language[language] = by_language.get(language, 0) + count
        by_status[parse_status] = by_status.get(parse_status, 0) + count
        truncated += truncated_count
    symbol_rows = session.execute(
        select(Symbol.kind, func.count())
        .where(Symbol.snapshot_index_id == index.id)
        .group_by(Symbol.kind)
    ).all()
    by_kind = {kind: count for kind, count in symbol_rows}
    return IndexCounts(by_language, by_status, sum(by_kind.values()), by_kind, truncated)


def list_indexed_files(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    has_problems: bool,
    language: str | None,
    limit: int,
    offset: int,
) -> tuple[list[tuple[IndexedFile, str, int]], int]:
    """分頁列出被索引的檔案；`has_problems` 只列 `parsed_with_errors`／`failed`。

    回傳 ((索引檔案, 路徑, snapshot_file_id) 列表, 符合條件的總數)。
    """
    index = require_completed_index(session, snapshot_id, settings)
    conditions: list[ColumnElement[bool]] = [IndexedFile.snapshot_index_id == index.id]
    if has_problems:
        conditions.append(IndexedFile.parse_status != "parsed")
    if language is not None:
        conditions.append(IndexedFile.language == language)
    total = session.scalar(select(func.count()).select_from(IndexedFile).where(*conditions)) or 0
    rows = session.execute(
        select(IndexedFile, SnapshotFile.path, SnapshotFile.id)
        .join(SnapshotFile, SnapshotFile.id == IndexedFile.snapshot_file_id)
        .where(*conditions)
        .order_by(SnapshotFile.path.collate("C"), IndexedFile.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows], total


def _symbol_statement(index_id: int) -> Select[Any]:
    child = aliased(Symbol)
    has_children = exists().where(
        child.parent_symbol_id == Symbol.id, child.snapshot_index_id == Symbol.snapshot_index_id
    )
    return (
        select(Symbol, SnapshotFile.id, SnapshotFile.path, case((has_children, True), else_=False))
        .join(
            IndexedFile,
            (IndexedFile.id == Symbol.indexed_file_id)
            & (IndexedFile.snapshot_index_id == Symbol.snapshot_index_id),
        )
        .join(SnapshotFile, SnapshotFile.id == IndexedFile.snapshot_file_id)
        .where(Symbol.snapshot_index_id == index_id)
    )


def list_symbols(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    kind: str | None,
    parent_id: int | None,
    top_level: bool,
    file_id: int | None,
    path_prefix: str | None,
    limit: int,
    offset: int,
) -> tuple[list[SymbolRow], int]:
    """分頁列出符號。`parent_id`（直接子符號）與 `top_level`（沒有父符號者，即各檔的模組）
    是符號樹的兩種入口；兩者同時指定時沒有符號符合。
    """
    index = require_completed_index(session, snapshot_id, settings)
    conditions: list[ColumnElement[bool]] = []
    if kind is not None:
        conditions.append(Symbol.kind == kind)
    if parent_id is not None:
        conditions.append(Symbol.parent_symbol_id == parent_id)
    if top_level:
        conditions.append(Symbol.parent_symbol_id.is_(None))
    if file_id is not None:
        conditions.append(SnapshotFile.id == file_id)
    if path_prefix:
        # autoescape：使用者輸入中的 % 與 _ 視為字面字元，而非 LIKE 萬用字元
        conditions.append(SnapshotFile.path.startswith(path_prefix, autoescape=True))

    count_statement = (
        select(func.count())
        .select_from(Symbol)
        .join(
            IndexedFile,
            (IndexedFile.id == Symbol.indexed_file_id)
            & (IndexedFile.snapshot_index_id == Symbol.snapshot_index_id),
        )
        .join(SnapshotFile, SnapshotFile.id == IndexedFile.snapshot_file_id)
        .where(Symbol.snapshot_index_id == index.id, *conditions)
    )
    total = session.scalar(count_statement) or 0
    statement = _symbol_statement(index.id).where(*conditions)
    rows = session.execute(
        statement.order_by(SnapshotFile.path.collate("C"), Symbol.start_line, Symbol.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        SymbolRow(symbol, file_id, path, bool(has_children))
        for symbol, file_id, path, has_children in rows
    ], total


def get_symbol_detail(
    session: Session, snapshot_id: int, settings: Settings, symbol_id: int
) -> tuple[SymbolRow, list[ImportReference], list[SymbolRelation]]:
    """單一符號與它的關係、同檔的 import；符號不在該快照目前的索引內回 404。"""
    index = require_completed_index(session, snapshot_id, settings)
    row = session.execute(_symbol_statement(index.id).where(Symbol.id == symbol_id)).first()
    if row is None:
        raise SymbolNotFoundError("Symbol not found in this snapshot's index.")
    symbol_obj, file_id, path, has_children = row
    symbol = SymbolRow(symbol_obj, file_id, path, bool(has_children))
    imports = list(
        session.scalars(
            select(ImportReference)
            .where(
                ImportReference.snapshot_index_id == index.id,
                ImportReference.indexed_file_id == symbol.symbol.indexed_file_id,
            )
            .order_by(ImportReference.line, ImportReference.id)
            .limit(MAX_DETAIL_IMPORTS)
        )
    )
    relations = list(
        session.scalars(
            select(SymbolRelation)
            .where(
                SymbolRelation.snapshot_index_id == index.id,
                SymbolRelation.source_symbol_id == symbol_id,
            )
            .order_by(SymbolRelation.line, SymbolRelation.id)
        )
    )
    return symbol, imports, relations
