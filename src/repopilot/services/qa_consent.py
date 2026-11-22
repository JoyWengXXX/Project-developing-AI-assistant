"""逐 repo 資料外送同意（第四階段 4-1c，規格 D2）：沒有資料列＝未同意（預設），撤回即刪除。"""

import logging
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.db.models import Repository, RepositoryQaConsent
from repopilot.errors import RepositoryNotFoundError
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services.repositories import get_repository

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.qa_consent")


def get_consented_at(session: Session, repository_id: int) -> datetime | None:
    get_repository(session, repository_id)
    return session.scalar(
        select(RepositoryQaConsent.consented_at).where(
            RepositoryQaConsent.repository_id == repository_id
        )
    )


def grant(session: Session, repository_id: int, *, now: datetime) -> datetime:
    """同意；冪等，已同意時保留第一次同意的時間（重複 PUT 不會改寫紀錄）。"""
    get_repository(session, repository_id)
    try:
        session.execute(
            insert(RepositoryQaConsent)
            .values(repository_id=repository_id, consented_at=now)
            .on_conflict_do_nothing(index_elements=[RepositoryQaConsent.repository_id])
        )
        session.commit()
    except IntegrityError:
        # 檢查之後 repo 才被移除（外鍵失敗）
        session.rollback()
        raise RepositoryNotFoundError("Repository not found.") from None
    logger.info("qa_consent_granted", extra={"repository_id": repository_id})
    consented_at = session.scalar(
        select(RepositoryQaConsent.consented_at).where(
            RepositoryQaConsent.repository_id == repository_id
        )
    )
    return consented_at if consented_at is not None else now


def revoke(session: Session, repository_id: int) -> None:
    """撤回；冪等。"""
    get_repository(session, repository_id)
    session.execute(
        delete(RepositoryQaConsent).where(RepositoryQaConsent.repository_id == repository_id)
    )
    session.commit()
    logger.info("qa_consent_revoked", extra={"repository_id": repository_id})


def list_consents(session: Session) -> list[RepositoryQaConsent]:
    return list(
        session.scalars(select(RepositoryQaConsent).order_by(RepositoryQaConsent.repository_id))
    )


def missing_consents(session: Session, repository_ids: Collection[int]) -> list[tuple[int, str]]:
    """尚未同意資料外送的 repo（id、名稱），依 id 排序；全部已同意時為空。"""
    if not repository_ids:
        return []
    consented = set(
        session.scalars(
            select(RepositoryQaConsent.repository_id).where(
                RepositoryQaConsent.repository_id.in_(repository_ids)
            )
        )
    )
    missing = set(repository_ids) - consented
    if not missing:
        return []
    rows = session.execute(
        select(Repository.id, Repository.name)
        .where(Repository.id.in_(missing))
        .order_by(Repository.id)
    ).all()
    return [(row[0], row[1]) for row in rows]
