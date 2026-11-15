"""允許檢索索引背景工作；降版時保護工作歷史。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = "kind IN ('snapshot_import', 'snapshot_index', 'github_register', 'github_fetch')"
_NEW_KINDS = (
    "kind IN ('snapshot_import', 'snapshot_index', 'github_register', 'github_fetch', "
    "'snapshot_retrieval')"
)
_OLD_PHASES = (
    "phase IN ('pending', 'importing', 'indexing', 'resolving', 'cloning', "
    "'registering', 'fetching', 'completed')"
)
_NEW_PHASES = (
    "phase IN ('pending', 'importing', 'indexing', 'resolving', 'cloning', "
    "'registering', 'fetching', 'embedding', 'completed')"
)


def _replace_constraints(kinds: str, phases: str) -> None:
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_phase_valid"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind_valid"), "jobs", kinds)
    op.create_check_constraint(op.f("ck_jobs_phase_valid"), "jobs", phases)


def upgrade() -> None:
    _replace_constraints(_NEW_KINDS, _NEW_PHASES)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM jobs WHERE kind = 'snapshot_retrieval' OR phase = 'embedding'
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while retrieval jobs exist.';
            END IF;
        END;
        $$
        """
    )
    _replace_constraints(_OLD_KINDS, _OLD_PHASES)
