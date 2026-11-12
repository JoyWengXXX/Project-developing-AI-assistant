"""repo 概況（切片 5d-1，D35）：預設分支最新 commit 的統計、語言分布、README 與專案資訊。

**唯讀**：不匯入、不寫入任何資料。該 commit 在目前匯入設定下沒有 `completed` 快照時，只回傳
`head`（`analyzed=False`），由呼叫端決定是否另外呼叫 `POST /imports`。

查詢次數為小常數，與檔案數無關：
1. `get_default_head`：取得 repo＋路徑重驗＋Git 唯讀查詢（無資料庫連線被持有）；
2. 找 completed 快照（1 次）；
3. 該快照所有檔案的 `path／byte_size／status`（1 次，不含 `content`）→ Python 端算統計與語言；
4. README 前 4,000 字元（1 次，`substr` 在資料庫端截斷）；
5. 專案描述檔內容（1 次，`WHERE path IN (…)`，最多 23 個檔案）。
"""

import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.git.history import CommitRecord
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services import project_manifests
from repopilot.services.import_profile import ImportProfile
from repopilot.services.project_manifests import ProjectInfo
from repopilot.services.repository_history import get_default_head
from repopilot.services.snapshot_queries import SNAPSHOT_COMPLETED

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.repository_overview")

README_MAX_CHARS = 4000
MAX_LANGUAGES = 10
OTHER_LANGUAGE = "其他"

# 副檔名（小寫，含點）→ 語言。只是顯示用的啟發式：依副檔名，不看內容，也不代表理解該語言。
# `.h` 歸 C（也可能是 C++）、`.m` 等有歧義者不收。要新增語言時直接加一列。
_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".cs": "C#",
    ".csx": "C#",
    ".razor": "Razor",
    ".cshtml": "Razor",
    ".vb": "Visual Basic",
    ".fs": "F#",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".vue": "Vue",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".rst": "reStructuredText",
    ".json": "JSON",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".sql": "SQL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".swift": "Swift",
    ".csproj": "MSBuild",
    ".vbproj": "MSBuild",
    ".fsproj": "MSBuild",
    ".sln": "MSBuild",
    ".slnx": "MSBuild",
    ".props": "MSBuild",
    ".targets": "MSBuild",
}
# 以檔名（小寫）辨識者
_LANGUAGE_BY_NAME: dict[str, str] = {"dockerfile": "Dockerfile"}

# 根目錄的 README 檔名（小寫）→ 優先序（愈小愈優先）
_README_RANK: dict[str, int] = {
    "readme": 0,
    "readme.md": 1,
    "readme.rst": 2,
    "readme.txt": 3,
    "readme.markdown": 4,
}


@dataclass(frozen=True)
class LanguageStat:
    language: str
    files: int
    bytes: int


@dataclass(frozen=True)
class FileStats:
    imported_files: int
    skipped_files: int
    total_bytes: int


@dataclass(frozen=True)
class ReadmeExcerpt:
    path: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class OverviewResult:
    repository_id: int
    default_ref: str | None
    head: CommitRecord | None
    # 以下只有 `analyzed=True` 時才有內容
    snapshot: Snapshot | None = None
    stats: FileStats | None = None
    languages: list[LanguageStat] = field(default_factory=list)
    readme: ReadmeExcerpt | None = None
    project_info: list[ProjectInfo] = field(default_factory=list)

    @property
    def analyzed(self) -> bool:
        return self.snapshot is not None


