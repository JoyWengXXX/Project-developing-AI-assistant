import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.errors import (
    ImportLimitExceededError,
    RepoPilotError,
    SnapshotImportInProgressError,
)
from repopilot.git.objects import TreeEntry, list_tree, read_blobs, resolve_commit
from repopilot.git.repository import ensure_repository_root
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services.import_profile import ImportProfile
from repopilot.services.repositories import get_repository, resolve_repository_path

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.snapshots")

SNAPSHOT_UNIQUE_CONSTRAINT = "uq_snapshots_repository_id_commit_sha_import_profile_hash"
ERROR_SUMMARY_LIMIT = 500
INSERT_BATCH_SIZE = 1000
_UNSAFE_SEGMENTS = frozenset({"", ".", "..", ".git"})
_MAX_ACQUIRE_ATTEMPTS = 3

FileRecord = dict[str, Any]


def import_snapshot(
    session: Session, repository_id: int, revision: str, settings: Settings
) -> tuple[Snapshot, bool]:
    """匯入 repo 在指定 revision 的固定版本快照；回傳 (Snapshot, 是否本次新建完成)。

    只讀取 Git 物件，不讀工作目錄。相同 (repo, commit, 匯入設定) 已完成時直接回傳既有快照。
    """
    started = time.monotonic()
    repository = get_repository(session, repository_id)
    raw_path = repository.canonical_local_path
    # 結束讀取交易，避免之後的 Git 作業期間持有閒置連線
    session.commit()

    path = resolve_repository_path(raw_path, settings.repository_roots)
    ensure_repository_root(path, timeout=settings.git_timeout_seconds)
    commit_sha = resolve_commit(path, revision, timeout=settings.git_timeout_seconds)
    profile = ImportProfile.from_settings(settings)

    snapshot, owned = _acquire_snapshot(
        session, repository_id, commit_sha, profile.hash(), settings.import_stale_seconds
    )
    if not owned:
        logger.info("snapshot_import_reused", extra={"snapshot_id": snapshot.id})
        return snapshot, False

    snapshot_id = snapshot.id
    token = snapshot.started_at
    try:
        records, stats = _build_records(path, commit_sha, profile, settings)
        _complete_snapshot(session, snapshot_id, token, records, stats)
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


def _find_snapshot(
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
) -> tuple[Snapshot, bool]:
    """取得匯入所有權；回傳 (快照, 是否由本請求擁有)。

    唯一性由資料庫約束仲裁：先嘗試 INSERT，衝突後再依既有狀態決定回傳、拒絕或接手。
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
            if SNAPSHOT_UNIQUE_CONSTRAINT not in str(exc.orig):
                raise
        else:
            return snapshot, True

        existing = _find_snapshot(session, repository_id, commit_sha, profile_hash)
        if existing is None:  # 衝突後該列被刪除；重新嘗試建立
            continue
        if existing.status == "completed":
            return existing, False
        cutoff = _utcnow() - timedelta(seconds=stale_seconds)
        if existing.status == "importing" and existing.started_at >= cutoff:
            raise SnapshotImportInProgressError("The same snapshot is already being imported.")
        claimed = _claim_snapshot(session, existing.id, cutoff)
        if claimed is not None:
            return claimed, True
    raise SnapshotImportInProgressError("The same snapshot is already being imported.")


def _claim_snapshot(session: Session, snapshot_id: int, stale_cutoff: datetime) -> Snapshot | None:
    """以條件式 UPDATE 接手 failed 或逾時的 importing 快照；影響列數為 0 表示被他人搶先。"""
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Snapshot)
            .where(
                Snapshot.id == snapshot_id,
                or_(
                    Snapshot.status == "failed",
                    (Snapshot.status == "importing") & (Snapshot.started_at < stale_cutoff),
                ),
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
    path: Path, commit_sha: str, profile: ImportProfile, settings: Settings
) -> tuple[list[FileRecord], dict[str, int]]:
    entries = sorted(
        list_tree(path, commit_sha, timeout=settings.git_timeout_seconds),
        key=lambda entry: entry.path,
    )
    if len(entries) > profile.max_files:
        raise ImportLimitExceededError(
            f"The snapshot has {len(entries)} files; the limit is {profile.max_files}."
        )

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

    expected_sizes = {entry.object_id: entry.size or 0 for entry in to_read.values()}
    blobs = read_blobs(path, expected_sizes, timeout=settings.import_git_timeout_seconds)
    for index, entry in to_read.items():
        records[index] = _classify_content(entry, blobs[entry.object_id])

    ordered = [records[index] for index in range(len(entries))]
    imported = sum(1 for record in ordered if record["status"] == "imported")
    stats = {
        "file_count": imported,
        "skipped_count": len(ordered) - imported,
        "total_bytes": total_bytes,
    }
    return ordered, stats


def _complete_snapshot(
    session: Session,
    snapshot_id: int,
    token: datetime,
    records: list[FileRecord],
    stats: dict[str, int],
) -> None:
    """檔案列與 completed 狀態在同一交易提交；以 started_at 確認仍擁有此快照。"""
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
