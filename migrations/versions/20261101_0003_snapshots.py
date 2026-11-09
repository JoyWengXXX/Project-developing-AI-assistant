"""建立 snapshots 與 snapshot_files 資料表

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("import_profile_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('importing', 'completed', 'failed')",
            name=op.f("ck_snapshots_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_snapshots_repository_id_repositories"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshots")),
        sa.UniqueConstraint(
            "repository_id",
            "commit_sha",
            "import_profile_hash",
            name=op.f("uq_snapshots_repository_id_commit_sha_import_profile_hash"),
        ),
    )
    op.create_table(
        "snapshot_files",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("git_blob_id", sa.String(length=64), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("skip_reason", sa.String(length=30), nullable=True),
        sa.CheckConstraint(
            "status IN ('imported', 'skipped')",
            name=op.f("ck_snapshot_files_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'imported') = (content IS NOT NULL)",
            name=op.f("ck_snapshot_files_content_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_snapshot_files_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshot_files")),
        sa.UniqueConstraint("snapshot_id", "path", name=op.f("uq_snapshot_files_snapshot_id_path")),
    )


def downgrade() -> None:
    op.drop_table("snapshot_files")
    op.drop_table("snapshots")
