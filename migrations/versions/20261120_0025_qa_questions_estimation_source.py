"""問答記錄新增 `estimation_source`（預留輸入 token 的來源：`provider_count` 或 `local_estimate`）。

只是說明用的追蹤欄位，NULL 表示還沒預留（或是加入欄位之前的記錄）；降版直接刪除欄位。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("qa_questions", sa.Column("estimation_source", sa.String(20), nullable=True))
    op.create_check_constraint(
        op.f("ck_qa_questions_estimation_source_known"),
        "qa_questions",
        "estimation_source IN ('provider_count', 'local_estimate')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_qa_questions_estimation_source_known"), "qa_questions", type_="check"
    )
    op.drop_column("qa_questions", "estimation_source")
