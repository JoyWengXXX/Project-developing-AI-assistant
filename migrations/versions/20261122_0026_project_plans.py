"""新增 `project_plans`：新專案計畫（第五階段 5-1～5-2）。

參考快照以 ON DELETE SET NULL 連結（快照被移除後計畫仍可讀，來源以保存的 repo 名稱與
commit SHA 說明）。降版直接刪除資料表（計畫是衍生資料，不影響快照、檢索與問答）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_plans",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("target_project_config", postgresql.JSONB(), nullable=False),
        sa.Column("project_requirements", sa.Text(), nullable=False),
        sa.Column("reference_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("reference_repository_name", sa.String(200), nullable=False),
        sa.Column("reference_commit_sha", sa.String(40), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("plan_markdown", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(30), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("usage_known", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'confirmed')", name=op.f("ck_project_plans_status_valid")
        ),
        sa.CheckConstraint(
            "(status = 'confirmed') = (confirmed_at IS NOT NULL)",
            name=op.f("ck_project_plans_confirmed_matches_status"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0",
            name=op.f("ck_project_plans_amounts_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["reference_snapshot_id"],
            ["snapshots.id"],
            name=op.f("fk_project_plans_reference_snapshot_id_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_plans")),
    )
    op.create_index(
        "ix_project_plans_reference_snapshot_id", "project_plans", ["reference_snapshot_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_plans_reference_snapshot_id", table_name="project_plans")
    op.drop_table("project_plans")
