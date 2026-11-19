"""建立問答追蹤記錄（問題、送出片段、陳述與引用）並允許 qa_question 背景工作。

降版時若已有問答或 qa_question 工作就拒絕，不靜默刪除。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "kind IN ('snapshot_import', 'snapshot_index', 'github_register', 'github_fetch', "
    "'snapshot_retrieval')"
)
_NEW_KINDS = (
    "kind IN ('snapshot_import', 'snapshot_index', 'github_register', 'github_fetch', "
    "'snapshot_retrieval', 'qa_question')"
)
_OLD_PHASES = (
    "phase IN ('pending', 'importing', 'indexing', 'resolving', 'cloning', "
    "'registering', 'fetching', 'embedding', 'completed')"
)
_NEW_PHASES = (
    "phase IN ('pending', 'importing', 'indexing', 'resolving', 'cloning', "
    "'registering', 'fetching', 'embedding', 'retrieving', 'generating', 'completed')"
)


def _replace_job_constraints(kinds: str, phases: str) -> None:
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_phase_valid"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind_valid"), "jobs", kinds)
    op.create_check_constraint(op.f("ck_jobs_phase_valid"), "jobs", phases)


def upgrade() -> None:
    # 一個問答跨多個快照，所以 qa_question 工作的 repository_id／snapshot_id 一律為 NULL
    # （兩欄本來就可為 NULL）；只需要放寬 kind 與 phase 的值域。
    _replace_job_constraints(_NEW_KINDS, _NEW_PHASES)

    op.create_table(
        "qa_questions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("snapshot_ids", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("answer_status", sa.String(25), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(20), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("estimated_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("usage_unknown", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("provider_request_id", sa.String(100), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_qa_questions_status_valid"),
        ),
        sa.CheckConstraint(
            "answer_status IN ('answered', 'insufficient_evidence')",
            name=op.f("ck_qa_questions_answer_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (answer_status IS NOT NULL)",
            name=op.f("ck_qa_questions_answer_matches_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name=op.f("ck_qa_questions_finished_matches_status"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR error_code IS NOT NULL",
            name=op.f("ck_qa_questions_failed_has_error"),
        ),
        sa.CheckConstraint(
            "cardinality(snapshot_ids) >= 1", name=op.f("ck_qa_questions_snapshot_ids_nonempty")
        ),
        sa.CheckConstraint("max_tokens >= 1", name=op.f("ck_qa_questions_max_tokens_positive")),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0)"
            " AND (output_tokens IS NULL OR output_tokens >= 0)"
            " AND (cost_usd IS NULL OR cost_usd >= 0)"
            " AND estimated_input_tokens >= 0 AND estimated_cost_usd >= 0",
            name=op.f("ck_qa_questions_amounts_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_qa_questions_job_id_jobs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_questions")),
    )
    op.create_index("ix_qa_questions_status_id", "qa_questions", ["status", "id"])
    op.create_index("ix_qa_questions_job_id", "qa_questions", ["job_id"])

    op.create_table(
        "qa_context_chunks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(10), nullable=True),
        sa.Column("skip_reason", sa.String(20), nullable=True),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("repo_name", sa.String(200), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "(code IS NULL) = (skip_reason IS NOT NULL)",
            name=op.f("ck_qa_context_chunks_code_matches_skip"),
        ),
        sa.CheckConstraint(
            "skip_reason IN ('count_cap', 'budget_exhausted', 'empty_text')",
            name=op.f("ck_qa_context_chunks_skip_reason_valid"),
        ),
        sa.CheckConstraint(
            "start_line >= 1 AND end_line >= start_line",
            name=op.f("ck_qa_context_chunks_lines_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["qa_questions.id"],
            name=op.f("fk_qa_context_chunks_question_id_qa_questions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_context_chunks")),
        sa.UniqueConstraint(
            "question_id", "rank", name=op.f("uq_qa_context_chunks_question_id_rank")
        ),
        sa.UniqueConstraint(
            "question_id", "code", name=op.f("uq_qa_context_chunks_question_id_code")
        ),
    )

    op.create_table(
        "qa_statements",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(15), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column(
            "rejection_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('fact', 'inference', 'open_question')",
            name=op.f("ck_qa_statements_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('valid', 'invalid')", name=op.f("ck_qa_statements_status_valid")
        ),
        sa.CheckConstraint(
            "status = 'valid' OR jsonb_array_length(rejection_reasons) > 0",
            name=op.f("ck_qa_statements_invalid_has_reasons"),
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["qa_questions.id"],
            name=op.f("fk_qa_statements_question_id_qa_questions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_statements")),
        sa.UniqueConstraint(
            "question_id", "position", name=op.f("uq_qa_statements_question_id_position")
        ),
    )

    op.create_table(
        "qa_citations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("statement_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("claimed_chunk", sa.String(50), nullable=False),
        sa.Column("claimed_start_line", sa.BigInteger(), nullable=False),
        sa.Column("claimed_end_line", sa.BigInteger(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.ARRAY(sa.String(40)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("resolved_chunk_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_repository_id", sa.Integer(), nullable=True),
        sa.Column("resolved_repo_name", sa.String(200), nullable=True),
        sa.Column("resolved_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("resolved_commit_sha", sa.String(40), nullable=True),
        sa.Column("resolved_path", sa.Text(), nullable=True),
        sa.Column("resolved_start_line", sa.Integer(), nullable=True),
        sa.Column("resolved_end_line", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "valid = (cardinality(reasons) = 0)",
            name=op.f("ck_qa_citations_valid_matches_reasons"),
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["qa_statements.id"],
            name=op.f("fk_qa_citations_statement_id_qa_statements"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_citations")),
        sa.UniqueConstraint(
            "statement_id", "position", name=op.f("uq_qa_citations_statement_id_position")
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM qa_questions) THEN
                RAISE EXCEPTION 'Cannot downgrade while qa questions exist.';
            END IF;
            IF EXISTS (
                SELECT 1 FROM jobs
                WHERE kind = 'qa_question' OR phase IN ('retrieving', 'generating')
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while qa question jobs exist.';
            END IF;
        END;
        $$
        """
    )
    op.drop_table("qa_citations")
    op.drop_table("qa_statements")
    op.drop_table("qa_context_chunks")
    op.drop_table("qa_questions")
    _replace_job_constraints(_OLD_KINDS, _OLD_PHASES)
