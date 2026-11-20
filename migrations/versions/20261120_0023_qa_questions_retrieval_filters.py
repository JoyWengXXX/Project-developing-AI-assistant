"""問答記錄新增 `retrieval_filters`（當次檢索範圍：`file_kinds`、`path_prefix`；CL-01）。

沒有篩選時為 NULL。降版會丟掉範圍：已有問答記錄過範圍就拒絕，不靜默刪除。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qa_questions WHERE retrieval_filters IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while qa questions record retrieval filters.';
            END IF;
        END;
        $$
        """
    )
    op.drop_column("qa_questions", "retrieval_filters")
