"""建立 migration 基線（尚無資料表）

Revision ID: 0001
Revises:
Create Date: 2026-10-24
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 切片 2 只建立 migration 流程；資料表由後續切片的 migration 加入
    pass


def downgrade() -> None:
    pass
