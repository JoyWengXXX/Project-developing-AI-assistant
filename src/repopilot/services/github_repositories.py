"""GitHub 來源 repo 的註冊（驗證 → 查 GitHub → clone → 寫入資料列）與 fetch。

重要順序（D17／D23／D32）：clone 與呼叫 GitHub 期間**不持有資料庫交易**——每次讀完資料就
`commit()` 結束交易；只有最後的 INSERT 在短交易內完成。去重由部分唯一索引保證，先查再寫的競爭窗口
以 IntegrityError 後重查處理（同 D20）。

互斥（第二階段切片 5，D44）：同一個 repo 的 clone／fetch 由 PostgreSQL **advisory lock 的獨占鎖**
序列化（`db/locks.py`，鍵＝不分大小寫的 owner／name，所以註冊階段資料列還不存在也能鎖），跨程序
（API 與 worker、多個 worker）有效；D32 的行程內 `threading.Lock` 已被它取代。同步端點遇到鎖忙碌時
**等待**（原本就是「等前一個做完」，最多 `GITHUB_CLONE_TIMEOUT_SECONDS`）；worker 取不到就延後重排。

共用實作（C1＝B，同步端點與 worker 不複製邏輯）：`clone_and_publish`（clone 到暫存目錄、rename 為
最終位置，可注入「記錄暫存目錄」「輪詢（取消、進度）」「發布前檢查點」）與 `run_fetch`（fetch 並整理
結果）。兩條路徑的差別只在「怎麼寫資料列」：同步端點自己 INSERT 後提交，worker 則在完成 job 的同一
交易內 INSERT（`services/github_job_runner.py`），取消與成功由列鎖決定先後。
"""

import functools
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import (
    AdvisoryLock,
    acquire_advisory_lock_wait,
    engine_of,
    repo_lock_key,
)
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
    github_repo_key,
    resolve_registered_path,
)

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.github_repositories")

# 一次 fetch 最多回報的 ref 變更數，避免回應過大；超過時 `truncated=True`
MAX_REPORTED_REFS = 100

_BUSY_MESSAGE = "A clone or fetch of this repository is in progress; retry when it finishes."


@dataclass(frozen=True)
class FetchResult:
    repository_id: int
    updated_refs: list[RefChange]
    truncated: bool


@dataclass(frozen=True)
class CloneHooks:
    """clone 流程的注入點（同步端點只給 `checkpoint`，worker 全給）。

    - `record_temp_dir(name)`：決定暫存目錄名稱後、**建立目錄之前**呼叫
      （寫入 `work_state.temp_dir`）。
    - `poll(temp_path)`：clone 期間每 `poll_interval` 秒呼叫；可拋例外中止（取消、租約遺失），
      也用來統計暫存目錄大小。
    - `checkpoint()`：clone 完成、rename 為最終位置之前呼叫（確認鎖連線仍存活、取消旗標等）。
    """

    record_temp_dir: Callable[[str], None] | None = None
    poll: Callable[[Path], None] | None = None
    poll_interval: float = 1.0
    checkpoint: Callable[[], None] | None = None


@dataclass(frozen=True)
class PublishedClone:
    """已 rename 到最終位置的 clone（本次建立的；之後任何失敗都要清掉它，除非資料列已指向它）。"""

    destination: Path
    managed_root: Path


class DestinationOccupiedError(Exception):
    """最終位置已被占用（多程序競爭，或先前中斷留下未註冊的目錄）；絕不覆蓋或刪除它。"""


def find_github_repository(session: Session, owner: str, name: str) -> Repository | None:
    """以不分大小寫的 owner／name 查詢（與部分唯一索引相同的運算式）。"""
    return session.scalars(
        select(Repository).where(
            Repository.source_type == "github",
            func.lower(Repository.github_owner) == owner.lower(),
            func.lower(Repository.github_name) == name.lower(),
        )
    ).first()


