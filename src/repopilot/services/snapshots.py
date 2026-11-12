import hashlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import AdvisoryLock, engine_of, snapshot_lock_key, try_advisory_lock
from repopilot.db.models import Repository, Snapshot, SnapshotFile
from repopilot.errors import (
    GitCommandError,
    ImportCancelledError,
    ImportLimitExceededError,
    InvalidRepositoryPathError,
    RepoPilotError,
    RepositoryNotFoundError,
    SnapshotImportInProgressError,
)
from repopilot.git.objects import TreeEntry, list_tree, read_blobs, resolve_commit
from repopilot.git.repository import ensure_repository_root
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services.import_profile import ImportProfile
from repopilot.services.repositories import get_repository, resolve_registered_path

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.snapshots")

SNAPSHOT_UNIQUE_CONSTRAINT = "uq_snapshots_repository_id_commit_sha_import_profile_hash"
REPOSITORY_FOREIGN_KEY = "fk_snapshots_repository_id_repositories"
ERROR_SUMMARY_LIMIT = 500
INSERT_BATCH_SIZE = 1000
# 快照內路徑的長度上限（UTF-8 位元組，即存入資料庫的形式；D37）。`(snapshot_id, path)` 的 btree
# 唯一索引列上限實測為 2,688 位元組（PostgreSQL 17、8 KiB 區塊、不可壓縮的路徑），超過會被資料庫
# 拒絕。2,048 在其下留約 640 位元組餘裕；一般 repo 的路徑遠短於此（未做統計）。不放進設定也不進
# import_profile_hash：這是資料庫的硬限制，不是使用者政策。
MAX_PATH_BYTES = 2048
_UNSAFE_SEGMENTS = frozenset({"", ".", "..", ".git"})
_MAX_ACQUIRE_ATTEMPTS = 3

FileRecord = dict[str, Any]
# 進度回呼（processed_files, total_files）：只供 worker 的 job 使用；回呼可以拋例外來中止
# 匯入（例如使用者要求取消、租約已遺失），匯入服務會把快照標記為 failed 後讓例外繼續往上傳
ProgressCallback = Callable[[int, int | None], None]
# 完成交易的接手者（`finalize(snapshot_id, complete) -> 是否已發布`）：worker 傳入
# `complete_job` 的包裝，讓「快照 completed＋檔案列」與「job succeeded」在**同一個交易**提交
# （規格 5.8 的提交前檢查點）。`complete(session)` 做快照的完成寫入（不 commit）；
# 回傳 False 代表使用者已取消、快照不得發布
Finalizer = Callable[[int, Callable[[Session], None]], bool]


