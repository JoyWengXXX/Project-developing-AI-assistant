"""建立 github_credentials 資料表（單列，保存加密後的 GitHub token）

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_credentials",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("login", sa.String(length=100), nullable=False),
        sa.Column("token_type", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 固定 id = 1 的主鍵 + CHECK：資料庫層保證最多一筆
        sa.CheckConstraint("id = 1", name=op.f("ck_github_credentials_single_row")),
        sa.CheckConstraint(
            "token_type IN ('fine_grained', 'classic', 'unknown')",
            name=op.f("ck_github_credentials_type_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_github_credentials")),
    )


def downgrade() -> None:
    op.drop_table("github_credentials")
