"""問答記錄新增 `attempts`（實際呼叫供應商的次數，含重試）。

降版會丟掉次數：已有問答記錄過嘗試次數就拒絕，不靜默刪除。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "qa_questions",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_qa_questions_attempts_nonnegative"), "qa_questions", "attempts >= 0"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qa_questions WHERE attempts > 0) THEN
                RAISE EXCEPTION 'Cannot downgrade while qa questions record attempts.';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(op.f("ck_qa_questions_attempts_nonnegative"), "qa_questions", type_="check")
    op.drop_column("qa_questions", "attempts")
