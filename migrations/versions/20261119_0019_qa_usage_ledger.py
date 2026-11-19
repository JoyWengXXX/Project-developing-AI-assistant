"""建立問答額度帳本（只追加）；降版時若已有帳本事件就拒絕，不靜默刪除。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qa_usage_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(10), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("usage_unknown", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('reserve', 'settle', 'release', 'reset')",
            name=op.f("ck_qa_usage_ledger_event_type_valid"),
        ),
        sa.CheckConstraint(
            "(event_type = 'reset') = (reservation_id IS NULL)",
            name=op.f("ck_qa_usage_ledger_reservation_matches_event"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0",
            name=op.f("ck_qa_usage_ledger_amounts_nonnegative"),
        ),
        sa.CheckConstraint(
            "event_type <> 'reset' OR (input_tokens = 0 AND output_tokens = 0 AND cost_usd = 0)",
            name=op.f("ck_qa_usage_ledger_reset_amounts_zero"),
        ),
        sa.CheckConstraint(
            "NOT usage_unknown OR event_type = 'settle'",
            name=op.f("ck_qa_usage_ledger_usage_unknown_only_on_settle"),
        ),
    )
    op.create_index(
        "uq_qa_usage_ledger_reservation_id_reserve",
        "qa_usage_ledger",
        ["reservation_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'reserve'"),
    )
    op.create_index(
        "uq_qa_usage_ledger_reservation_id_final",
        "qa_usage_ledger",
        ["reservation_id"],
        unique=True,
        postgresql_where=sa.text("event_type IN ('settle', 'release')"),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qa_usage_ledger) THEN
                RAISE EXCEPTION 'Cannot downgrade while qa usage ledger events exist.';
            END IF;
        END;
        $$
        """
    )
    op.drop_table("qa_usage_ledger")