def acquire_repo_lock_waiting(session: Session, key: str, settings: Settings) -> AdvisoryLock:
    """同步端點用：等到取得 repo 獨占鎖（最多 clone 逾時），取不到回 409 `repository_busy`。"""
    lock = acquire_advisory_lock_wait(
        engine_of(session),
        repo_lock_key(key),
        timeout=settings.github_clone_timeout_seconds,
    )
    if lock is None:
        raise RepositoryBusyError(_BUSY_MESSAGE)
    return lock


def new_repository_row(
    info: GitHubRepo, destination: Path, *, host_path: str | None = None
) -> Repository:
    """GitHub 來源的資料列。`host_path`：worker 在容器內建立的目錄要以**主機路徑**入庫（D18）。"""
    return Repository(
        name=f"{info.owner}/{info.name}",
        canonical_local_path=(
            host_path if host_path is not None else str(destination.resolve(strict=True))
        ),
        source_type="github",
        github_owner=info.owner,
        github_name=info.name,
        github_is_private=info.is_private,
        github_default_branch=info.default_branch,
    )


def register_github_repository(
    session: Session, *, raw_repo: str, settings: Settings, client: GitHubClient
) -> tuple[Repository, bool]:
    """以 `owner/name`（或 GitHub URL）註冊 repo；回傳 (Repository, 是否新建)。已註冊回既有資料。

    流程：驗證輸入 → 確認受管目錄已設定 → 取 token（可無）→ 查 GitHub（標準大小寫、private、
    預設分支）→ 查是否已註冊 → 取 repo 獨占鎖 → clone 到暫存目錄 → rename 為最終位置 → 寫入資料列。
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

    lock = acquire_repo_lock_waiting(session, github_repo_key(info.owner, info.name), settings)
    try:
        # 等鎖期間可能已有另一個請求（或 worker）完成註冊
        existing = find_github_repository(session, info.owner, info.name)
        session.commit()
        if existing is not None:
            return existing, False
        repository, created = _clone_and_insert(
            session, info, token, settings, managed_dir, CloneHooks(checkpoint=lock.assert_alive)
        )
    finally:
        lock.release()
    logger.info(
        "github_repository_registered",
        extra={
            "repository_id": repository.id,
            "newly_created": created,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return repository, created


def clone_and_publish(
    info: GitHubRepo,
    token: str | None,
    settings: Settings,
    managed_dir: Path,
    hooks: CloneHooks | None = None,
) -> PublishedClone:
    """clone 到 `.tmp/<隨機>`，成功後 rename 為 `github/<owner>/<name>`（呼叫端須已持有獨占鎖）。

    暫存目錄的清理只針對本次建立的路徑（`remove_created_tree` 驗證）；最終位置已被占用拋
    `DestinationOccupiedError`（絕不覆蓋或刪除）；其他檔案系統失敗為 `ManagedStorageError`。
    `hooks` 的例外（取消、租約遺失）照常往外拋，暫存目錄在 `finally` 清掉。
    """
    hooks = hooks or CloneHooks()
    managed_root = managed_repos.prepare_managed_root(managed_dir)
    temp = managed_repos.new_temp_path(managed_root)
    if hooks.record_temp_dir is not None:
        hooks.record_temp_dir(temp.name)  # 先記錄、再建立：崩潰時才有紀錄可清理（規格 5.12）
    managed_repos.create_temp_dir_at(temp)
    temp_exists = True
    destination = managed_repos.github_destination(managed_root, info.owner, info.name)
    hook_poll = hooks.poll
    poll: Callable[[], None] | None = (
        None if hook_poll is None else functools.partial(hook_poll, temp)
    )
    try:
        clone_repository(
            managed_root,
            temp,
            base_url=settings.github_clone_base_url,
            owner=info.owner,
            name=info.name,
            token=token,
            timeout=settings.github_clone_timeout_seconds,
            poll=poll,
            poll_interval=hooks.poll_interval,
        )
        if hooks.checkpoint is not None:
            hooks.checkpoint()
        try:
            managed_repos.publish(temp, destination)
            temp_exists = False
        except FileExistsError:
            # 最終位置已被占用；絕不覆蓋或刪除它
            raise DestinationOccupiedError from None
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
    return PublishedClone(destination, managed_root)


def occupied_destination_error() -> ManagedStorageError:
    logger.error("managed_destination_occupied")
    return ManagedStorageError(
        "The managed directory for this repository already exists but is not registered; "
        "remove it manually and retry."
    )


def _clone_and_insert(
    session: Session,
    info: GitHubRepo,
    token: str | None,
    settings: Settings,
    managed_dir: Path,
    hooks: CloneHooks,
) -> tuple[Repository, bool]:
    try:
        published = clone_and_publish(info, token, settings, managed_dir, hooks)
    except DestinationOccupiedError:
        existing = find_github_repository(session, info.owner, info.name)
        session.commit()
        if existing is not None:
            return existing, False
        raise occupied_destination_error() from None

    # 從這裡起 destination 是本次建立的；之後任何失敗（含資料庫）都要清掉它，除非資料列已指向它
    destination, managed_root = published.destination, published.managed_root
    try:
        return _insert_row(session, info, destination), True
    except IntegrityError as exc:
        session.rollback()
        existing = find_github_repository(session, info.owner, info.name)
        session.commit()
        remove_destination_unless_registered(destination, existing, managed_root)
        if existing is None:
            raise
        logger.info("github_registration_lost_race", extra={"constraint": _constraint_hint(exc)})
        return existing, False
    except BaseException:
        session.rollback()
        remove_destination_unless_registered(destination, None, managed_root)
        raise


def _insert_row(session: Session, info: GitHubRepo, destination: Path) -> Repository:
    repository = new_repository_row(info, destination)
    session.add(repository)
    session.commit()
    return repository


def remove_destination_unless_registered(
    destination: Path,
    existing: Repository | None,
    managed_root: Path,
    *,
    registered_path: str | None = None,
) -> None:
    """清掉本次 rename 出來的最終位置，除非贏得競爭的資料列正使用這個位置。

    `registered_path`：資料列記錄的是主機路徑，worker（容器內）要傳入 `destination` 對應的主機路徑
    才能比對；省略時以 `destination` 本身比對（API 與資料庫同在主機）。
    """
    expected = registered_path if registered_path is not None else str(destination)
    if existing is not None and os.path.normcase(existing.canonical_local_path) == os.path.normcase(
        expected
    ):
        return  # 贏得競爭的資料列使用這個位置，不能刪
    managed_repos.remove_created_tree(
        destination, managed_root=managed_root, expected_parent=destination.parent
    )


def _constraint_hint(exc: IntegrityError) -> str:
    text = str(exc.orig)
    return "github_full_name" if "uq_repositories_github_full_name_lower" in text else "other"


def run_fetch(
    path: Path,
    token: str | None,
    settings: Settings,
    *,
    repository_id: int,
    poll: Callable[[], None] | None = None,
) -> tuple[FetchResult, int]:
    """對已驗證的 clone 路徑執行 `git fetch --prune origin`；回傳 (最多 100 筆的結果, 實際變更數)。

    同步端點與 worker 共用（呼叫端須已持有 repo 獨占鎖、已做路徑驗證）。`poll` 見 `CloneHooks`。
    """
    ensure_repository_root(path, timeout=settings.git_timeout_seconds)
    changes = fetch_origin(
        path,
        base_url=settings.github_clone_base_url,
        token=token,
        timeout=settings.github_clone_timeout_seconds,
        poll=poll,
        poll_interval=settings.git_poll_seconds,
    )
    result = FetchResult(
        repository_id=repository_id,
        updated_refs=changes[:MAX_REPORTED_REFS],
        truncated=len(changes) > MAX_REPORTED_REFS,
    )
    return result, len(changes)


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
    key = github_repo_key(repository.github_owner or "", repository.github_name or "")
    token = get_decrypted_token(session, settings)
    session.commit()  # fetch 期間不持有資料庫交易

    started = time.monotonic()
    lock = acquire_repo_lock_waiting(session, key, settings)
    try:
        result, changed = run_fetch(path, token, settings, repository_id=repository_id)
    finally:
        lock.release()
    logger.info(
        "github_repository_fetched",
        extra={
            "repository_id": repository_id,
            "changed_refs": changed,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return result
