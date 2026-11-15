"""建立檢索索引與向量片段，不公開尚未完成的索引。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_retrieval_chunks_id_chunk_set_id"), "retrieval_chunks", ["id", "chunk_set_id"]
    )
    op.create_table(
        "retrieval_indexes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("chunk_set_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("owner_token", sa.Uuid(), nullable=True),
        sa.Column("segment_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["chunk_set_id", "snapshot_id"],
            ["retrieval_chunk_sets.id", "retrieval_chunk_sets.snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("chunk_set_id", "profile_hash"),
        sa.UniqueConstraint("id", "chunk_set_id"),
        sa.CheckConstraint(
            "profile_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_retrieval_indexes_profile_hash_valid")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(profile) = 'object'", name=op.f("ck_retrieval_indexes_profile_object")
        ),
        sa.CheckConstraint(
            "(status = 'building' AND owner_token IS NOT NULL AND completed_at IS NULL) OR "
            "(status = 'failed' AND owner_token IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND owner_token IS NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_retrieval_indexes_state_valid"),
        ),
        sa.CheckConstraint(
            "segment_count >= 0", name=op.f("ck_retrieval_indexes_segment_count_nonnegative")
        ),
    )
    op.create_table(
        "retrieval_vectors",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("index_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_set_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.ForeignKeyConstraint(
            ["index_id", "chunk_set_id"],
            ["retrieval_indexes.id", "retrieval_indexes.chunk_set_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "chunk_set_id"],
            ["retrieval_chunks.id", "retrieval_chunks.chunk_set_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("index_id", "chunk_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_retrieval_vectors_ordinal_nonnegative")),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name=op.f("ck_retrieval_vectors_offsets_valid"),
        ),
        sa.CheckConstraint(
            "token_count >= 1 AND token_count <= 512",
            name=op.f("ck_retrieval_vectors_token_count_valid"),
        ),
    )
    op.create_index(
        "ix_retrieval_vectors_chunk_id_chunk_set_id",
        "retrieval_vectors",
        ["chunk_id", "chunk_set_id"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM retrieval_indexes) THEN
                RAISE EXCEPTION 'Cannot downgrade while retrieval indexes exist.';
            END IF;
        END;
        $$
        """
    )
    op.drop_index("ix_retrieval_vectors_chunk_id_chunk_set_id", table_name="retrieval_vectors")
    op.drop_table("retrieval_vectors")
    op.drop_table("retrieval_indexes")
    op.drop_constraint(
        op.f("uq_retrieval_chunks_id_chunk_set_id"), "retrieval_chunks", type_="unique"
    )
