"""建立背景工作資料表：jobs、job_dispatches（outbox）、service_heartbeats

第二階段切片 2（D39）。只新增資料表，既有資料表不改欄位：
- jobs.repository_id ON DELETE CASCADE、jobs.snapshot_id ON DELETE SET NULL，
  所以第一階段 D33 移除 repo 的刪除順序（snapshot_files → snapshots → repositories）不必修改。
- job_dispatches.job_id ON DELETE CASCADE。
- 不建立索引／符號相關的表（切片 6 的 0007）。

降版（downgrade）會刪除這三張表，**job 歷史與待派送事件一併消失**；第一階段資料不受影響。

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("phase", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=True),
        sa.Column("snapshot_id", sa.Integer(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("repo_key", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "work_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("import_profile_hash", sa.String(length=64), nullable=True),
        sa.Column("index_profile_hash", sa.String(length=64), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_files", sa.Integer(), nullable=True),
        sa.Column("indexed_files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parse_error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes_received", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("phase_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('snapshot_import', 'snapshot_index', 'github_register', 'github_fetch')",
            name=op.f("ck_jobs_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "phase IN ('pending', 'importing', 'indexing', 'resolving', 'cloning', "
            "'registering', 'fetching', 'completed')",
            name=op.f("ck_jobs_phase_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (lease_token IS NOT NULL)",
            name=op.f("ck_jobs_lease_matches_status"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (lease_expires_at IS NOT NULL)",
            name=op.f("ck_jobs_lease_expiry_matches_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name=op.f("ck_jobs_finished_matches_status"),
        ),
        sa.CheckConstraint(
            "attempt >= 0 AND max_attempts >= 1", name=op.f("ck_jobs_attempt_range")
        ),
        sa.CheckConstraint(
            "processed_files >= 0 AND indexed_files >= 0 AND parse_error_count >= 0"
            " AND (total_files IS NULL OR total_files >= 0)"
            " AND (bytes_received IS NULL OR bytes_received >= 0)",
            name=op.f("ck_jobs_progress_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_jobs_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_jobs_snapshot_id_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    # 「進行中才唯一」：重複提交與併發提交只會有一個 queued／running 的 job（資料庫仲裁）
    op.create_index(
        op.f("uq_jobs_dedupe_key_active"),
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(op.f("ix_jobs_status_id"), "jobs", ["status", "id"])
    op.create_index(op.f("ix_jobs_repository_id_id"), "jobs", ["repository_id", "id"])
    op.create_index(op.f("ix_jobs_snapshot_id"), "jobs", ["snapshot_id"])
    op.create_index(op.f("ix_jobs_dedupe_key_id"), "jobs", ["dedupe_key", "id"])
    op.create_index(
        op.f("ix_jobs_running_lease_expires_at"),
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "job_dispatches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_target", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("send_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent')", name=op.f("ck_job_dispatches_status_valid")
        ),
        sa.CheckConstraint(
            "attempt_target >= 1 AND send_count >= 0 AND failure_count >= 0",
            name=op.f("ck_job_dispatches_counts_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_job_dispatches_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_dispatches")),
        sa.UniqueConstraint(
            "job_id", "attempt_target", name=op.f("uq_job_dispatches_job_id_attempt_target")
        ),
    )
    # 派送與復原掃描都只看 pending／sent 的子集合
    op.create_index(
        op.f("ix_job_dispatches_pending_not_before_id"),
        "job_dispatches",
        ["not_before", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        op.f("ix_job_dispatches_sent_last_sent_at"),
        "job_dispatches",
        ["last_sent_at"],
        postgresql_where=sa.text("status = 'sent'"),
    )

    op.create_table(
        "service_heartbeats",
        sa.Column("service", sa.String(length=20), nullable=False),
        sa.Column("instance_id", sa.String(length=100), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "info",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "service IN ('dispatcher', 'worker')", name=op.f("ck_service_heartbeats_service_valid")
        ),
        sa.PrimaryKeyConstraint("service", "instance_id", name=op.f("pk_service_heartbeats")),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")
    op.drop_table("job_dispatches")  # 索引隨資料表一併刪除
    op.drop_table("jobs")
