import logging
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.db.models import Repository
from repopilot.errors import InvalidRepositoryPathError, RepositoryNotFoundError
from repopilot.git.repository import ensure_repository_root
from repopilot.logging_config import APP_LOGGER_NAME

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.repositories")

PATH_UNIQUE_CONSTRAINT = "uq_repositories_canonical_local_path"


def resolve_repository_path(raw_path: str, roots: Sequence[Path]) -> Path:
    """將使用者提供的路徑正規化，並確認位於 `roots` 之內。

    `resolve()` 會展開 symlink、`..`、8.3 短檔名並還原實際大小寫，因此指向允許範圍外的
    symlink 會在這裡被拒絕。比較使用 `Path.is_relative_to`，不使用字串前綴。
    """
    if not raw_path.strip() or "\x00" in raw_path:
        raise InvalidRepositoryPathError("The path is empty or contains invalid characters.")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise InvalidRepositoryPathError("The path must be absolute.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise InvalidRepositoryPathError("The path does not exist.") from None
    if not resolved.is_dir():
        raise InvalidRepositoryPathError("The path is not a directory.")

    resolved_roots = _resolve_roots(roots)
    if not resolved_roots:
        raise InvalidRepositoryPathError("No REPOSITORY_ROOTS are configured.")
    if not any(resolved.is_relative_to(root) for root in resolved_roots):
        raise InvalidRepositoryPathError("The path is outside the allowed REPOSITORY_ROOTS.")
    return resolved


def _resolve_roots(roots: Sequence[Path]) -> list[Path]:
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            logger.warning("repository_root_unavailable")
    return resolved


def register_repository(
    session: Session,
    *,
    raw_path: str,
    name: str | None,
    roots: Sequence[Path],
    git_timeout: float,
) -> tuple[Repository, bool]:
    """註冊本機 repo；回傳 (Repository, 是否新建)。相同 canonical path 為冪等。"""
    path = resolve_repository_path(raw_path, roots)
    ensure_repository_root(path, timeout=git_timeout)
    canonical = str(path)

    existing = _find_by_path(session, canonical)
    if existing is not None:
        return existing, False

    repository = Repository(name=name or path.name, canonical_local_path=canonical)
    session.add(repository)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # 先查再寫有競爭窗口；以約束名稱確認是路徑重複，再回傳贏得競爭的那一筆
        if PATH_UNIQUE_CONSTRAINT not in str(exc.orig):
            raise
        existing = _find_by_path(session, canonical)
        if existing is None:
            raise
        return existing, False
    logger.info("repository_registered", extra={"repository_id": repository.id})
    return repository, True


def _find_by_path(session: Session, canonical: str) -> Repository | None:
    return session.scalars(
        select(Repository).where(Repository.canonical_local_path == canonical)
    ).first()


def list_repositories(session: Session) -> list[Repository]:
    return list(session.scalars(select(Repository).order_by(Repository.id)))


def get_repository(session: Session, repository_id: int) -> Repository:
    repository = session.get(Repository, repository_id)
    if repository is None:
        raise RepositoryNotFoundError("Repository not found.")
    return repository
