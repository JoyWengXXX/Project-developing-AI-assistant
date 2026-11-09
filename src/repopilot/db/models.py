from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from repopilot.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Repository(Base):
    """已註冊的本機 Git repo；不綁定「參考／目標」角色，由分析任務選擇。"""

    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # 唯一約束名稱由 naming convention 產生：uq_repositories_canonical_local_path
    canonical_local_path: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Snapshot(Base):
    """某個 commit 在特定匯入設定下的固定版本快照；非 `completed` 者不得被當成有效資料。"""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha", "import_profile_hash"),
        CheckConstraint("status IN ('importing', 'completed', 'failed')", name="status_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    commit_sha: Mapped[str] = mapped_column(String(64))
    import_profile_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    # file_count 為 imported 檔案數；skipped_count 為 skipped 列數
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    # 實際讀取並計算 SHA-256 的 blob 位元組總和（不含 excluded／too_large 等未讀取者）
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # 同時作為所有權憑證：只有持有相同 started_at 的請求能完成或標記失敗
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)


class SnapshotFile(Base):
    """快照內的單一路徑；skipped 時 content 為 NULL，symlink／submodule 可能沒有 sha256 等欄位。"""

    __tablename__ = "snapshot_files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "path"),
        CheckConstraint("status IN ('imported', 'skipped')", name="status_valid"),
        CheckConstraint(
            "(status = 'imported') = (content IS NOT NULL)", name="content_matches_status"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    git_blob_id: Mapped[str | None] = mapped_column(String(64))
    sha256: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    skip_reason: Mapped[str | None] = mapped_column(String(30))
