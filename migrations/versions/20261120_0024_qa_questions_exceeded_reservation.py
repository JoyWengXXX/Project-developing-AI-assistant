"""問答記錄新增 `exceeded_reservation`（實際用量超過預留；CL-03）。

降版會丟掉標記：已有問答被標記過就拒絕，不靜默刪除。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "qa_questions",
        sa.Column(
            "exceeded_reservation", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qa_questions WHERE exceeded_reservation) THEN
                RAISE EXCEPTION 'Cannot downgrade while qa questions record exceeded reservations.';
            END IF;
        END;
        $$
        """
    )
    op.drop_column("qa_questions", "exceeded_reservation")
