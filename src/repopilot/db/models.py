from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from repopilot.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Repository(Base):
    """已註冊的 Git repo（本機路徑或 GitHub clone）；不綁定「參考／目標」角色，由分析任務選擇。

    `source_type='github'` 時 `canonical_local_path` 為受管目錄（`MANAGED_REPOS_DIR`）內的路徑；
    `github_owner`／`github_name` 為 GitHub API 回傳的標準大小寫，去重以小寫為準（部分唯一索引）。
    `github_is_private`／`github_default_branch` 是註冊當下的值，之後可能過期。
    """

    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("source_type IN ('local', 'github')", name="source_type_valid"),
        CheckConstraint(
            "(source_type = 'github' AND github_owner IS NOT NULL AND github_name IS NOT NULL)"
            " OR (source_type = 'local' AND github_owner IS NULL AND github_name IS NULL)",
            name="github_fields_match_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # 唯一約束名稱由 naming convention 產生：uq_repositories_canonical_local_path
    canonical_local_path: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    source_type: Mapped[str] = mapped_column(
        String(10), default="local", server_default=text("'local'")
    )
    github_owner: Mapped[str | None] = mapped_column(String(39))
    github_name: Mapped[str | None] = mapped_column(String(100))
    github_is_private: Mapped[bool | None] = mapped_column(Boolean)
    github_default_branch: Mapped[str | None] = mapped_column(String(255))


# GitHub 來源以「不分大小寫的 owner/name」去重；本機來源不受影響（部分索引）。
# 註冊時捕捉此索引名稱的 IntegrityError 後重新查詢（同 D20）
GITHUB_UNIQUE_INDEX = "uq_repositories_github_full_name_lower"
Index(
    GITHUB_UNIQUE_INDEX,
    func.lower(Repository.github_owner),
    func.lower(Repository.github_name),
    unique=True,
    postgresql_where=text("source_type = 'github'"),
)


class Snapshot(Base):
    """某個 commit 在特定匯入設定下的固定版本快照；非 `completed` 者不得被當成有效資料。"""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha", "import_profile_hash"),
        # 冗餘的唯一約束（id 本來就唯一）：只為了讓 repository_references 能以複合外鍵
        # (snapshot_id, repository_id) 保證「參考的快照屬於同一個 repo」（0007，D41）
        UniqueConstraint("id", "repository_id"),
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


class RepositoryReference(Base):
    """repo 的「參考版本」標記（第二階段切片 3b，D41）：每個 repo 最多一列（`repository_id` 唯一）。

    只是標記與持久化，目前不接 Agent。`(snapshot_id, repository_id)` 是指向
    `snapshots(id, repository_id)` 的複合外鍵，所以資料庫保證快照屬於同一個 repo；
    `ON DELETE CASCADE`：快照或 repo 消失時參考一起消失。快照必須 `completed` 這件事由 service
    層檢查（`completed` 是終態，見 D30），資料庫不重複表達。
    """

    __tablename__ = "repository_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "repository_id"],
            ["snapshots.id", "snapshots.repository_id"],
            ondelete="CASCADE",
        ),
        # 外鍵 cascade 刪除快照時用 snapshot_id 找參考列
        Index("ix_repository_references_snapshot_id", "snapshot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), unique=True
    )
    snapshot_id: Mapped[int] = mapped_column(Integer)
    # 設定（或換成另一個快照）的時間；重複 PUT 同一個快照不會更新
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class SnapshotFile(Base):
    """快照內的單一路徑；skipped 時 content 為 NULL，symlink／submodule 可能沒有 sha256 等欄位。"""

    __tablename__ = "snapshot_files"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "path"),
        UniqueConstraint("id", "snapshot_id"),
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


