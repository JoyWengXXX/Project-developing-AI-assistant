"""repositories 新增 GitHub 來源欄位（source_type、owner、name、private、default branch）

既有資料列不受影響：source_type 預設為 local，GitHub 欄位為 NULL。

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default 讓既有列在加欄位時直接取得 'local'，不需另寫 UPDATE
    op.add_column(
        "repositories",
        sa.Column(
            "source_type", sa.String(length=10), server_default=sa.text("'local'"), nullable=False
        ),
    )
    op.add_column("repositories", sa.Column("github_owner", sa.String(length=39), nullable=True))
    op.add_column("repositories", sa.Column("github_name", sa.String(length=100), nullable=True))
    op.add_column("repositories", sa.Column("github_is_private", sa.Boolean(), nullable=True))
    op.add_column(
        "repositories", sa.Column("github_default_branch", sa.String(length=255), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_repositories_source_type_valid"),
        "repositories",
        "source_type IN ('local', 'github')",
    )
    op.create_check_constraint(
        op.f("ck_repositories_github_fields_match_source"),
        "repositories",
        "(source_type = 'github' AND github_owner IS NOT NULL AND github_name IS NOT NULL)"
        " OR (source_type = 'local' AND github_owner IS NULL AND github_name IS NULL)",
    )
    # 只對 GitHub 來源去重，且不分大小寫（GitHub 的 owner／name 不分大小寫）
    op.create_index(
        op.f("uq_repositories_github_full_name_lower"),
        "repositories",
        [sa.text("lower(github_owner)"), sa.text("lower(github_name)")],
        unique=True,
        postgresql_where=sa.text("source_type = 'github'"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("uq_repositories_github_full_name_lower"),
        table_name="repositories",
        postgresql_where=sa.text("source_type = 'github'"),
    )
    op.drop_constraint(
        op.f("ck_repositories_github_fields_match_source"), "repositories", type_="check"
    )
    op.drop_constraint(op.f("ck_repositories_source_type_valid"), "repositories", type_="check")
    op.drop_column("repositories", "github_default_branch")
    op.drop_column("repositories", "github_is_private")
    op.drop_column("repositories", "github_name")
    op.drop_column("repositories", "github_owner")
    op.drop_column("repositories", "source_type")