def import_snapshot(
    session: Session,
    repository_id: int,
    revision: str,
    settings: Settings,
    *,
    path: Path | None = None,
    takeover: AdvisoryLock | None = None,
    on_progress: ProgressCallback | None = None,
    finalize: Finalizer | None = None,
) -> tuple[Snapshot, bool]:
    """匯入 repo 在指定 revision 的固定版本快照；回傳 (Snapshot, 是否本次新建完成)。

    只讀取 Git 物件，不讀工作目錄。相同 (repo, commit, 匯入設定) 已完成時直接回傳既有快照。

    **快照鎖（規格 5.10）**：匯入同一快照的所有呼叫者（同步端點與 worker）以 advisory lock
    互斥。同步路徑（`takeover=None`）自己取鎖，取不到回 `SnapshotImportInProgressError`
    （409，與 D3 一致）。

    後四個是 worker 專用的選用參數，預設 `None` 時行為與第一階段完全相同：
    - `path`：worker 已在容器內驗證過的 repo 路徑；省略則依 D28 重新驗證資料庫中的主機路徑。
    - `takeover`：呼叫者**已持有的**該快照的快照鎖。活著的匯入者一定持有這把鎖，所以持鎖者
      可以立即接手殘留的 `importing` 快照（不必等 `IMPORT_STALE_SECONDS`，規格 5.4）；鎖的
      鍵必須與這次要匯入的快照一致，發布前會確認鎖連線仍存活。被接手的舊寫入者仍會在完成時
      因 `started_at` 不符而失敗，不會產生半套資料。
    - `on_progress`、`finalize`：見型別說明。
    """
    started = time.monotonic()
    repository = get_repository(session, repository_id)
    # 重新驗證路徑（D28）：允許的根目錄依 source_type 而定（local→REPOSITORY_ROOTS，
    # github→MANAGED_REPOS_DIR 內的固定位置）；只涉及檔案系統，不執行 Git
    if path is None:
        path = resolve_registered_path(repository, settings)
    # 結束讀取交易，避免之後的 Git 作業期間持有閒置連線
    session.commit()

    try:
        ensure_repository_root(path, timeout=settings.git_timeout_seconds)
        commit_sha = resolve_commit(path, revision, timeout=settings.git_timeout_seconds)
    except (GitCommandError, InvalidRepositoryPathError):
        # 與「移除 repo」競爭：GitHub 來源的 clone 目錄可能剛被刪掉（D33）
        raise_if_repository_removed(session, repository_id)
        raise
    profile = ImportProfile.from_settings(settings)
    profile_hash = profile.hash()
    key = snapshot_lock_key(repository_id, commit_sha, profile_hash)

    if takeover is not None:
        if takeover.key != key or takeover.shared:
            raise ValueError("The held lock is not the snapshot lock of this import.")
        return _import_with_lock(
            session,
            repository_id,
            commit_sha,
            path,
            profile,
            profile_hash,
            settings,
            started,
            lock=takeover,
            takeover=True,
            on_progress=on_progress,
            finalize=finalize,
        )

    # 同步路徑：已完成的快照直接重用，不必取鎖（與第一階段相同：重用不會因為別人持鎖而變 409）
    existing = find_snapshot(session, repository_id, commit_sha, profile_hash)
    session.rollback()
    if existing is not None and existing.status == "completed":
        logger.info("snapshot_import_reused", extra={"snapshot_id": existing.id})
        return existing, False
    with try_advisory_lock(engine_of(session), key) as lock:
        if lock is None:
            raise SnapshotImportInProgressError("The same snapshot is already being imported.")
        return _import_with_lock(
            session,
            repository_id,
            commit_sha,
            path,
            profile,
            profile_hash,
            settings,
            started,
            lock=lock,
            takeover=False,
            on_progress=on_progress,
            finalize=finalize,
        )


def _import_with_lock(
    session: Session,
    repository_id: int,
    commit_sha: str,
    path: Path,
    profile: ImportProfile,
    profile_hash: str,
    settings: Settings,
    started: float,
    *,
    lock: AdvisoryLock,
    takeover: bool,
    on_progress: ProgressCallback | None,
    finalize: Finalizer | None,
) -> tuple[Snapshot, bool]:
    snapshot, owned = _acquire_snapshot(
        session,
        repository_id,
        commit_sha,
        profile_hash,
        settings.import_stale_seconds,
        takeover=takeover,
    )
    if not owned:
        logger.info("snapshot_import_reused", extra={"snapshot_id": snapshot.id})
        return snapshot, False

    snapshot_id = snapshot.id
    token = snapshot.started_at
    try:
        if on_progress is None:
            records, stats = _build_records(path, commit_sha, profile, settings)
        else:
            records, stats = _build_records(
                path, commit_sha, profile, settings, on_progress=on_progress
            )
        lock.assert_alive()  # 持鎖期間連線若被中斷，鎖已默默消失：發布前確認（規格 5.10）
        if finalize is None:
            _complete_snapshot(session, snapshot_id, token, records, stats)
        else:
            published = finalize(
                snapshot_id,
                lambda txn_session: _write_completion(
                    txn_session, snapshot_id, token, records, stats
                ),
            )
            if not published:
                raise ImportCancelledError("The import was cancelled.")
    except SnapshotImportInProgressError:
        # 所有權已被接手；不得覆寫對方的狀態
        raise
    except Exception as exc:
        _mark_failed(session, snapshot_id, token, _summarize(exc))
        raise

    completed = session.get(Snapshot, snapshot_id, populate_existing=True)
    if completed is None:  # 不應發生：快照剛完成提交
        raise RuntimeError("Snapshot disappeared after import.")
    logger.info(
        "snapshot_import_completed",
        extra={
            "repository_id": repository_id,
            "snapshot_id": snapshot_id,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "file_count": completed.file_count,
            "skipped_count": completed.skipped_count,
        },
    )
    return completed, True


