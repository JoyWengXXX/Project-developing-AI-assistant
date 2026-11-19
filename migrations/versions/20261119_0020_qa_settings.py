"""建立問答設定（單列）與逐 repo 資料外送同意；降版時若已有資料就拒絕，不靜默刪除。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qa_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("provider", sa.String(20), server_default=sa.text("'anthropic'"), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("per_request_token_limit", sa.Integer(), nullable=True),
        sa.Column("daily_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("daily_cost_limit_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("price_table", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("price_table_as_of", sa.Date(), nullable=False),
        sa.Column("encrypted_provider_key", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_qa_settings_single_row")),
        sa.CheckConstraint("provider IN ('anthropic')", name=op.f("ck_qa_settings_provider_valid")),
        sa.CheckConstraint(
            "per_request_token_limit IS NULL OR per_request_token_limit > 0",
            name=op.f("ck_qa_settings_per_request_token_limit_positive"),
        ),
        sa.CheckConstraint(
            "daily_token_limit IS NULL OR daily_token_limit > 0",
            name=op.f("ck_qa_settings_daily_token_limit_positive"),
        ),
        sa.CheckConstraint(
            "daily_cost_limit_usd IS NULL OR daily_cost_limit_usd > 0",
            name=op.f("ck_qa_settings_daily_cost_limit_usd_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_settings")),
    )
    op.create_table(
        "repository_qa_consent",
        sa.Column("repository_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_repository_qa_consent_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("repository_id", name=op.f("pk_repository_qa_consent")),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qa_settings) OR EXISTS (SELECT 1 FROM repository_qa_consent)
            THEN
                RAISE EXCEPTION 'Cannot downgrade while qa settings or qa consents exist.';
            END IF;
        END;
        $$
        """
    )
    op.drop_table("repository_qa_consent")
    op.drop_table("qa_settings")
