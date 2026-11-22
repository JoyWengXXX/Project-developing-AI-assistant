"""`project_plans` 的參考來源改為可為空：規劃時可以不使用參考快照（只憑需求與配置）。

降版前須先沒有任何不使用參考的計畫，否則 NOT NULL 無法還原（資料庫會拒絕，不自動刪資料）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("project_plans", "reference_repository_name", nullable=True)
    op.alter_column("project_plans", "reference_commit_sha", nullable=True)
    op.create_check_constraint(
        op.f("ck_project_plans_reference_fields_together"),
        "project_plans",
        "(reference_repository_name IS NULL) = (reference_commit_sha IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_project_plans_reference_fields_together"), "project_plans", type_="check"
    )
    op.alter_column(
        "project_plans", "reference_commit_sha", existing_type=sa.String(40), nullable=False
    )
    op.alter_column(
        "project_plans", "reference_repository_name", existing_type=sa.String(200), nullable=False
    )
