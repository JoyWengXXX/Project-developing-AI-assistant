"""參考快照的唯讀摘要（第五階段）：語言分布、依賴檔分類、頂層目錄。

給 5-0b（對話推薦）與 5-1（新專案規劃）共用；只讀已完成快照的資料表，不碰 Git 與檔案系統。
快照內容是不可信資料：這裡只取名稱與計數，依賴檔交給有大小上限、不執行任何東西的解析器。
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from repopilot.db.models import Repository, Snapshot, SnapshotFile
from repopilot.errors import RetrievalSnapshotNotCompletedError, RetrievalSnapshotNotFoundError
from repopilot.services import project_init, project_manifests
from repopilot.services.repository_overview import LanguageStat, summarize_languages

MAX_TOP_DIRECTORIES = 15


@dataclass(frozen=True)
class ReferenceSummary:
    snapshot_id: int
    repository_id: int
    repository_name: str
    commit_sha: str
    file_count: int
    languages: list[LanguageStat]
    language_files: Counter[str]
    classification: project_init.Classification | None
    top_directories: list[tuple[str, int]] = field(default_factory=list)


def load_reference_summary(session: Session, snapshot_id: int) -> ReferenceSummary:
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise RetrievalSnapshotNotFoundError(snapshot_id)
    if snapshot.status != "completed":
        raise RetrievalSnapshotNotCompletedError(snapshot_id)
    repository_name = session.scalar(
        select(Repository.name).where(Repository.id == snapshot.repository_id)
    )

    rows = session.execute(
        select(SnapshotFile.path, SnapshotFile.byte_size).where(
            SnapshotFile.snapshot_id == snapshot_id, SnapshotFile.status == "imported"
        )
    ).all()
    files = [(path, size) for path, size in rows]
    language_files = count_languages(path for path, _ in files)

    manifest_sizes = {
        path: size or 0 for path, size in files if path in project_init.MANIFEST_ECOSYSTEMS
    }
    readable = [
        p for p, size in manifest_sizes.items() if size <= project_manifests.MAX_MANIFEST_BYTES
    ]
    texts: dict[str, str] = {}
    if readable:
        content_rows = session.execute(
            select(SnapshotFile.path, SnapshotFile.content).where(
                SnapshotFile.snapshot_id == snapshot_id,
                SnapshotFile.path.in_(readable),
                SnapshotFile.status == "imported",
            )
        )
        texts = {path: content for path, content in content_rows if content is not None}
    # 過大或讀不到內容的依賴檔仍視為存在，只是沒有可解析的內容
    for path in manifest_sizes:
        texts.setdefault(path, "")

    top_directories = Counter(path.split("/", 1)[0] for path, _ in files if "/" in path)
    return ReferenceSummary(
        snapshot_id=snapshot_id,
        repository_id=snapshot.repository_id,
        repository_name=repository_name or "",
        commit_sha=snapshot.commit_sha,
        file_count=len(files),
        languages=summarize_languages(files),
        language_files=language_files,
        classification=project_init.classify(texts, language_files),
        top_directories=top_directories.most_common(MAX_TOP_DIRECTORIES),
    )


def count_languages(paths: Iterable[str]) -> Counter[str]:
    """依副檔名計數；略過依賴與建置目錄內的檔案。"""
    counts: Counter[str] = Counter()
    for path in paths:
        parts = path.split("/")
        if any(part in project_init.SKIPPED_DIRS for part in parts[:-1]):
            continue
        dot = parts[-1].rfind(".")
        language = (
            project_init.LANGUAGE_EXTENSIONS.get(parts[-1][dot:].lower()) if dot > 0 else None
        )
        if language is not None:
            counts[language] += 1
    return counts