def _utcnow() -> datetime:
    return datetime.now(UTC)


def raise_if_repository_removed(session: Session, repository_id: int) -> None:
    """repo 已被移除（或正在移除）時改回 404，而不是 Git 或路徑錯誤。

    `FOR SHARE` 會等到持有 `FOR UPDATE` 的移除交易結束：移除成功 → 查無資料列 → 404；
    移除失敗而 rollback → 資料列仍在 → 呼叫端照原樣拋出原本的錯誤。
    """
    found = session.scalar(
        select(Repository.id).where(Repository.id == repository_id).with_for_update(read=True)
    )
    session.rollback()
    if found is None:
        raise RepositoryNotFoundError("Repository not found.") from None


def find_snapshot(
    session: Session, repository_id: int, commit_sha: str, profile_hash: str
) -> Snapshot | None:
    return session.scalars(
        select(Snapshot)
        .where(
            Snapshot.repository_id == repository_id,
            Snapshot.commit_sha == commit_sha,
            Snapshot.import_profile_hash == profile_hash,
        )
        .execution_options(populate_existing=True)
    ).first()


def _acquire_snapshot(
    session: Session,
    repository_id: int,
    commit_sha: str,
    profile_hash: str,
    stale_seconds: int,
    *,
    takeover: bool = False,
) -> tuple[Snapshot, bool]:
    """取得匯入所有權；回傳 (快照, 是否由本請求擁有)。

    唯一性由資料庫約束仲裁：先嘗試 INSERT，衝突後再依既有狀態決定回傳、拒絕或接手。
    `takeover=True`（呼叫者持有快照鎖）時，任何 `importing` 都視為可接手：活著的匯入者一定持有鎖，
    所以殘留的 `importing` 必然是已中斷的匯入，不必等 `stale_seconds`。
    """
    for _ in range(_MAX_ACQUIRE_ATTEMPTS):
        snapshot = Snapshot(
            repository_id=repository_id,
            commit_sha=commit_sha,
            import_profile_hash=profile_hash,
            status="importing",
            started_at=_utcnow(),
        )
        session.add(snapshot)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if REPOSITORY_FOREIGN_KEY in str(exc.orig):
                # 匯入期間 repo 被移除（D33）：移除交易提交後，外鍵檢查才失敗
                raise RepositoryNotFoundError("Repository not found.") from None
            if SNAPSHOT_UNIQUE_CONSTRAINT not in str(exc.orig):
                raise
        else:
            return snapshot, True

        existing = find_snapshot(session, repository_id, commit_sha, profile_hash)
        if existing is None:  # 衝突後該列被刪除；重新嘗試建立
            continue
        if existing.status == "completed":
            return existing, False
        # takeover 時 cutoff 為 None：不比較 started_at（也不受主機與容器的時鐘差影響）
        cutoff = None if takeover else _utcnow() - timedelta(seconds=stale_seconds)
        if cutoff is not None and existing.status == "importing" and existing.started_at >= cutoff:
            raise SnapshotImportInProgressError("The same snapshot is already being imported.")
        claimed = _claim_snapshot(session, existing.id, cutoff)
        if claimed is not None:
            return claimed, True
    raise SnapshotImportInProgressError("The same snapshot is already being imported.")


