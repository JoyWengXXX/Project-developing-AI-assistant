"""啟用向量擴充；降版不移除可能由其他用途共用的擴充。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # 擴充可能存在於 migration 之前，或被其他資料表使用。
    pass
