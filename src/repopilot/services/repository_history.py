"""repo 的版本瀏覽查詢（切片 5e-1，D34）：refs 與 commit 歷史。唯讀，不 fetch、不寫入。

流程：取得 repo → 重新驗證路徑（D28，依 `source_type`）→ 結束讀取交易 → 確認 repo 根目錄（D19）→
Git 唯讀查詢 →（commits）以單一資料庫查詢批次判斷 `imported`。Git 作業期間不持有資料庫連線（D17）。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Repository, Snapshot
from repopilot.git import history
from repopilot.git.repository import ensure_repository_root
from repopilot.services.import_profile import ImportProfile
from repopilot.services.repositories import get_repository, resolve_registered_path
from repopilot.services.snapshot_queries import SNAPSHOT_COMPLETED


@dataclass(frozen=True)
class BranchView:
    name: str
    ref: str
    sha: str
    is_default: bool


@dataclass(frozen=True)
class TagView:
    name: str
    ref: str
    sha: str


@dataclass(frozen=True)
class RefsResult:
    default_ref: str | None
    head_sha: str | None
    branches: list[BranchView]
    tags: list[TagView]
    truncated: bool


@dataclass(frozen=True)
class RefLabel:
    name: str
    kind: Literal["branch", "tag"]


@dataclass(frozen=True)
class CommitView:
    sha: str
    parents: tuple[str, ...]
    subject: str
    author_name: str
    authored_at: datetime
    refs: list[RefLabel]
    imported: bool


@dataclass(frozen=True)
class CommitsResult:
    items: list[CommitView]
    has_more: bool


@dataclass(frozen=True)
class _Target:
    repository: Repository
    path: Path
    # 本機來源看 refs/heads/*；GitHub 來源看 refs/remotes/origin/*（clone 的本機分支不列出）
    branch_namespace: str


def _open_repository(session: Session, repository_id: int, settings: Settings) -> _Target:
    repository = get_repository(session, repository_id)
    path = resolve_registered_path(repository, settings)
    # 結束讀取交易，避免之後的 Git 作業期間持有閒置連線
    session.commit()
    ensure_repository_root(path, timeout=settings.git_timeout_seconds)
    namespace = (
        history.ORIGIN_BRANCHES if repository.source_type == "github" else history.LOCAL_BRANCHES
    )
    return _Target(repository, path, namespace)


def _find_default(
    target: _Target, listing: history.RefListing, timeout: float
) -> tuple[history.RefEntry | None, str | None]:
    """預設分支與其最新 commit SHA（refs 與 overview 共用，D34／D35）。"""
    if target.repository.source_type == "github":
        # clone 時建立的本機 HEAD 在 fetch 後不會前進（D32），不能代表預設分支的現況；
        # 以註冊時記錄的預設分支名稱對應 origin/<分支>，找不到（例如已改名）則為 null
        wanted = history.ORIGIN_BRANCHES + (target.repository.github_default_branch or "")
        default = next((b for b in listing.branches if b.ref == wanted), None)
        return default, default.sha if default is not None else None
    # 目前簽出的分支；detached HEAD 時沒有，但 head_sha 仍有值（空 repo 為 null）
    default = next((b for b in listing.branches if b.is_head), None)
    return default, history.head_commit(target.path, timeout=timeout)


@dataclass(frozen=True)
class DefaultHeadResult:
    default_ref: str | None
    # 預設分支最新 commit（含 subject／作者；無 email）；空 repo 或找不到預設分支時為 None
    head: history.CommitRecord | None


def get_default_head(session: Session, repository_id: int, settings: Settings) -> DefaultHeadResult:
    """預設分支與其最新 commit（概況 API 的起點，D35）；判斷規則與 `get_refs` 完全相同。

    唯讀：路徑重驗（D28）→ `for-each-ref` → 單一 `git log -1`；不寫入資料庫。
    """
    target = _open_repository(session, repository_id, settings)
    timeout = settings.git_timeout_seconds
    listing = history.list_refs(
        target.path, branch_namespace=target.branch_namespace, timeout=timeout
    )
    default, head_sha = _find_default(target, listing, timeout)
    head = None
    if head_sha is not None:
        page = history.read_commits(
            target.path,
            start_sha=head_sha,
            branch_namespace=target.branch_namespace,
            limit=1,
            offset=0,
            timeout=timeout,
        )
        head = page.records[0] if page.records else None
    return DefaultHeadResult(default.ref if default is not None else None, head)


def get_refs(session: Session, repository_id: int, settings: Settings) -> RefsResult:
    target = _open_repository(session, repository_id, settings)
    timeout = settings.git_timeout_seconds
    listing = history.list_refs(
        target.path, branch_namespace=target.branch_namespace, timeout=timeout
    )
    default, head_sha = _find_default(target, listing, timeout)
    default_ref = default.ref if default is not None else None

    return RefsResult(
        default_ref=default_ref,
        head_sha=head_sha,
        branches=[
            BranchView(b.name, b.ref, b.sha, is_default=b.ref == default_ref)
            for b in listing.branches
        ],
        tags=[TagView(t.name, t.ref, t.sha) for t in listing.tags],
        truncated=listing.truncated,
    )


def get_commits(
    session: Session,
    repository_id: int,
    settings: Settings,
    *,
    ref: str | None,
    limit: int,
    offset: int,
) -> CommitsResult:
    """列出 commit（`--topo-order`）；`ref` 省略＝所有分支與標籤，否則必須是存在的完整 ref。"""
    target = _open_repository(session, repository_id, settings)
    timeout = settings.git_timeout_seconds

    start_sha: str | None = None
    if ref is not None:
        start_sha = history.find_ref(
            target.path, ref, branch_namespace=target.branch_namespace, timeout=timeout
        ).sha
    page = history.read_commits(
        target.path,
        start_sha=start_sha,
        branch_namespace=target.branch_namespace,
        limit=limit,
        offset=offset,
        timeout=timeout,
    )
    labels = _labels_by_sha(
        history.list_refs(target.path, branch_namespace=target.branch_namespace, timeout=timeout)
    )
    imported = _imported_shas(
        session, repository_id, [record.sha for record in page.records], settings
    )
    return CommitsResult(
        items=[
            CommitView(
                sha=record.sha,
                parents=record.parents,
                subject=record.subject,
                author_name=record.author_name,
                authored_at=record.authored_at,
                refs=labels.get(record.sha, []),
                imported=record.sha in imported,
            )
            for record in page.records
        ],
        has_more=page.has_more,
    )


def _labels_by_sha(listing: history.RefListing) -> dict[str, list[RefLabel]]:
    """sha → 指向它的分支與標籤（分支在前，各自依名稱排序）。

    使用 refs 清單（完整 ref 名稱，無歧義）而不是 `git log` 的 `%D`：`%D` 只有短名稱，本機分支
    `origin/main` 與遠端分支 `origin/main` 無法區分，而且會混入 stash 等非分支 refs。
    代價：只有 `/refs` 清單內的 ref（各 500 個）會有標記。
    """
    labels: dict[str, list[RefLabel]] = {}
    for branch in listing.branches:
        labels.setdefault(branch.sha, []).append(RefLabel(branch.name, "branch"))
    for tag in listing.tags:
        labels.setdefault(tag.sha, []).append(RefLabel(tag.name, "tag"))
    return labels


def _imported_shas(
    session: Session, repository_id: int, shas: list[str], settings: Settings
) -> set[str]:
    """這些 SHA 中，已有 `completed` 快照（以目前匯入設定）者；單一查詢，無 N+1。

    以目前的匯入設定雜湊為準，因此 `imported=true` 恰好等於「`POST /imports` 會回 200 而不是
    建立新快照」（D3、D26）。
    """
    if not shas:
        return set()
    profile_hash = ImportProfile.from_settings(settings).hash()
    return set(
        session.scalars(
            select(Snapshot.commit_sha).where(
                Snapshot.repository_id == repository_id,
                Snapshot.status == SNAPSHOT_COMPLETED,
                Snapshot.import_profile_hash == profile_hash,
                Snapshot.commit_sha.in_(shas),
            )
        )
    )
