from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from repopilot.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Repository(Base):
    """已註冊的本機 Git repo；不綁定「參考／目標」角色，由分析任務選擇。"""

    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # 唯一約束名稱由 naming convention 產生：uq_repositories_canonical_local_path
    canonical_local_path: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
