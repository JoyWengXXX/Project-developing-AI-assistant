"""GitHub 來源 repo 的註冊（驗證 → 查 GitHub → clone → 寫入資料列）與 fetch。

重要順序（D17／D23／D32）：clone 與呼叫 GitHub 期間**不持有資料庫交易**——每次讀完資料就
`commit()` 結束交易；只有最後的 INSERT 在短交易內完成。去重由部分唯一索引保證，先查再寫的競爭窗口
以 IntegrityError 後重查處理（同 D20）。同一程序內另以「依 repo 分開的鎖」序列化 clone，避免兩個
請求同時 clone 同一個 repo 而浪費時間；跨程序（多 worker）時鎖不生效，由 rename 與唯一索引兜底。
"""

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Repository
from repopilot.errors import (
    GitHubRegistrationDisabledError,
    ManagedStorageError,
    RepositoryBusyError,
    RepositoryNotGitHubError,
)
from repopilot.git.remote import RefChange, clone_repository, fetch_origin
from repopilot.git.repository import ensure_repository_root
from repopilot.github.client import GitHubClient, GitHubRepo
from repopilot.github.repo_ref import parse_github_repo
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import managed_repos
from repopilot.services.github_credentials import get_decrypted_token
from repopilot.services.repositories import (
    get_repository,
    resolve_registered_path,
)

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.github_repositories")

# 一次 fetch 最多回報的 ref 變更數，避免回應過大；超過時 `truncated=True`
MAX_REPORTED_REFS = 100

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


@contextmanager
def exclusive_repo_access(owner: str, name: str) -> Iterator[None]:
    """不等待地取得該 GitHub repo 的行程內鎖（與 clone／fetch 共用）；取不到就拋出 409。

    給「移除 repo」使用（D33）：fetch 可能長達 `GITHUB_CLONE_TIMEOUT_SECONDS`，等鎖會佔住執行緒，
    而且等完之後 clone 目錄多半已被更新，使用者應該自己決定是否重試，所以快速失敗。
    """
    lock = _lock_for(f"{owner}/{name}".lower())
    if not lock.acquire(blocking=False):
        raise RepositoryBusyError(
            "A clone or fetch of this repository is in progress; retry when it finishes."
        )
    try:
        yield
    finally:
        lock.release()


@dataclass(frozen=True)
class FetchResult:
    repository_id: int
    updated_refs: list[RefChange]
    truncated: bool


def find_github_repository(session: Session, owner: str, name: str) -> Repository | None:
    """以不分大小寫的 owner／name 查詢（與部分唯一索引相同的運算式）。"""
    return session.scalars(
        select(Repository).where(
            Repository.source_type == "github",
            func.lower(Repository.github_owner) == owner.lower(),
            func.lower(Repository.github_name) == name.lower(),
        )
    ).first()