def _claim_snapshot(
    session: Session, snapshot_id: int, stale_cutoff: datetime | None
) -> Snapshot | None:
    """以條件式 UPDATE 接手 failed 或逾時的 importing 快照；影響列數為 0 表示被他人搶先。

    `stale_cutoff=None` 是 takeover：任何 `importing` 都可接手（呼叫者已持有快照鎖）。
    """
    importing_is_claimable = (
        Snapshot.status == "importing"
        if stale_cutoff is None
        else (Snapshot.status == "importing") & (Snapshot.started_at < stale_cutoff)
    )
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Snapshot)
            .where(
                Snapshot.id == snapshot_id,
                or_(Snapshot.status == "failed", importing_is_claimable),
            )
            .values(
                status="importing",
                started_at=_utcnow(),
                completed_at=None,
                error_summary=None,
                file_count=0,
                skipped_count=0,
                total_bytes=0,
            )
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    # 防禦性清除殘留檔案列；正常流程失敗時不會留下任何檔案
    session.execute(delete(SnapshotFile).where(SnapshotFile.snapshot_id == snapshot_id))
    session.commit()
    return session.get(Snapshot, snapshot_id, populate_existing=True)


def _skipped(entry: TreeEntry, reason: str, *, blob: bool = True) -> FileRecord:
    return {
        "path": entry.path,
        "git_blob_id": entry.object_id if blob else None,
        "sha256": None,
        "byte_size": entry.size if blob else None,
        "content": None,
        "status": "skipped",
        "skip_reason": reason,
    }


def _classify_without_reading(entry: TreeEntry, profile: ImportProfile) -> FileRecord | None:
    """不需讀取內容即可判定者回傳 skipped 紀錄；需讀取內容的一般檔案回傳 None。"""
    if any(segment in _UNSAFE_SEGMENTS for segment in entry.path.split("/")):
        return _skipped(entry, "unsafe_path", blob=False)
    if entry.mode == "160000" or entry.object_type == "commit":
        return _skipped(entry, "submodule", blob=False)
    if entry.mode == "120000":
        return _skipped(entry, "symlink")
    if entry.object_type != "blob" or not entry.mode.startswith("100"):
        return _skipped(entry, "unsupported", blob=False)
    if profile.is_excluded(entry.path):
        return _skipped(entry, "excluded")
    if entry.size is not None and entry.size > profile.max_file_bytes:
        return _skipped(entry, "too_large")
    return None


def _classify_content(entry: TreeEntry, data: bytes) -> FileRecord:
    record: FileRecord = {
        "path": entry.path,
        "git_blob_id": entry.object_id,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
        "content": None,
        "status": "skipped",
        "skip_reason": None,
    }
    # PostgreSQL text 不接受 NUL，因此檢查整個內容而非僅前 8000 bytes
    if b"\x00" in data:
        record["skip_reason"] = "binary"
        return record
    try:
        record["content"] = data.decode("utf-8")
    except UnicodeDecodeError:
        record["skip_reason"] = "invalid_utf8"
        return record
    record["status"] = "imported"
    return record


