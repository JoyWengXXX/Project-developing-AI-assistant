"""結構索引的建立與維護（第二階段切片 6，規格 6、7）。

索引是快照的**衍生資料**：只讀資料庫裡 `snapshot_files` 的文字內容，不讀 Git、不執行受分析的程式。
索引失敗不使快照失效；`building`／`failed` 的索引不被任何查詢採用（查詢端只採用目前設定下
`completed` 的索引）。

生命週期與保護（規格 5.4、6.1）：

- **索引鎖**（`index_lock_key`，advisory lock，獨占）：同一快照在同一索引設定下只有一個建構者。
  持有它的人可以立即接手殘留的 `building`／`failed` 索引（刪除舊的子資料、重新建構）；活著的
  建構者一定持有這把鎖，所以不會接手到別人正在寫的索引。
- **資料面柵欄**（`snapshot_indexes.owner_token`）：建構期間 `owner_token` ＝ 建立它的 job 的
  `lease_token`。每一批寫入都在同一交易內先以 `FOR SHARE` 確認「我還是擁有者」，完成與標記失敗
  則是帶著令牌的條件式 `UPDATE`。即使租約邏輯有缺陷，舊 worker 也不能覆寫新 attempt 的索引。
- **完成交易**：呼叫端傳入 `finalize`，讓「索引 `completed`」與「job `succeeded`」在同一個交易內
  提交（與匯入的 `finalize` 同一個作法，規格 5.8）。
- **逐批**：以 `snapshot_files.id` 的 keyset 分頁讀取，每批最多 `INDEX_BATCH_MAX_FILES` 檔或約
  `INDEX_BATCH_MAX_BYTES` 位元組（依 `byte_size`）；記憶體只與批次大小成正比。每批寫入後呼叫
  `on_batch`（進度、取消與租約檢查）。
- **毒藥檔案**（`job_poison`，D43）：每個檔案解析前把它記為「目前單位」；同一檔案讓 worker 連續崩潰
  達門檻就略過，記為 `failed`／`parser_crash`，其他檔案照常索引。

每次嘗試都從頭建構（先刪除上一次留下的子資料）：資料量小到不值得為「續傳」增加狀態，
也避免半套索引被誤用。

.NET 對照：像一個以資料列版本（rowversion）當柵欄的批次匯入作業；advisory lock 相當於
`sp_getapplock`，防止兩個作業同時重建同一份資料。
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from psycopg import sql
from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import AdvisoryLock, index_lock_key
from repopilot.db.models import (
    ImportReference,
    IndexedFile,
    Snapshot,
    SnapshotFile,
    SnapshotIndex,
    Symbol,
    SymbolRelation,
)
from repopilot.errors import (
    ImportCancelledError,
    JobLeaseLostError,
    SnapshotNotCompletedError,
    SnapshotNotFoundError,
)
from repopilot.indexing.csharp_extractor import extract_csharp
from repopilot.indexing.profile import IndexProfile
from repopilot.indexing.python_extractor import extract_python
from repopilot.indexing.types import FileExtraction, Language
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import job_poison

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.snapshot_indexing")

ERROR_SUMMARY_LIMIT = 500
# 一次最多處理的 `IN (...)` 檔案 id 數（也是每批檔案數的上限，不依設定放大）
_SQL_BATCH_LIMIT = 1000

# 完成交易的接手者（`finalize(summary, complete) -> 是否已發布`）：worker 傳入 `complete_job` 的
# 包裝，讓「索引 completed」與「job succeeded」在同一個交易提交；回傳 False 代表使用者已取消，
# 索引不得發布
IndexFinalizer = Callable[["IndexSummary", Callable[[Session], None]], bool]
# 每批寫入後呼叫（已索引檔案數, 其中有解析問題的檔案數）；可以拋例外來中止（取消、租約遺失）
IndexProgress = Callable[[int, int], None]


@dataclass(frozen=True)
class IndexSummary:
    index_id: int
    reused: bool  # True＝目前設定的索引早已 completed，沒有做任何工作
    file_count: int
    symbol_count: int
    parse_error_count: int
    truncated_file_count: int
    skipped_file_count: int  # 因毒藥檔案框架略過（parser_crash）的檔案數

    def as_result(self) -> dict[str, Any]:
        """job `result` 的安全摘要（只有計數與 id）。"""
        return {
            "index_id": self.index_id,
            "index_reused": self.reused,
            "indexed_files": self.file_count,
            "symbols": self.symbol_count,
            "parse_error_files": self.parse_error_count,
            "truncated_files": self.truncated_file_count,
            "skipped_files": self.skipped_file_count,
        }


def language_of(path: str) -> Language | None:
    """副檔名（不分大小寫）決定語言；其他檔案不索引（規格 7.1）。`.pyi`、`.csx` 不在本階段。"""
    lowered = path.lower()
    if lowered.endswith(".py"):
        return "python"
    if lowered.endswith(".cs"):
        return "csharp"
    return None


def _candidate_filter(snapshot_id: int) -> list[ColumnElement[bool]]:
    lowered = func.lower(SnapshotFile.path)
    return [
        SnapshotFile.snapshot_id == snapshot_id,
        SnapshotFile.status == "imported",
        or_(lowered.like("%.py"), lowered.like("%.cs")),
    ]


def count_candidate_files(session: Session, snapshot_id: int) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(SnapshotFile).where(*_candidate_filter(snapshot_id))
        ).scalar_one()
    )


def find_index(session: Session, snapshot_id: int, index_profile_hash: str) -> SnapshotIndex | None:
    return session.execute(
        select(SnapshotIndex)
        .where(
            SnapshotIndex.snapshot_id == snapshot_id,
            SnapshotIndex.index_profile_hash == index_profile_hash,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def build_index(
    session: Session,
    snapshot_id: int,
    settings: Settings,
    *,
    owner_token: UUID,
    lock: AdvisoryLock,
    tracker: job_poison.PoisonTracker | None = None,
    on_total: Callable[[int], None] | None = None,
    on_batch: IndexProgress | None = None,
    finalize: IndexFinalizer | None = None,
) -> IndexSummary:
    """建立（或接手重建）快照在目前設定下的結構索引；回傳摘要。

    `lock` 是呼叫者**已持有**的索引鎖（鍵必須等於這份索引）。目前設定的索引早已 `completed` 時
    不做任何工作（`reused=True`），**也不呼叫 `finalize`**——呼叫端自己完成 job。
    `finalize` 回傳 False（使用者已取消）會拋 `ImportCancelledError`，索引標記為 `failed`。
    任何失敗都會把索引標記為 `failed`（`JobLeaseLostError` 例外：舊擁有者不得寫入任何東西）
    並讓例外往上傳。
    """
    profile = IndexProfile.from_settings(settings)
    profile_hash = profile.hash()
    if lock.key != index_lock_key(snapshot_id, profile_hash) or lock.shared:
        raise ValueError("The held lock is not the index lock of this snapshot.")
    snapshot = session.get(Snapshot, snapshot_id, populate_existing=True)
    if snapshot is None:
        session.rollback()
        raise SnapshotNotFoundError("Snapshot not found.")
    if snapshot.status != "completed":
        session.rollback()
        raise SnapshotNotCompletedError("The snapshot has not been imported completely.")

    started = time.monotonic()
    claimed = _claim_index(session, snapshot_id, profile, owner_token)
    if claimed.reused:
        logger.info("snapshot_index_reused", extra={"snapshot_id": snapshot_id})
        return IndexSummary(claimed.index_id, True, *claimed.counts, 0, 0)
    index_id = claimed.index_id

    try:
        total = count_candidate_files(session, snapshot_id)
        session.commit()
        if on_total is not None:
            on_total(total)
        summary = _build_batches(
            session, snapshot_id, index_id, total, settings, owner_token, tracker, on_batch
        )
        lock.assert_alive()  # 持鎖期間連線若被中斷，鎖已默默消失：發布前確認（規格 5.10）
        if finalize is None:
            _complete_index(session, index_id, owner_token, summary)
            session.commit()
        else:
            published = finalize(
                summary,
                lambda txn_session: _complete_index(txn_session, index_id, owner_token, summary),
            )
            if not published:
                raise ImportCancelledError("The index build was cancelled.")
    except JobLeaseLostError:
        raise  # 擁有者已換人：不得寫入任何東西（包括標記失敗）
    except Exception as exc:
        _mark_failed(session, index_id, owner_token, _summarize(exc))
        raise
    logger.info(
        "snapshot_index_completed",
        extra={
            "snapshot_id": snapshot_id,
            "index_id": index_id,
            "file_count": summary.file_count,
            "symbol_count": summary.symbol_count,
            "parse_error_files": summary.parse_error_count,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return summary


@dataclass(frozen=True)
class _Claimed:
    index_id: int
    reused: bool
    counts: tuple[int, int, int] = (0, 0, 0)  # file_count, symbol_count, parse_error_count


def _claim_index(
    session: Session, snapshot_id: int, profile: IndexProfile, owner_token: UUID
) -> _Claimed:
    """取得（或接手）索引列：已 completed 則重用；否則清掉舊的子資料、成為 `building` 的擁有者。

    呼叫者持有索引鎖，所以不會與另一個建構者競爭；列鎖只用來序列化與完成交易的競爭。
    """
    profile_hash = profile.hash()
    existing = session.execute(
        select(SnapshotIndex)
        .where(
            SnapshotIndex.snapshot_id == snapshot_id,
            SnapshotIndex.index_profile_hash == profile_hash,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    try:
        if existing is not None and existing.status == "completed":
            claimed = _Claimed(
                existing.id,
                True,
                (existing.file_count, existing.symbol_count, existing.parse_error_count),
            )
        elif existing is not None:
            # building（上一個建構者中斷）或 failed：丟掉殘留的子資料，重新建構
            session.execute(delete(IndexedFile).where(IndexedFile.snapshot_index_id == existing.id))
            session.execute(
                update(SnapshotIndex)
                .where(SnapshotIndex.id == existing.id)
                .values(
                    status="building",
                    owner_token=owner_token,
                    analyzers=profile.describe(),
                    symbol_count=0,
                    parse_error_count=0,
                    started_at=func.now(),
                    completed_at=None,
                    error_summary=None,
                )
            )
            claimed = _Claimed(existing.id, False)
        else:
            created = SnapshotIndex(
                snapshot_id=snapshot_id,
                index_profile_hash=profile_hash,
                status="building",
                owner_token=owner_token,
                analyzers=profile.describe(),
            )
            session.add(created)
            session.flush()
            claimed = _Claimed(created.id, False)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return claimed


def _next_batch(
    session: Session, snapshot_id: int, after_id: int, settings: Settings
) -> list[tuple[int, str, str]]:
    """下一批要索引的檔案（id, path, content）；沒有了回傳空列表。

    先只讀 `(id, path, byte_size)` 決定要取哪些（避免一次把 200 個檔案的內容全載入再丟掉），
    再以 id 讀內容。單一檔案超過位元組上限時獨占一批（它本來就受匯入的單檔上限約束）。
    """
    limit = min(settings.index_batch_max_files, _SQL_BATCH_LIMIT)
    rows = session.execute(
        select(SnapshotFile.id, SnapshotFile.path, SnapshotFile.byte_size)
        .where(*_candidate_filter(snapshot_id), SnapshotFile.id > after_id)
        .order_by(SnapshotFile.id)
        .limit(limit)
    ).all()
    chosen: list[tuple[int, str]] = []
    budget = settings.index_batch_max_bytes
    for file_id, path, byte_size in rows:
        size = byte_size or 0
        if chosen and size > budget:
            break
        chosen.append((file_id, path))
        budget -= size
    if not chosen:
        return []
    contents = dict(
        session.execute(
            select(SnapshotFile.id, SnapshotFile.content).where(
                SnapshotFile.id.in_([file_id for file_id, _ in chosen])
            )
        ).all()
    )
    return [(file_id, path, contents[file_id] or "") for file_id, path in chosen]


def _build_batches(
    session: Session,
    snapshot_id: int,
    index_id: int,
    total: int,
    settings: Settings,
    owner_token: UUID,
    tracker: job_poison.PoisonTracker | None,
    on_batch: IndexProgress | None,
) -> IndexSummary:
    files = symbols = error_files = truncated = skipped = 0
    after_id = 0
    position = 0
    while True:
        batch = _next_batch(session, snapshot_id, after_id, settings)
        if not batch:
            break
        after_id = batch[-1][0]
        extracted: list[tuple[int, str, FileExtraction]] = []
        for file_id, path, content in batch:
            if tracker is not None and tracker.should_skip(str(file_id)):
                tracker.skip(str(file_id))
                result = _crashed_file(language_of(path) or "python")
                skipped += 1
            else:
                if tracker is not None:
                    tracker.enter(
                        job_poison.WorkUnit(str(file_id), "file", "parser_crash", position, total)
                    )
                result = _extract(file_id, path, content, settings)
            extracted.append((file_id, path, result))
            position += 1
        written = _write_batch(session, index_id, owner_token, extracted)
        files += len(extracted)
        symbols += written
        error_files += sum(1 for _, _, r in extracted if r.parse_status != "parsed")
        truncated += sum(1 for _, _, r in extracted if r.symbols_truncated)
        if on_batch is not None:
            on_batch(files, error_files)
    return IndexSummary(index_id, False, files, symbols, error_files, truncated, skipped)


def _extract(file_id: int, path: str, content: str, settings: Settings) -> FileExtraction:
    language = language_of(path) or "python"
    try:
        if language == "csharp":
            return extract_csharp(
                path,
                content,
                max_symbols=settings.index_max_symbols_per_file,
                include_fields=settings.index_csharp_include_fields,
            )
        return extract_python(path, content, max_symbols=settings.index_max_symbols_per_file)
    except MemoryError:
        raise  # 記憶體耗盡是工作失敗，不偽裝成單檔錯誤（規格 7.1）
    except Exception as exc:
        # 抽取器本身的缺陷：只標記該檔，其他檔案照常索引；日誌只記型別與檔案 id，不含內容
        logger.warning(
            "index_extractor_error",
            extra={"snapshot_file_id": file_id, "error_type": type(exc).__name__},
        )
        return _crashed_file(language)


def _crashed_file(language: Language) -> FileExtraction:
    return FileExtraction(language, "failed", "parser_crash", 1)


def _allocate_ids(session: Session, table: str, count: int) -> list[int]:
    """預先配置主鍵：符號之間有父子關係，需要在 INSERT 前就知道 id（一個往返取得整批）。"""
    if count == 0:
        return []
    rows = session.execute(
        text(
            "SELECT nextval(pg_get_serial_sequence(:table, 'id')) FROM generate_series(1, :count)"
        ),
        {"table": table, "count": count},
    )
    return [int(value) for value in rows.scalars()]


def _write_batch(
    session: Session,
    index_id: int,
    owner_token: UUID,
    extracted: list[tuple[int, str, FileExtraction]],
) -> int:
    """寫入一批檔案的索引資料（單一交易）；回傳寫入的符號數。"""
    # 資料面柵欄：交易內先確認自己仍是這份索引的擁有者。FOR SHARE 會擋住並行的接手
    # （它要更新這一列），所以確認之後到提交之前擁有者不會被換掉
    owner = session.execute(
        select(SnapshotIndex.id)
        .where(
            SnapshotIndex.id == index_id,
            SnapshotIndex.status == "building",
            SnapshotIndex.owner_token == owner_token,
        )
        .with_for_update(read=True)
    ).first()
    if owner is None:
        session.rollback()
        raise JobLeaseLostError("The index is no longer owned by this worker.")

    file_ids = _allocate_ids(session, "indexed_files", len(extracted))
    symbol_ids = _allocate_ids(
        session, "symbols", sum(len(result.symbols) for _, _, result in extracted)
    )
    file_rows: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    import_rows: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    next_symbol = 0
    for indexed_file_id, (snapshot_file_id, _path, result) in zip(file_ids, extracted, strict=True):
        ids = symbol_ids[next_symbol : next_symbol + len(result.symbols)]
        next_symbol += len(result.symbols)
        file_rows.append(
            {
                "id": indexed_file_id,
                "snapshot_index_id": index_id,
                "snapshot_file_id": snapshot_file_id,
                "language": result.language,
                "parse_status": result.parse_status,
                "error_kind": result.error_kind,
                "error_count": result.error_count,
                "first_error_line": result.first_error_line,
                "first_error_column": result.first_error_column,
                "module_name": result.module_name,
                "symbol_count": len(result.symbols),
                "symbols_truncated": result.symbols_truncated,
            }
        )
        for symbol in result.symbols:
            symbol_rows.append(
                {
                    "id": ids[symbol.local_id],
                    "snapshot_index_id": index_id,
                    "indexed_file_id": indexed_file_id,
                    "parent_symbol_id": (
                        None if symbol.parent_local_id is None else ids[symbol.parent_local_id]
                    ),
                    "language": result.language,
                    "kind": symbol.kind,
                    "name": symbol.name,
                    "qualified_name": symbol.qualified_name,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "start_column": symbol.start_column,
                    "end_column": symbol.end_column,
                    "leading_start_line": symbol.leading_start_line,
                    "modifiers": symbol.modifiers,
                    "is_async": symbol.is_async,
                    "type_parameter_count": symbol.type_parameter_count,
                    "has_errors": symbol.has_errors,
                    "conditional": symbol.conditional,
                }
            )
        for ref in result.imports:
            import_rows.append(
                {
                    "snapshot_index_id": index_id,
                    "indexed_file_id": indexed_file_id,
                    "source_symbol_id": (
                        None if ref.source_local_id is None else ids[ref.source_local_id]
                    ),
                    "language": result.language,
                    "kind": ref.kind,
                    "target": ref.target,
                    "imported_name": ref.imported_name,
                    "alias": ref.alias,
                    "relative_level": ref.relative_level,
                    "line": ref.line,
                }
            )
        for relation in result.relations:
            relation_rows.append(
                {
                    "snapshot_index_id": index_id,
                    "source_symbol_id": ids[relation.source_local_id],
                    "relation_type": relation.relation_type,
                    "target_name": relation.target_name,
                    "target_symbol_id": (
                        None if relation.target_local_id is None else ids[relation.target_local_id]
                    ),
                    "resolution": relation.resolution,
                    "line": relation.line,
                }
            )
    try:
        # 依外鍵順序：檔案 → 符號（父在前；同一批內的父子由資料庫在語句結束時檢查）→ 其餘
        _bulk_insert(session, IndexedFile, file_rows)
        _bulk_insert(session, Symbol, symbol_rows)
        _bulk_insert(session, ImportReference, import_rows)
        _bulk_insert(session, SymbolRelation, relation_rows)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return len(symbol_rows)


def _bulk_insert(session: Session, model: type[Any], rows: list[dict[str, Any]]) -> None:
    """以 PostgreSQL `COPY` 一次寫入一大批列（同一個交易、同一條連線）。

    不用 `session.execute(insert(Model), rows)`（executemany：沒有 RETURNING 時逐列一個語句）也不用
    多列 `INSERT ... VALUES`（SQLAlchemy 要為每個參數編譯一個 bindparam）：單次量測 400 個檔案、
    32,800 個符號，前者 43 秒、後者 31 秒，時間幾乎都在往返或編譯；`COPY` 沒有這兩種成本。
    列的順序保留；外鍵與唯一約束照常在語句結束時檢查，所以同一批內的父子符號不受順序影響。
    表名與欄位名來自模型（不是使用者輸入），仍以 `psycopg.sql.Identifier` 引用。
    """
    if not rows:
        return
    table = model.__table__
    columns = list(rows[0])
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table.name), sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    )
    raw = session.connection().connection.driver_connection
    if raw is None:  # 不應發生：交易中的連線一定存在
        raise RuntimeError("The database connection is not available.")
    with raw.cursor() as cursor, cursor.copy(statement) as copy:
        for row in rows:
            copy.write_row([row[c] for c in columns])


def _complete_index(
    session: Session, index_id: int, owner_token: UUID, summary: IndexSummary
) -> None:
    """索引的完成寫入（**不 commit**，由呼叫者的交易一起提交）；以擁有者令牌確認仍擁有它。"""
    result = cast(
        CursorResult[Any],
        session.execute(
            update(SnapshotIndex)
            .where(
                SnapshotIndex.id == index_id,
                SnapshotIndex.status == "building",
                SnapshotIndex.owner_token == owner_token,
            )
            .values(
                status="completed",
                owner_token=None,
                file_count=summary.file_count,
                symbol_count=summary.symbol_count,
                parse_error_count=summary.parse_error_count,
                completed_at=func.now(),
                error_summary=None,
            )
        ),
    )
    if result.rowcount != 1:
        raise JobLeaseLostError("The index is no longer owned by this worker.")


def _summarize(exc: Exception) -> str:
    """固定的安全摘要：只含例外型別，不含訊息（可能夾帶路徑或內容）。"""
    if isinstance(exc, ImportCancelledError):
        return "The job was cancelled."
    return f"Indexing failed ({type(exc).__name__})."[:ERROR_SUMMARY_LIMIT]


def _mark_failed(session: Session, index_id: int, owner_token: UUID, summary: str) -> None:
    """把仍屬於自己的 `building` 索引標記為 `failed` 並盡力清掉子資料（失敗只記日誌）。"""
    try:
        session.rollback()
        result = cast(
            CursorResult[Any],
            session.execute(
                update(SnapshotIndex)
                .where(
                    SnapshotIndex.id == index_id,
                    SnapshotIndex.status == "building",
                    SnapshotIndex.owner_token == owner_token,
                )
                .values(status="failed", owner_token=None, error_summary=summary)
            ),
        )
        if result.rowcount == 1:
            session.execute(delete(IndexedFile).where(IndexedFile.snapshot_index_id == index_id))
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("snapshot_index_mark_failed_error", extra={"index_id": index_id})


# 移除 repo／快照時，符號數達到這個值才分批清除索引資料（否則直接靠外鍵級聯，秒級）。單次量測：
# 每個符號的外鍵級聯檢查約 40 微秒（164 萬個符號 ≈ 66 秒），10 萬個約 4 秒；數值未調校
PURGE_THRESHOLD_SYMBOLS = 100_000
# 每批刪除的符號數（每批各自一個短交易，約 1 秒）
PURGE_BATCH_SYMBOLS = 20_000


def count_symbols(session: Session, snapshot_ids: list[int]) -> int:
    """這些快照的索引符號總數（用 `snapshot_indexes.symbol_count`，不掃描 `symbols`）。"""
    if not snapshot_ids:
        return 0
    return int(
        session.scalar(
            select(func.coalesce(func.sum(SnapshotIndex.symbol_count), 0)).where(
                SnapshotIndex.snapshot_id.in_(snapshot_ids)
            )
        )
        or 0
    )


def purge_index_data(
    session: Session, snapshot_ids: list[int], *, batch_size: int | None = None
) -> None:
    """分批清除這些快照的全部結構索引資料（含索引列）；**會 commit**，結束時沒有未完成的交易。

    大型索引被外鍵級聯刪除時，單一交易會持有 repo 資料列鎖很久（D33 的既有取捨）。分批的作法：

    1. 先把索引標成 `failed`（`owner_token` 清空）並 commit——查詢端只採用 `completed` 的索引，
       所以之後不論哪一步中斷，都不會有「看起來完成、其實缺資料」的索引；呼叫者先前取得的
       列鎖也在這次 commit 釋放。建構中的舊 worker 下一批寫入會因擁有者不符而失敗。
    2. 關係與 import（筆數少、刪得快）各一個語句；符號每批 `batch_size` 個、各自一個短交易
       （符號的外鍵級聯檢查是主要成本，分批不會更快，但不會長時間持有任何鎖）。
    3. 最後刪除檔案與索引列。

    中途失敗：索引維持 `failed`、剩下的資料之後由重試移除（呼叫端的移除流程可安全重做）。
    呼叫者須已持有擋住其他建構者的互斥（repo 獨占鎖或快照鎖）。
    """
    if not snapshot_ids:
        return
    batch = batch_size or PURGE_BATCH_SYMBOLS
    index_ids = select(SnapshotIndex.id).where(SnapshotIndex.snapshot_id.in_(snapshot_ids))
    session.execute(
        update(SnapshotIndex)
        .where(SnapshotIndex.snapshot_id.in_(snapshot_ids))
        .values(status="failed", owner_token=None, error_summary="The index is being removed.")
    )
    session.commit()
    session.execute(delete(SymbolRelation).where(SymbolRelation.snapshot_index_id.in_(index_ids)))
    session.execute(delete(ImportReference).where(ImportReference.snapshot_index_id.in_(index_ids)))
    session.commit()
    while True:
        deleted = cast(
            CursorResult[Any],
            session.execute(
                delete(Symbol).where(
                    Symbol.id.in_(
                        select(Symbol.id)
                        .where(Symbol.snapshot_index_id.in_(index_ids))
                        .limit(batch)
                    )
                )
            ),
        ).rowcount
        session.commit()
        if deleted == 0:
            break
    session.execute(delete(IndexedFile).where(IndexedFile.snapshot_index_id.in_(index_ids)))
    session.execute(delete(SnapshotIndex).where(SnapshotIndex.snapshot_id.in_(snapshot_ids)))
    session.commit()
    logger.info("snapshot_index_purged", extra={"snapshot_count": len(snapshot_ids)})


def delete_outdated_indexes(session: Session, snapshot_id: int, keep_hash: str) -> int:
    """新索引完成後，清掉同一快照其他設定的舊索引（不動 `building` 者：可能正被別的 job 建構）。

    收尾步驟：失敗只記日誌，不影響 job 成功（規格 6.4）。回傳刪除的索引數。
    """
    try:
        result = cast(
            CursorResult[Any],
            session.execute(
                delete(SnapshotIndex).where(
                    SnapshotIndex.snapshot_id == snapshot_id,
                    SnapshotIndex.index_profile_hash != keep_hash,
                    SnapshotIndex.status != "building",
                )
            ),
        )
        session.commit()
        return int(result.rowcount)
    except Exception:
        session.rollback()
        logger.warning("snapshot_index_cleanup_failed", extra={"snapshot_id": snapshot_id})
        return 0
