"""建立 repository_references（每個 repo 最多一個參考版本）

第二階段切片 3b（D41）。
- `snapshots` 新增冗餘的唯一約束 `(id, repository_id)`：`id` 本來就唯一，所以對既有資料一定成立
  （不會因資料而失敗），只多一個小索引。目的是讓 `repository_references` 以複合外鍵
  `(snapshot_id, repository_id) → snapshots(id, repository_id)` 由資料庫保證「參考的快照屬於
  同一個 repo」，不必只靠服務層檢查。
- `repository_references.repository_id` 唯一且 ON DELETE CASCADE；複合外鍵 ON DELETE CASCADE，
  所以 D33 移除 repo（先刪快照）與移除快照時，參考自然消失，D33 的刪除順序不必修改。

降版（downgrade）會刪除 `repository_references`（參考標記一併消失）並移除新增的唯一約束；
快照與其他第一、二階段資料不受影響。

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        op.f("uq_snapshots_id_repository_id"), "snapshots", ["id", "repository_id"]
    )
    op.create_table(
        "repository_references",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_repository_references_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "repository_id"],
            ["snapshots.id", "snapshots.repository_id"],
            name=op.f("fk_repository_references_snapshot_id_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_references")),
        sa.UniqueConstraint("repository_id", name=op.f("uq_repository_references_repository_id")),
    )
    op.create_index(
        op.f("ix_repository_references_snapshot_id"),
        "repository_references",
        ["snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_repository_references_snapshot_id"), table_name="repository_references")
    op.drop_table("repository_references")
    op.drop_constraint(op.f("uq_snapshots_id_repository_id"), "snapshots", type_="unique")