def _build_records(
    path: Path,
    commit_sha: str,
    profile: ImportProfile,
    settings: Settings,
    *,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[FileRecord], dict[str, int]]:
    entries = sorted(
        list_tree(path, commit_sha, timeout=settings.git_timeout_seconds),
        key=lambda entry: entry.path,
    )
    if len(entries) > profile.max_files:
        raise ImportLimitExceededError(
            f"The snapshot has {len(entries)} files; the limit is {profile.max_files}."
        )

    # 每個項目（含被跳過者）的路徑都會寫進 (snapshot_id, path) 唯一索引，所以過長的路徑
    # 無法以 skipped 保存；在讀取任何 blob 之前就讓整個匯入失敗（D37）。訊息不含路徑本身
    long_paths = [
        size for entry in entries if (size := len(entry.path.encode("utf-8"))) > MAX_PATH_BYTES
    ]
    if long_paths:
        raise ImportLimitExceededError(
            f"The snapshot has {len(long_paths)} path(s) longer than {MAX_PATH_BYTES} bytes "
            f"(longest: {max(long_paths)} bytes)."
        )

    # 進度回報（只有 job 路徑會傳回呼）：`ls-tree` 完成後才知道總數；匯入的 blob 讀取是單一
    # `cat-file --batch`（D23），無法逐檔回報，所以只在三個點回報：總數已知、不需讀取者分類
    # 完成、全部完成。回呼也是取消與租約遺失的檢查點（它可以拋例外來中止）
    total = len(entries)
    if on_progress is not None:
        on_progress(0, total)

    records: dict[int, FileRecord] = {}
    to_read: dict[int, TreeEntry] = {}
    total_bytes = 0
    for index, entry in enumerate(entries):
        record = _classify_without_reading(entry, profile)
        if record is not None:
            records[index] = record
            continue
        # 先依 ls-tree 的大小檢查，超過上限時完全不讀取 blob
        total_bytes += entry.size or 0
        if total_bytes > profile.max_snapshot_bytes:
            raise ImportLimitExceededError(
                f"The snapshot exceeds the total size limit of {profile.max_snapshot_bytes} bytes."
            )
        to_read[index] = entry
    if on_progress is not None:
        on_progress(len(records), total)

    expected_sizes = {entry.object_id: entry.size or 0 for entry in to_read.values()}
    blobs = read_blobs(path, expected_sizes, timeout=settings.import_git_timeout_seconds)
    for index, entry in to_read.items():
        records[index] = _classify_content(entry, blobs[entry.object_id])
    if on_progress is not None:
        on_progress(total, total)

    ordered = [records[index] for index in range(len(entries))]
    imported = sum(1 for record in ordered if record["status"] == "imported")
    stats = {
        "file_count": imported,
        "skipped_count": len(ordered) - imported,
        "total_bytes": total_bytes,
    }
    return ordered, stats


def _write_completion(
    session: Session,
    snapshot_id: int,
    token: datetime,
    records: list[FileRecord],
    stats: dict[str, int],
) -> None:
    """檔案列與 completed 狀態的寫入（**不 commit**，由呼叫者的交易一起提交）。

    以 started_at 確認仍擁有此快照。
    """
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Snapshot)
            .where(
                Snapshot.id == snapshot_id,
                Snapshot.status == "importing",
                Snapshot.started_at == token,
            )
            .values(status="completed", completed_at=_utcnow(), error_summary=None, **stats)
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        raise SnapshotImportInProgressError(
            "The snapshot import was taken over by another request."
        )
    for start in range(0, len(records), INSERT_BATCH_SIZE):
        batch = records[start : start + INSERT_BATCH_SIZE]
        session.execute(
            insert(SnapshotFile), [{"snapshot_id": snapshot_id, **record} for record in batch]
        )


def _complete_snapshot(
    session: Session,
    snapshot_id: int,
    token: datetime,
    records: list[FileRecord],
    stats: dict[str, int],
) -> None:
    """檔案列與 completed 狀態在同一交易提交；以 started_at 確認仍擁有此快照。"""
    _write_completion(session, snapshot_id, token, records, stats)
    session.commit()


def _summarize(exc: Exception) -> str:
    # 領域例外的訊息不含檔案內容；其他例外只記錄型別，避免 SQL 參數（可能含內容）外洩
    message = (
        exc.message if isinstance(exc, RepoPilotError) else f"Unexpected {type(exc).__name__}."
    )
    return message[:ERROR_SUMMARY_LIMIT]


def _mark_failed(session: Session, snapshot_id: int, token: datetime, summary: str) -> None:
    """rollback 後以獨立交易記錄失敗；本身失敗時只記錄日誌，不遮蔽原始例外。"""
    try:
        session.rollback()
        session.execute(
            update(Snapshot)
            .where(
                Snapshot.id == snapshot_id,
                Snapshot.status == "importing",
                Snapshot.started_at == token,
            )
            .values(status="failed", completed_at=_utcnow(), error_summary=summary)
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error(
            "snapshot_mark_failed_error",
            extra={"snapshot_id": snapshot_id, "error_type": type(exc).__name__},
        )