def get_overview(session: Session, repository_id: int, settings: Settings) -> OverviewResult:
    started = time.monotonic()
    default = get_default_head(session, repository_id, settings)
    head = default.head
    if head is None:
        return OverviewResult(repository_id, default.default_ref, None)

    snapshot = _find_completed_snapshot(session, repository_id, head.sha, settings)
    if snapshot is None:
        return OverviewResult(repository_id, default.default_ref, head)

    files = session.execute(
        select(SnapshotFile.path, SnapshotFile.byte_size, SnapshotFile.status).where(
            SnapshotFile.snapshot_id == snapshot.id
        )
    ).all()
    imported = [(path, size) for path, size, status in files if status == "imported"]

    result = OverviewResult(
        repository_id,
        default.default_ref,
        head,
        snapshot=snapshot,
        stats=FileStats(
            imported_files=len(imported),
            skipped_files=len(files) - len(imported),
            total_bytes=sum(size or 0 for _, size in imported),
        ),
        languages=summarize_languages(imported),
        readme=_read_readme(session, snapshot.id, [path for path, _ in imported]),
        project_info=_read_project_info(session, snapshot.id, imported),
    )
    logger.info(
        "repository_overview_built",
        extra={
            "repository_id": repository_id,
            "snapshot_id": snapshot.id,
            "file_count": len(files),
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return result


def _find_completed_snapshot(
    session: Session, repository_id: int, commit_sha: str, settings: Settings
) -> Snapshot | None:
    """該 commit 在**目前匯入設定雜湊**下的 completed 快照（與 `imported` 旗標同一個定義，D34）。"""
    return session.scalars(
        select(Snapshot).where(
            Snapshot.repository_id == repository_id,
            Snapshot.commit_sha == commit_sha,
            Snapshot.status == SNAPSHOT_COMPLETED,
            Snapshot.import_profile_hash == ImportProfile.from_settings(settings).hash(),
        )
    ).first()


# --- 語言 ---


def language_for_path(path: str) -> str:
    """依檔名或副檔名（不分大小寫）對照語言；無副檔名或不認得時為「其他」。"""
    name = path.rsplit("/", 1)[-1].lower()
    by_name = _LANGUAGE_BY_NAME.get(name)
    if by_name is not None:
        return by_name
    dot = name.rfind(".")
    if dot <= 0:  # 無副檔名，或 `.gitignore` 這種以點開頭的檔名
        return OTHER_LANGUAGE
    return _LANGUAGE_BY_EXTENSION.get(name[dot:], OTHER_LANGUAGE)


def summarize_languages(files: Iterable[tuple[str, int | None]]) -> list[LanguageStat]:
    """依檔案數遞減（同數依名稱）排序，最多 `MAX_LANGUAGES` 項；「其他」永遠排在最後。

    超過上限時，排在後面的語言併入「其他」，所以結果（含「其他」）不超過上限。
    """
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for path, size in files:
        entry = totals[language_for_path(path)]
        entry[0] += 1
        entry[1] += size or 0

    other = totals.pop(OTHER_LANGUAGE, [0, 0])
    ranked = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
    if len(ranked) + (1 if other[0] else 0) > MAX_LANGUAGES:
        for _, (count, size) in ranked[MAX_LANGUAGES - 1 :]:
            other[0] += count
            other[1] += size
        ranked = ranked[: MAX_LANGUAGES - 1]

    result = [LanguageStat(name, count, size) for name, (count, size) in ranked]
    if other[0]:
        result.append(LanguageStat(OTHER_LANGUAGE, other[0], other[1]))
    return result


# --- README ---


def pick_readme(paths: Iterable[str]) -> str | None:
    """根目錄（path 不含 `/`）的 README，依固定優先序取第一個；同優先序依 path 排序。"""
    best: tuple[int, str] | None = None
    for path in paths:
        if "/" in path:
            continue
        rank = _README_RANK.get(path.lower())
        if rank is not None and (best is None or (rank, path) < best):
            best = (rank, path)
    return best[1] if best is not None else None


def truncate_chars(text: str, limit: int) -> tuple[str, bool]:
    """以字元（Unicode code point）截斷，所以不會切壞多位元組字元（中文、emoji）。"""
    return (text[:limit], True) if len(text) > limit else (text, False)


def _read_readme(
    session: Session, snapshot_id: int, imported_paths: list[str]
) -> ReadmeExcerpt | None:
    path = pick_readme(imported_paths)
    if path is None:
        return None
    # 在資料庫端只取前 N+1 個字元（多取一個判斷是否截斷），不把整份 README 載入
    head = session.scalar(
        select(func.substr(SnapshotFile.content, 1, README_MAX_CHARS + 1)).where(
            SnapshotFile.snapshot_id == snapshot_id,
            SnapshotFile.path == path,
            SnapshotFile.status == "imported",
        )
    )
    if head is None:
        return None
    text, truncated = truncate_chars(head, README_MAX_CHARS)
    return ReadmeExcerpt(path, text, truncated)


# --- 專案資訊 ---


def _read_project_info(
    session: Session, snapshot_id: int, imported: list[tuple[str, int | None]]
) -> list[ProjectInfo]:
    refs = project_manifests.find_manifests(imported)
    # 先用 byte_size 判斷：過大的檔案完全不讀取內容
    readable = [ref for ref in refs if ref.byte_size <= project_manifests.MAX_MANIFEST_BYTES]
    contents: dict[str, str] = {}
    if readable:
        rows = session.execute(
            select(SnapshotFile.path, SnapshotFile.content).where(
                SnapshotFile.snapshot_id == snapshot_id,
                SnapshotFile.path.in_([ref.path for ref in readable]),
                SnapshotFile.status == "imported",
            )
        )
        contents = {path: content for path, content in rows if content is not None}

    items: list[ProjectInfo] = []
    for ref in refs:
        if ref.byte_size > project_manifests.MAX_MANIFEST_BYTES:
            items.append(project_manifests.skipped_info(ref, project_manifests.SKIPPED_TOO_LARGE))
        elif ref.path in contents:
            items.append(project_manifests.parse_manifest(ref, contents[ref.path]))
    return items
