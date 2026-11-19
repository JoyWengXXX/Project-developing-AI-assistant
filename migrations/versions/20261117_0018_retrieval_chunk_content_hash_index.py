"""為片段內容雜湊建立索引，供相同內容的向量重用查詢；降版只移除索引，不刪資料。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_retrieval_chunks_content_hash", "retrieval_chunks", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_retrieval_chunks_content_hash", table_name="retrieval_chunks")
