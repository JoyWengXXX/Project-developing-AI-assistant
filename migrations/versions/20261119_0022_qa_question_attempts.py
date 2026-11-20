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
    # 檢查是否有 qa_questions 存在，若有則拒絕降版
    result = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM qa_questions")).scalar()
    if result and result > 0:
        # 建立一個會失敗的檢查約束，強制降版失敗
        op.create_check_constraint(
            "downgrade_blocked_qa_questions_exist",
            "qa_questions",
            "FALSE",  # 此條件永遠為假，會導致降版失敗
        )

    op.drop_constraint(op.f("ck_qa_questions_attempts_nonnegative"), "qa_questions", type_="check")
    op.drop_column("qa_questions", "attempts")
