"""0023: QaQuestion 新增 retrieval_filters 欄位

revision: 0023
Revises: 0022
Create Date: 2026-11-20 12:00:00.000000

新增欄位用於儲存檢索篩選（file_kinds、path_prefix）。

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-20 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 新增 retrieval_filters 欄位：檢索時的篩選條件（file_kinds、path_prefix）
    op.add_column(
        "qa_questions",
        sa.Column(
            "retrieval_filters",
            JSONB(),
            nullable=True,
            comment="檢索篩選條件，包含 file_kinds (列表) 和 path_prefix (字串)",
        ),
    )


def downgrade() -> None:
    # 刪除欄位
    op.drop_column("qa_questions", "retrieval_filters")