def register_github_repository(
    session: Session, *, raw_repo: str, settings: Settings, client: GitHubClient
) -> tuple[Repository, bool]:
    """以 `owner/name`（或 GitHub URL）註冊 repo；回傳 (Repository, 是否新建)。已註冊回既有資料。

    流程：驗證輸入 → 確認受管目錄已設定 → 取 token（可無）→ 查 GitHub（標準大小寫、private、
    預設分支）→ 查是否已註冊 → clone 到暫存目錄 → rename 為最終位置 → 寫入資料列。
    """
    started = time.monotonic()
    ref = parse_github_repo(raw_repo)
    managed_dir = settings.managed_repos_dir
    if managed_dir is None:
        raise GitHubRegistrationDisabledError(
            "GitHub registration is disabled: set MANAGED_REPOS_DIR to an absolute path."
        )

    token = get_decrypted_token(session, settings)
    session.commit()  # 結束讀取交易；呼叫 GitHub 與 clone 期間不持有連線
    info = client.get_repository(ref.owner, ref.name, token)

    existing = find_github_repository(session, info.owner, info.name)
    session.commit()
    if existing is not None:
        return existing, False

    with _lock_for(f"{info.owner}/{info.name}".lower()):
        # 等鎖期間可能已有另一個請求完成註冊
        existing = find_github_repository(session, info.owner, info.name)
        session.commit()
        if existing is not None:
            return existing, False
        repository, created = _clone_and_insert(session, info, token, settings, managed_dir)
    logger.info(
        "github_repository_registered",
        extra={
            "repository_id": repository.id,
            "newly_created": created,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return repository, created


def _clone_and_insert(
    session: Session, info: GitHubRepo, token: str | None, settings: Settings, managed_dir: Path
) -> tuple[Repository, bool]:
    managed_root = managed_repos.prepare_managed_root(managed_dir)
    temp = managed_repos.create_temp_dir(managed_root)
    temp_exists = True
    destination = managed_repos.github_destination(managed_root, info.owner, info.name)
    try:
        clone_repository(
            managed_root,
            temp,
            base_url=settings.github_clone_base_url,
            owner=info.owner,
            name=info.name,
            token=token,
            timeout=settings.github_clone_timeout_seconds,
        )
        try:
            managed_repos.publish(temp, destination)
            temp_exists = False
        except FileExistsError:
            # 最終位置已被占用（多程序競爭，或先前中斷留下未註冊的目錄）；絕不覆蓋或刪除它
            existing = find_github_repository(session, info.owner, info.name)
            session.commit()
            if existing is not None:
                return existing, False
            logger.error("managed_destination_occupied")
            raise ManagedStorageError(
                "The managed directory for this repository already exists but is not registered; "
                "remove it manually and retry."
            ) from None
        except OSError as exc:
            logger.error("managed_publish_failed", extra={"error_type": type(exc).__name__})
            raise ManagedStorageError("Could not move the clone into place.") from None
    finally:
        if temp_exists:
            managed_repos.remove_created_tree(
                temp,
                managed_root=managed_root,
                expected_parent=managed_repos.temp_root(managed_root),
            )

    # 從這裡起 destination 是本次建立的；之後任何失敗（含資料庫）都要清掉它，除非資料列已指向它
    try:
        return _insert_row(session, info, destination), True
    except IntegrityError as exc:
        session.rollback()
        existing = find_github_repository(session, info.owner, info.name)
        session.commit()
        _remove_destination_unless_registered(destination, existing, managed_root)
        if existing is None:
            raise
        logger.info("github_registration_lost_race", extra={"constraint": _constraint_hint(exc)})
        return existing, False
    except BaseException:
        session.rollback()
        _remove_destination_unless_registered(destination, None, managed_root)
        raise


def _insert_row(session: Session, info: GitHubRepo, destination: Path) -> Repository:
    repository = Repository(
        name=f"{info.owner}/{info.name}",
        canonical_local_path=str(destination.resolve(strict=True)),
        source_type="github",
        github_owner=info.owner,
        github_name=info.name,
        github_is_private=info.is_private,
        github_default_branch=info.default_branch,
    )
    session.add(repository)
    session.commit()
    return repository


def _remove_destination_unless_registered(
    destination: Path, existing: Repository | None, managed_root: Path
) -> None:
    if existing is not None and os.path.normcase(existing.canonical_local_path) == os.path.normcase(
        str(destination)
    ):
        return  # 贏得競爭的資料列使用這個位置，不能刪
    managed_repos.remove_created_tree(
        destination, managed_root=managed_root, expected_parent=destination.parent
    )


def _constraint_hint(exc: IntegrityError) -> str:
    text = str(exc.orig)
    return "github_full_name" if "uq_repositories_github_full_name_lower" in text else "other"


def fetch_github_repository(
    session: Session, repository_id: int, settings: Settings
) -> FetchResult:
    """對 GitHub 來源 repo 執行 `git fetch --prune origin`，回報 ref 的新增／更新／移除。

    只在受管目錄內的 repo 執行：先以 `resolve_registered_path` 重新驗證路徑與 repo 根目錄（D28）。
    local 來源回 `RepositoryNotGitHubError`（409）。
    """
    repository = get_repository(session, repository_id)
    if repository.source_type != "github":
        raise RepositoryNotGitHubError("Only GitHub repositories can be fetched.")
    if settings.managed_repos_dir is None:
        raise GitHubRegistrationDisabledError(
            "GitHub support is disabled: set MANAGED_REPOS_DIR to an absolute path."
        )
    path = resolve_registered_path(repository, settings)
    key = f"{repository.github_owner}/{repository.github_name}".lower()
    token = get_decrypted_token(session, settings)
    session.commit()  # fetch 期間不持有資料庫交易

    ensure_repository_root(path, timeout=settings.git_timeout_seconds)
    started = time.monotonic()
    with _lock_for(key):
        changes = fetch_origin(
            path,
            base_url=settings.github_clone_base_url,
            token=token,
            timeout=settings.github_clone_timeout_seconds,
        )
    logger.info(
        "github_repository_fetched",
        extra={
            "repository_id": repository_id,
            "changed_refs": len(changes),
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return FetchResult(
        repository_id=repository_id,
        updated_refs=changes[:MAX_REPORTED_REFS],
        truncated=len(changes) > MAX_REPORTED_REFS,
    )