class RetrievalChunkSet(Base):
    """單一快照與切分設定下的完整片段集合，不代表 embedding 已完成。"""

    __tablename__ = "retrieval_chunk_sets"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "profile_hash"),
        UniqueConstraint("id", "snapshot_id"),
        CheckConstraint("max_bytes >= 4", name="max_bytes_valid"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_nonnegative"),
        CheckConstraint(
            "(structure_index_id IS NULL) = (structure_profile_hash IS NULL)",
            name="structure_provenance_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    profile_hash: Mapped[str] = mapped_column(String(64))
    max_bytes: Mapped[int] = mapped_column(Integer)
    structure_index_id: Mapped[int | None] = mapped_column(Integer)
    structure_profile_hash: Mapped[str | None] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class RetrievalChunk(Base):
    """快照文字的半開字元區間，複合外鍵禁止跨快照引用。"""

    __tablename__ = "retrieval_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_set_id", "snapshot_id"],
            ["retrieval_chunk_sets.id", "retrieval_chunk_sets.snapshot_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["snapshot_file_id", "snapshot_id"],
            ["snapshot_files.id", "snapshot_files.snapshot_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("chunk_set_id", "snapshot_file_id", "ordinal"),
        UniqueConstraint("id", "chunk_set_id"),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_valid"),
        CheckConstraint("start_line >= 1 AND end_line >= start_line", name="lines_valid"),
        CheckConstraint(
            "split_kind IN ('structure', 'paragraph', 'text')", name="split_kind_valid"
        ),
        Index("ix_retrieval_chunks_snapshot_file_id", "snapshot_file_id"),
        Index("ix_retrieval_chunks_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer)
    chunk_set_id: Mapped[int] = mapped_column(BigInteger)
    snapshot_file_id: Mapped[int] = mapped_column(BigInteger)
    ordinal: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    split_kind: Mapped[str] = mapped_column(String(12))
    content_hash: Mapped[str] = mapped_column(String(64))


class RetrievalIndex(Base):
    """單一片段集合與 embedding 設定下的索引；只有完成狀態可查詢。"""

    __tablename__ = "retrieval_indexes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_set_id", "snapshot_id"],
            ["retrieval_chunk_sets.id", "retrieval_chunk_sets.snapshot_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("chunk_set_id", "profile_hash"),
        UniqueConstraint("id", "chunk_set_id"),
        CheckConstraint("profile_hash ~ '^[0-9a-f]{64}$'", name="profile_hash_valid"),
        CheckConstraint("jsonb_typeof(profile) = 'object'", name="profile_object"),
        CheckConstraint(
            "(status = 'building' AND owner_token IS NOT NULL AND completed_at IS NULL) OR "
            "(status = 'failed' AND owner_token IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND owner_token IS NULL AND completed_at IS NOT NULL)",
            name="state_valid",
        ),
        CheckConstraint("segment_count >= 0", name="segment_count_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(Integer)
    chunk_set_id: Mapped[int] = mapped_column(BigInteger)
    profile_hash: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    owner_token: Mapped[UUID | None] = mapped_column(Uuid)
    segment_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetrievalVector(Base):
    """片段內的半開字元區間及 768 維向量；複合外鍵限制索引與片段的歸屬。"""

    __tablename__ = "retrieval_vectors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["index_id", "chunk_set_id"],
            ["retrieval_indexes.id", "retrieval_indexes.chunk_set_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "chunk_set_id"],
            ["retrieval_chunks.id", "retrieval_chunks.chunk_set_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("index_id", "chunk_id", "ordinal"),
        CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="offsets_valid"),
        CheckConstraint("token_count >= 1 AND token_count <= 512", name="token_count_valid"),
        Index("ix_retrieval_vectors_chunk_id_chunk_set_id", "chunk_id", "chunk_set_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    index_id: Mapped[int] = mapped_column(BigInteger)
    chunk_set_id: Mapped[int] = mapped_column(BigInteger)
    chunk_id: Mapped[int] = mapped_column(BigInteger)
    ordinal: Mapped[int] = mapped_column(Integer)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(768))


class GithubCredential(Base):
    """目前保存的 GitHub token（單一使用者，最多一列）。

    `id` 固定為 1，由 CHECK 約束保證；覆蓋以單一 `INSERT ... ON CONFLICT DO UPDATE` 完成。
    `encrypted_token` 是 Fernet 密文；`created_at` 為最近一次保存（含覆蓋）的時間。
    """

    __tablename__ = "github_credentials"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("token_type IN ('fine_grained', 'classic', 'unknown')", name="type_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    encrypted_token: Mapped[str] = mapped_column(Text)
    login: Mapped[str] = mapped_column(String(100))
    token_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Job(Base):
    """背景工作（第二階段，D39）：狀態機與租約由 `services/jobs.py` 維護，事實來源是 PostgreSQL。

    `lease_token` 是「目前由誰執行」的柵欄令牌（每次取得租約都重新產生）；**不得**經 API 回傳，
    也不得出現在 `params`／`work_state`／`result`／日誌。`work_state` 是 worker 內部資料，
    同樣不經 API 回傳。`repository_id` 可為 NULL（`github_register` 成功前資料列尚不存在）。
    `repository_id` 為 ON DELETE CASCADE、`snapshot_id` 為 ON DELETE SET NULL，所以 D33 移除 repo
    時既有的刪除順序（snapshot_files → snapshots → repositories）不必修改。
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('snapshot_import', 'snapshot_index', 'github_register', 'github_fetch', "
            "'snapshot_retrieval', 'qa_question')",
            name="kind_valid",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "phase IN ('pending', 'importing', 'indexing', 'resolving', 'cloning', "
            "'registering', 'fetching', 'embedding', 'retrieving', 'generating', 'completed')",
            name="phase_valid",
        ),
        # running 若且唯若持有令牌與租約期限：不會有「沒有期限、永遠無法復原」的 running 工作
        CheckConstraint(
            "(status = 'running') = (lease_token IS NOT NULL)", name="lease_matches_status"
        ),
        CheckConstraint(
            "(status = 'running') = (lease_expires_at IS NOT NULL)",
            name="lease_expiry_matches_status",
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="finished_matches_status",
        ),
        CheckConstraint("attempt >= 0 AND max_attempts >= 1", name="attempt_range"),
        CheckConstraint(
            "processed_files >= 0 AND indexed_files >= 0 AND parse_error_count >= 0"
            " AND (total_files IS NULL OR total_files >= 0)"
            " AND (bytes_received IS NULL OR bytes_received >= 0)",
            name="progress_nonnegative",
        ),
        # 「進行中才唯一」：同一份工作同時只會有一個 queued／running 的 job，由資料庫仲裁
        Index(
            "uq_jobs_dedupe_key_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_jobs_status_id", "status", "id"),
        Index("ix_jobs_repository_id_id", "repository_id", "id"),
        Index("ix_jobs_snapshot_id", "snapshot_id"),
        Index("ix_jobs_dedupe_key_id", "dedupe_key", "id"),
        # 復原掃描：只掃 running 的租約期限
        Index(
            "ix_jobs_running_lease_expires_at",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    phase: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    repository_id: Mapped[int | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshots.id", ondelete="SET NULL"))
    dedupe_key: Mapped[str] = mapped_column(Text)
    repo_key: Mapped[str | None] = mapped_column(Text)
    # 只存安全欄位（requested_revision、commit_sha、github_repo…）；由 service 拒絕含秘密的鍵名
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # none_as_null：Python None 存成 SQL NULL（預設會存成 JSON 的 null，`IS NULL` 查不到）
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    work_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    import_profile_hash: Mapped[str | None] = mapped_column(String(64))
    index_profile_hash: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_files: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_files: Mapped[int | None] = mapped_column(Integer)
    indexed_files: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parse_error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bytes_received: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class JobDispatch(Base):
    """transactional outbox：與 job 同一交易寫入的待派送事件（`pending`），派送成功後為 `sent`。

    `(job_id, attempt_target)` 唯一：同一次執行嘗試只有一筆事件，重送是把同一列改回 `pending`。
    `send_count` 只計成功派送；`failure_count` 計發布器失敗次數（保留 `pending`，下一輪重試）。
    `last_error` 只存例外型別名稱，不存訊息。
    """

    __tablename__ = "job_dispatches"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_target"),
        CheckConstraint("status IN ('pending', 'sent')", name="status_valid"),
        CheckConstraint(
            "attempt_target >= 1 AND send_count >= 0 AND failure_count >= 0", name="counts_valid"
        ),
        Index(
            "ix_job_dispatches_pending_not_before_id",
            "not_before",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_job_dispatches_sent_last_sent_at",
            "last_sent_at",
            postgresql_where=text("status = 'sent'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    attempt_target: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    send_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class ServiceHeartbeat(Base):
    """dispatcher／worker 的存活心跳（C8）：讓介面區分「在排隊」與「根本沒有 worker」。"""

    __tablename__ = "service_heartbeats"
    __table_args__ = (CheckConstraint("service IN ('dispatcher', 'worker')", name="service_valid"),)

    service: Mapped[str] = mapped_column(String(20), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    # 只放非秘密資訊（解析器版本等）
    info: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class SnapshotIndex(Base):
    """某個快照在特定索引設定下的結構索引（第二階段切片 6，migration 0008）。

    衍生資料，與快照分離（規格 6.3）：索引失敗不使快照失效，`building`／`failed` 的索引不被任何
    查詢採用。`owner_token` 在 `building` 時＝建立它的 job 的 `lease_token`（資料面的柵欄：
    舊 worker 不能覆寫新 attempt 的索引），結束時清為 NULL。
    """

    __tablename__ = "snapshot_indexes"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "index_profile_hash"),
        CheckConstraint("status IN ('building', 'completed', 'failed')", name="status_valid"),
        CheckConstraint(
            "(status = 'building') = (owner_token IS NOT NULL)", name="owner_matches_status"
        ),
        CheckConstraint(
            "file_count >= 0 AND symbol_count >= 0 AND parse_error_count >= 0",
            name="counts_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    index_profile_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    owner_token: Mapped[UUID | None] = mapped_column(Uuid)
    # 各語言抽取器與版本（`IndexProfile.analyzers()`），不含秘密
    analyzers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    file_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    symbol_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parse_error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)


class IndexedFile(Base):
    """索引過的檔案；不支援的檔案沒有列，不宣稱理解其結構。"""

    __tablename__ = "indexed_files"
    __table_args__ = (
        UniqueConstraint("snapshot_index_id", "snapshot_file_id"),
        # 冗餘的唯一約束：讓 symbols 等表以複合外鍵保證「同一個索引」
        UniqueConstraint("id", "snapshot_index_id"),
        CheckConstraint(
            "language IN ('python', 'csharp', 'typescript', 'javascript', 'java', 'go', "
            "'rust', 'sql')",
            name="language_valid",
        ),
        CheckConstraint(
            "parse_status IN ('parsed', 'parsed_with_errors', 'failed')", name="parse_status_valid"
        ),
        CheckConstraint(
            "error_kind IS NULL OR error_kind IN "
            "('syntax_error', 'tree_error', 'too_deep', 'parser_crash', 'dialect_unspecified', "
            "'sql_too_large', 'sql_name_too_long')",
            name="error_kind_valid",
        ),
        CheckConstraint(
            "(parse_status = 'parsed') = (error_kind IS NULL AND error_count = 0)",
            name="error_matches_status",
        ),
        # 快照檔案被 SQL 刪除（D33）時，級聯需要用 snapshot_file_id 找到對應的列
        Index("ix_indexed_files_snapshot_file_id", "snapshot_file_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_index_id: Mapped[int] = mapped_column(
        ForeignKey("snapshot_indexes.id", ondelete="CASCADE")
    )
    snapshot_file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("snapshot_files.id", ondelete="CASCADE")
    )
    language: Mapped[str] = mapped_column(String(10))
    parse_status: Mapped[str] = mapped_column(String(20))
    error_kind: Mapped[str | None] = mapped_column(String(20))
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    first_error_line: Mapped[int | None] = mapped_column(Integer)
    first_error_column: Mapped[int | None] = mapped_column(Integer)
    module_name: Mapped[str | None] = mapped_column(Text)
    symbol_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    symbols_truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )


class Symbol(Base):
    """符號（模組、類別、函式…）。`(id, snapshot_index_id)` 讓複合外鍵禁止跨索引的父子關係。"""

    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("id", "snapshot_index_id"),
        # 不假設名稱唯一：C# 多載與 partial 會有相同的 qualified name，以位置區分
        UniqueConstraint(
            "indexed_file_id",
            "kind",
            "qualified_name",
            "start_line",
            "start_column",
            name="uq_symbols_file_kind_qualified_name_position",
        ),
        ForeignKeyConstraint(
            ["indexed_file_id", "snapshot_index_id"],
            ["indexed_files.id", "indexed_files.snapshot_index_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["parent_symbol_id", "snapshot_index_id"],
            ["symbols.id", "symbols.snapshot_index_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "language IN ('python', 'csharp', 'typescript', 'javascript', 'java', 'go', "
            "'rust', 'sql')",
            name="language_valid",
        ),
        CheckConstraint(
            "kind IN ('module', 'namespace', 'class', 'struct', 'record', 'interface', "
            "'type_alias', 'enum', 'enum_member', 'delegate', 'function', 'method', "
            "'constructor', 'property', 'field', 'table', 'view', 'procedure')",
            name="kind_valid",
        ),
        CheckConstraint("start_line >= 1 AND end_line >= start_line", name="lines_valid"),
        CheckConstraint("start_column >= 0 AND end_column >= 0", name="columns_valid"),
        Index("ix_symbols_snapshot_index_id_kind_name", "snapshot_index_id", "kind", "name"),
        Index(
            "ix_symbols_snapshot_index_id_indexed_file_id_start_line",
            "snapshot_index_id",
            "indexed_file_id",
            "start_line",
        ),
        Index(
            "ix_symbols_snapshot_index_id_parent_symbol_id", "snapshot_index_id", "parent_symbol_id"
        ),
        # 前綴查詢（LIKE 'a.b.%'）
        Index(
            "ix_symbols_snapshot_index_id_qualified_name",
            "snapshot_index_id",
            "qualified_name",
            postgresql_ops={"qualified_name": "text_pattern_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_index_id: Mapped[int] = mapped_column(Integer)
    indexed_file_id: Mapped[int] = mapped_column(BigInteger)
    parent_symbol_id: Mapped[int | None] = mapped_column(BigInteger)
    language: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(Text)
    qualified_name: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    start_column: Mapped[int] = mapped_column(Integer)
    end_column: Mapped[int] = mapped_column(Integer)
    leading_start_line: Mapped[int | None] = mapped_column(Integer)
    modifiers: Mapped[str | None] = mapped_column(Text)
    is_async: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    type_parameter_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    has_errors: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    conditional: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class ImportReference(Base):
    """同檔的 import／using 參照（只記文字，不解析到實際模組）。"""

    __tablename__ = "import_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["indexed_file_id", "snapshot_index_id"],
            ["indexed_files.id", "indexed_files.snapshot_index_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_symbol_id", "snapshot_index_id"],
            ["symbols.id", "symbols.snapshot_index_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "language IN ('python', 'csharp', 'typescript', 'javascript', 'java', 'go', "
            "'rust', 'sql')",
            name="language_valid",
        ),
        CheckConstraint(
            "kind IN ('import', 'from_import', 'using', 'using_static', 'using_alias', "
            "'global_using', 'script_import', 'java_import', 'java_static_import', "
            "'go_import', 'rust_use')",
            name="kind_valid",
        ),
        CheckConstraint("line >= 1", name="line_valid"),
        Index(
            "ix_import_references_snapshot_index_id_indexed_file_id",
            "snapshot_index_id",
            "indexed_file_id",
        ),
        Index(
            "ix_import_references_snapshot_index_id_source_symbol_id",
            "snapshot_index_id",
            "source_symbol_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_index_id: Mapped[int] = mapped_column(Integer)
    indexed_file_id: Mapped[int] = mapped_column(BigInteger)
    # 所在的符號（Python 的模組、類別或函式）；C# 位於 compilation unit 層級時為 NULL
    source_symbol_id: Mapped[int | None] = mapped_column(BigInteger)
    language: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(20))
    target: Mapped[str] = mapped_column(Text)
    imported_name: Mapped[str | None] = mapped_column(Text)
    alias: Mapped[str | None] = mapped_column(Text)
    relative_level: Mapped[int | None] = mapped_column(Integer)
    line: Mapped[int] = mapped_column(Integer)


class SymbolRelation(Base):
    """符號之間的關係（基底類別、裝飾器…）；`target_name` 是原始名稱，不宣稱已解析。"""

    __tablename__ = "symbol_relations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_symbol_id", "snapshot_index_id"],
            ["symbols.id", "symbols.snapshot_index_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["target_symbol_id", "snapshot_index_id"],
            ["symbols.id", "symbols.snapshot_index_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "relation_type IN ('base_class', 'base_type', 'decorator', 'attribute')",
            name="relation_type_valid",
        ),
        CheckConstraint(
            "resolution IN ('resolved_same_file', 'ambiguous', 'unresolved')",
            name="resolution_valid",
        ),
        CheckConstraint(
            "(resolution = 'resolved_same_file') = (target_symbol_id IS NOT NULL)",
            name="target_matches_resolution",
        ),
        Index(
            "ix_symbol_relations_snapshot_index_id_source_symbol_id",
            "snapshot_index_id",
            "source_symbol_id",
        ),
        Index(
            "ix_symbol_relations_snapshot_index_id_target_symbol_id",
            "snapshot_index_id",
            "target_symbol_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_index_id: Mapped[int] = mapped_column(Integer)
    source_symbol_id: Mapped[int] = mapped_column(BigInteger)
    relation_type: Mapped[str] = mapped_column(String(20))
    target_name: Mapped[str] = mapped_column(Text)
    target_symbol_id: Mapped[int | None] = mapped_column(BigInteger)
    resolution: Mapped[str] = mapped_column(String(25))
    line: Mapped[int] = mapped_column(Integer)


class QaUsageLedger(Base):
    """問答額度帳本（第四階段 4-1b，migration 0019）：只追加，不更新、不刪除。

    同一個預留的事件以 `reservation_id` 串起來：一個 `reserve`，之後至多一個 `settle` 或
    `release`（部分唯一索引保證）。`reset` 不屬於任何預留（`reservation_id` 為 NULL）。
    各事件的 token 與費用是「該事件當下對這個預留的有效用量」：reserve＝預留量，settle／release＝
    實際用量（用量未知的 settle 沿用預留量並標記 `usage_unknown`），reset 一律為 0。
    `created_at` 由服務傳入（可注入時間），沒有預設值。
    """

    __tablename__ = "qa_usage_ledger"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('reserve', 'settle', 'release', 'reset')", name="event_type_valid"
        ),
        CheckConstraint(
            "(event_type = 'reset') = (reservation_id IS NULL)", name="reservation_matches_event"
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0",
            name="amounts_nonnegative",
        ),
        CheckConstraint(
            "event_type <> 'reset' OR (input_tokens = 0 AND output_tokens = 0 AND cost_usd = 0)",
            name="reset_amounts_zero",
        ),
        CheckConstraint(
            "NOT usage_unknown OR event_type = 'settle'", name="usage_unknown_only_on_settle"
        ),
        Index(
            "uq_qa_usage_ledger_reservation_id_reserve",
            "reservation_id",
            unique=True,
            postgresql_where=text("event_type = 'reserve'"),
        ),
        Index(
            "uq_qa_usage_ledger_reservation_id_final",
            "reservation_id",
            unique=True,
            postgresql_where=text("event_type IN ('settle', 'release')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reservation_id: Mapped[UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(10))
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    # 美元；小數 8 位，足以表示每百萬 token 單價乘上整數 token 數
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    usage_unknown: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QaSettings(Base):
    """問答設定（第四階段 4-1c，migration 0020；單一使用者，最多一列，`id` 固定為 1）。

    `model` 為 NULL＝尚未選擇（沒有預設，規格 D11）；是否為已知模型由 API 層驗證。
    額度上限 NULL＝不限制。`price_table` 是 `{模型: {輸入單價, 輸出單價}}`（鍵為
    `input_usd_per_million`、`output_usd_per_million`，數字以字串保存，避免浮點誤差），
    `price_table_as_of` 是價格的讀取日期。
    `encrypted_provider_key` 是 Fernet 密文，只寫不讀。
    """

    __tablename__ = "qa_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("provider IN ('anthropic')", name="provider_valid"),
        CheckConstraint(
            "per_request_token_limit IS NULL OR per_request_token_limit > 0",
            name="per_request_token_limit_positive",
        ),
        CheckConstraint(
            "daily_token_limit IS NULL OR daily_token_limit > 0",
            name="daily_token_limit_positive",
        ),
        CheckConstraint(
            "daily_cost_limit_usd IS NULL OR daily_cost_limit_usd > 0",
            name="daily_cost_limit_usd_positive",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    provider: Mapped[str] = mapped_column(
        String(20), default="anthropic", server_default=text("'anthropic'")
    )
    model: Mapped[str | None] = mapped_column(String(100))
    per_request_token_limit: Mapped[int | None] = mapped_column(Integer)
    daily_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    daily_cost_limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    price_table: Mapped[dict[str, Any]] = mapped_column(JSONB)
    price_table_as_of: Mapped[date] = mapped_column(Date)
    encrypted_provider_key: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RepositoryQaConsent(Base):
    """逐 repo 同意把片段送往問答供應商（規格 D2）：沒有資料列＝未同意，撤回即刪除。

    `repository_id` 為 ON DELETE CASCADE，移除 repo 時同意一併消失。
    """

    __tablename__ = "repository_qa_consent"

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True, autoincrement=False
    )
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QaQuestion(Base):
    """一次問答的完整追蹤記錄（第四階段 4-3a，migration 0021；規格 D8 保存問題全文）。

    `snapshot_ids`、`reservation_id`（帳本，無外鍵）都是純值：快照、repo 被移除後記錄照舊可讀。
    `job_id` 為 ON DELETE SET NULL（job 歷史被清除不影響問答）。`input_tokens`／`output_tokens`
    是供應商回報的實際用量（輸出含思考）；`usage_unknown` 表示沒有回報，帳本沿用預留量。
    `estimated_*` 一開始是提問時的上限估計，worker 預留額度時改為實際預留的值。
    狀態機與 job 一致（queued／running／succeeded／failed／cancelled）；`answer_status` 只在
    `succeeded` 時有值。
    """

    __tablename__ = "qa_questions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "answer_status IN ('answered', 'insufficient_evidence')", name="answer_status_valid"
        ),
        CheckConstraint(
            "(status = 'succeeded') = (answer_status IS NOT NULL)", name="answer_matches_status"
        ),
        CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="finished_matches_status",
        ),
        CheckConstraint("status <> 'failed' OR error_code IS NOT NULL", name="failed_has_error"),
        CheckConstraint("cardinality(snapshot_ids) >= 1", name="snapshot_ids_nonempty"),
        CheckConstraint("max_tokens >= 1", name="max_tokens_positive"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0)"
            " AND (output_tokens IS NULL OR output_tokens >= 0)"
            " AND (cost_usd IS NULL OR cost_usd >= 0)"
            " AND estimated_input_tokens >= 0 AND estimated_cost_usd >= 0",
            name="amounts_nonnegative",
        ),
        Index("ix_qa_questions_status_id", "status", "id"),
        Index("ix_qa_questions_job_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    snapshot_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    answer_status: Mapped[str | None] = mapped_column(String(25))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(20))
    max_tokens: Mapped[int] = mapped_column(Integer)
    # 實際呼叫供應商的次數（含重試）；`input_tokens`／`output_tokens`／`cost_usd` 是各次嘗試的累計
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reservation_id: Mapped[UUID | None] = mapped_column(Uuid)
    estimated_input_tokens: Mapped[int] = mapped_column(BigInteger)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    usage_unknown: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # 任一次嘗試的實際用量超過該次預留（CL-03）；帳本仍以實際用量計入
    exceeded_reservation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    provider_request_id: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    request_id: Mapped[str | None] = mapped_column(String(64))
    retrieval_filters: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, comment="檢索篩選條件，包含 file_kinds (列表) 和 path_prefix (字串)"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QaContextChunk(Base):
    """檢索到的片段與送出當下的來源位置。`code` 為 NULL 表示沒有送出（`skip_reason` 說明原因）。

    `repository_id`、`snapshot_id`、`chunk_id` 是純整數（沒有外鍵）：快照被移除後仍能從這裡的
    repo 名稱、完整 SHA、路徑與行號解釋引用。
    """

    __tablename__ = "qa_context_chunks"
    __table_args__ = (
        UniqueConstraint("question_id", "rank"),
        UniqueConstraint("question_id", "code"),
        CheckConstraint("(code IS NULL) = (skip_reason IS NOT NULL)", name="code_matches_skip"),
        CheckConstraint(
            "skip_reason IN ('count_cap', 'budget_exhausted', 'empty_text')",
            name="skip_reason_valid",
        ),
        CheckConstraint("start_line >= 1 AND end_line >= start_line", name="lines_valid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("qa_questions.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer)
    code: Mapped[str | None] = mapped_column(String(10))
    skip_reason: Mapped[str | None] = mapped_column(String(20))
    repository_id: Mapped[int] = mapped_column(Integer)
    repo_name: Mapped[str] = mapped_column(String(200))
    snapshot_id: Mapped[int] = mapped_column(Integer)
    commit_sha: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    chunk_id: Mapped[int] = mapped_column(BigInteger)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class QaStatement(Base):
    """模型回答的一個陳述；`status=invalid` 的不屬於已驗證答案，`rejection_reasons` 說明原因。"""

    __tablename__ = "qa_statements"
    __table_args__ = (
        UniqueConstraint("question_id", "position"),
        CheckConstraint("kind IN ('fact', 'inference', 'open_question')", name="kind_valid"),
        CheckConstraint("status IN ('valid', 'invalid')", name="status_valid"),
        CheckConstraint(
            "status = 'valid' OR jsonb_array_length(rejection_reasons) > 0",
            name="invalid_has_reasons",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("qa_questions.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(15))
    # 欄位名稱是 text；屬性不能同名，否則會遮蔽類別本體中使用的 sqlalchemy `text()`
    body: Mapped[str] = mapped_column("text", Text)
    status: Mapped[str] = mapped_column(String(10))
    # [{"reason": ..., "citation_index": int | null}]
    rejection_reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )


class QaCitation(Base):
    """陳述的一個引用。

    `claimed_*` 是模型的說法；`resolved_*` 只在陳述通過驗證時有值（取自送出的片段）。
    """

    __tablename__ = "qa_citations"
    __table_args__ = (
        UniqueConstraint("statement_id", "position"),
        CheckConstraint("valid = (cardinality(reasons) = 0)", name="valid_matches_reasons"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("qa_statements.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    claimed_chunk: Mapped[str] = mapped_column(String(50))
    claimed_start_line: Mapped[int] = mapped_column(BigInteger)
    claimed_end_line: Mapped[int] = mapped_column(BigInteger)
    valid: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)), default=list, server_default=text("'{}'")
    )
    resolved_chunk_id: Mapped[int | None] = mapped_column(BigInteger)
    resolved_repository_id: Mapped[int | None] = mapped_column(Integer)
    resolved_repo_name: Mapped[str | None] = mapped_column(String(200))
    resolved_snapshot_id: Mapped[int | None] = mapped_column(Integer)
    resolved_commit_sha: Mapped[str | None] = mapped_column(String(40))
    resolved_path: Mapped[str | None] = mapped_column(Text)
    resolved_start_line: Mapped[int | None] = mapped_column(Integer)
    resolved_end_line: Mapped[int | None] = mapped_column(Integer)
