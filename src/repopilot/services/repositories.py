import logging
import os
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Repository
from repopilot.errors import InvalidRepositoryPathError, RepositoryNotFoundError
from repopilot.git.repository import ensure_repository_root
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.path_mapping import to_container_path

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.repositories")

PATH_UNIQUE_CONSTRAINT = "uq_repositories_canonical_local_path"
# 受管目錄下存放 GitHub clone 的子目錄：<MANAGED_REPOS_DIR>/github/<owner>/<name>
GITHUB_DIR_NAME = "github"


def resolve_repository_path(
    raw_path: str, roots: Sequence[Path], *, reserved_roots: Sequence[Path] = ()
) -> Path:
    """將使用者提供的路徑正規化，並確認位於 `roots` 之內。

    `resolve()` 會展開 symlink、`..`、8.3 短檔名並還原實際大小寫，因此指向允許範圍外的
    symlink 會在這裡被拒絕。比較使用 `Path.is_relative_to`，不使用字串前綴。
    `reserved_roots`（受管目錄 `MANAGED_REPOS_DIR`）內的路徑一律拒絕：受管目錄只給 GitHub
    clone 使用，本機註冊不得繞過 `source_type` 的來源限制（切片 5c-2，D32）。
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
    for reserved in reserved_roots:
        # 受管目錄可能尚未建立，使用非 strict 的 resolve（盡可能展開存在的部分）
        if resolved.is_relative_to(reserved.resolve()):
            raise InvalidRepositoryPathError(
                "The path is inside MANAGED_REPOS_DIR, which is reserved for GitHub repositories."
            )
    return resolved


def resolve_registered_path(
    repository: Repository, settings: Settings, *, raw_path: str | None = None
) -> Path:
    """匯入／fetch 前重新驗證已註冊 repo 的路徑（D28 的擴充）；回傳已 resolve 的路徑。

    允許的根目錄由 `source_type` 決定，兩種來源互不通用：
    - `local`：只允許 `REPOSITORY_ROOTS`，且不得位於 `MANAGED_REPOS_DIR` 內。
    - `github`：只允許 `MANAGED_REPOS_DIR`，且必須正好是 `<受管目錄>/github/<owner>/<name>`
      （由資料列的 owner／name 推得），因此資料列的路徑被改成受管目錄內的別處也會被拒絕。

    `raw_path` 預設為資料庫保存的主機路徑；worker 傳入**容器端路徑**（`WORKER_PATH_MAPPINGS`
    映射後），此時 `settings` 的根目錄也是容器端路徑（`REPOSITORY_ROOTS=/mnt/repos`），
    同一套規則在容器內再驗一次。
    """
    raw = repository.canonical_local_path if raw_path is None else raw_path
    managed_dir = settings.managed_repos_dir
    if repository.source_type != "github":
        return resolve_repository_path(
            raw,
            settings.repository_roots,
            reserved_roots=[managed_dir] if managed_dir is not None else [],
        )
    if managed_dir is None:
        raise InvalidRepositoryPathError("MANAGED_REPOS_DIR is not configured.")
    path = resolve_repository_path(raw, [managed_dir])
    expected = managed_dir.resolve() / GITHUB_DIR_NAME / (repository.github_owner or "")
    expected = expected / (repository.github_name or "")
    # Windows 大小寫不敏感：以 normcase 比較（resolve 已還原實際大小寫，兩邊皆為真實路徑）
    if os.path.normcase(str(path)) != os.path.normcase(str(expected)):
        raise InvalidRepositoryPathError(
            "The path is not the managed location of this GitHub repository."
        )
    return path


def resolve_worker_path(repository: Repository, settings: Settings) -> Path:
    """worker 在容器內取得 repo 的可讀路徑（規格 9.6 的第 2、3 層）。

    ①把資料庫的主機路徑映射成容器路徑（純字串；對不上拋 `PathNotMappedError`）；
    ②在容器內以 `resolve_registered_path` 重驗（`resolve(strict=True)`、仍在容器端允許的根
    之下、GitHub 來源正好是固定位置）；③**不跟隨 symlink／junction**：`resolve()` 的結果
    必須與映射後的路徑完全相同，否則路徑中某段是連結，一律拒絕。切片 1 實測：Windows
    junction 在容器內是目標不存在的懸空 symlink，`resolve(strict=True)` 直接失敗，同樣落入
    拒絕。大小寫以資料庫的標準大小寫為準（容器端 `resolve()` 不還原大小寫，所以不能靠它
    正規化）。`ensure_repository_root` 由呼叫端（匯入服務）接著執行。
    """
    container_path = to_container_path(
        repository.canonical_local_path, settings.worker_path_mappings
    )
    raw = str(container_path)
    resolved = resolve_registered_path(repository, settings, raw_path=raw)
    if resolved != Path(raw):
        raise InvalidRepositoryPathError(
            "The repository path contains a symbolic link or junction, which is not followed."
        )
    return resolved


def repository_key(repository: Repository) -> str:
    """repo 級鎖與 job 的 `repo_key`。

    GitHub 來源用小寫 `owner/name`（註冊前資料列不存在，所以不能用 id），local 來源用 repo id。
    """
    if repository.source_type == "github":
        owner = (repository.github_owner or "").lower()
        name = (repository.github_name or "").lower()
        return f"github:{owner}/{name}"
    return f"local:{repository.id}"


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
    managed_dir: Path | None = None,
) -> tuple[Repository, bool]:
    """註冊本機 repo；回傳 (Repository, 是否新建)。相同 canonical path 為冪等。

    `managed_dir`（MANAGED_REPOS_DIR）內的路徑會被拒絕：受管目錄只給 GitHub clone 使用。
    """
    path = resolve_repository_path(
        raw_path, roots, reserved_roots=[managed_dir] if managed_dir is not None else []
    )
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
