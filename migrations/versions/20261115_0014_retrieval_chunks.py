"""建立快照片段集合；這不是已完成的向量檢索索引。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_snapshot_files_id_snapshot_id"), "snapshot_files", ["id", "snapshot_id"]
    )
    op.create_table(
        "retrieval_chunk_sets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("max_bytes", sa.Integer(), nullable=False),
        sa.Column("structure_index_id", sa.Integer(), nullable=True),
        sa.Column("structure_profile_hash", sa.String(64), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["snapshots.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("snapshot_id", "profile_hash"),
        sa.UniqueConstraint("id", "snapshot_id"),
        sa.CheckConstraint("max_bytes >= 4", name=op.f("ck_retrieval_chunk_sets_max_bytes_valid")),
        sa.CheckConstraint(
            "chunk_count >= 0", name=op.f("ck_retrieval_chunk_sets_chunk_count_nonnegative")
        ),
        sa.CheckConstraint(
            "(structure_index_id IS NULL) = (structure_profile_hash IS NULL)",
            name=op.f("ck_retrieval_chunk_sets_structure_provenance_valid"),
        ),
    )
    op.create_table(
        "retrieval_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("chunk_set_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_file_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("split_kind", sa.String(12), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_set_id", "snapshot_id"],
            ["retrieval_chunk_sets.id", "retrieval_chunk_sets.snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_file_id", "snapshot_id"],
            ["snapshot_files.id", "snapshot_files.snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("chunk_set_id", "snapshot_file_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_retrieval_chunks_ordinal_nonnegative")),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name=op.f("ck_retrieval_chunks_offsets_valid"),
        ),
        sa.CheckConstraint(
            "start_line >= 1 AND end_line >= start_line",
            name=op.f("ck_retrieval_chunks_lines_valid"),
        ),
        sa.CheckConstraint(
            "split_kind IN ('structure', 'paragraph', 'text')",
            name=op.f("ck_retrieval_chunks_split_kind_valid"),
        ),
    )
    op.create_index(
        "ix_retrieval_chunks_snapshot_file_id", "retrieval_chunks", ["snapshot_file_id"]
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM retrieval_chunk_sets) THEN
                RAISE EXCEPTION 'Cannot downgrade while retrieval chunk sets exist.';
            END IF;
        END;
        $$
        """
    )
    op.drop_index("ix_retrieval_chunks_snapshot_file_id", table_name="retrieval_chunks")
    op.drop_table("retrieval_chunks")
    op.drop_table("retrieval_chunk_sets")
    op.drop_constraint(op.f("uq_snapshot_files_id_snapshot_id"), "snapshot_files", type_="unique")
