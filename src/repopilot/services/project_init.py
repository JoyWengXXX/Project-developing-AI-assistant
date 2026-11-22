"""初始專案骨架的路徑驗證與框架偵測（第五階段 5-0a）。

只讀：不寫檔、不安裝依賴、不執行專案內的任何東西。專案內容視為不可信資料：

- 路徑必須是絕對路徑、位於 `REPOSITORY_ROOTS`（且不在受管目錄）內，且路徑上沒有任何
  symlink／junction（`resolve()` 的結果必須與輸入相同）；目錄掃描也不跟隨 symlink。
- 只讀根目錄下固定檔名的依賴檔（有大小上限，檔案內容不用 XML 解析器）；不讀 `.env` 等其他檔案。
- 掃描有深度與檔案數上限，並略過 `node_modules`、`.git` 等目錄。
"""

import os
import re
import tomllib
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from repopilot.errors import ProjectDependenciesNotFoundError, ProjectPathInvalidError
from repopilot.services import project_manifests

MAX_SCAN_FILES = 20_000
MAX_SCAN_DEPTH = 6

SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "venv",
        ".venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".idea",
        ".vs",
    }
)
COMMON_DIRS = ("src", "tests", "test", "app", "lib", "cmd", "internal")

LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
}

# 根目錄的依賴檔（區分大小寫）→ 生態系；node 再依 TypeScript 檔案數與依賴分成 ts／js
MANIFEST_ECOSYSTEMS = {
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "package.json": "node",
    "pom.xml": "java",
    "go.mod": "go",
    "Cargo.toml": "rust",
}
_ECOSYSTEM_ORDER = ("python", "node", "java", "go", "rust")
_ECOSYSTEM_LANGUAGES = {
    "python": ("python",),
    "node": ("typescript", "javascript"),
    "java": ("java",),
    "go": ("go",),
    "rust": ("rust",),
}
_SUGGESTIONS = {
    "python": "Add requirements.txt or pyproject.toml that lists the project's dependencies.",
    "typescript": "Add package.json that lists the project's dependencies.",
    "javascript": "Add package.json that lists the project's dependencies.",
    "java": "Add pom.xml that lists the project's dependencies.",
    "go": "Add go.mod that lists the project's modules.",
    "rust": "Add Cargo.toml that lists the project's crates.",
}

Category = Literal["framework", "orm", "test_framework", "database"]

# 生態系 → 類別 → (依賴名稱, 值)；依賴名稱是完整比對（小寫），先列的優先
_MARKERS: dict[str, dict[Category, tuple[tuple[str, str], ...]]] = {
    "python": {
        "framework": (("fastapi", "fastapi"), ("django", "django"), ("flask", "flask")),
        "orm": (
            ("sqlalchemy", "sqlalchemy"),
            ("sqlmodel", "sqlmodel"),
            ("tortoise-orm", "tortoise"),
        ),
        "test_framework": (("pytest", "pytest"),),
        "database": (
            ("psycopg", "postgresql"),
            ("psycopg2", "postgresql"),
            ("psycopg2-binary", "postgresql"),
            ("asyncpg", "postgresql"),
            ("pymysql", "mysql"),
            ("mysqlclient", "mysql"),
            ("pymongo", "mongodb"),
        ),
    },
    "node": {
        "framework": (
            ("next", "next"),
            ("@nestjs/core", "nestjs"),
            ("express", "express"),
            ("vue", "vue"),
            ("react", "react"),
        ),
        "orm": (("prisma", "prisma"), ("typeorm", "typeorm"), ("sequelize", "sequelize")),
        "test_framework": (("vitest", "vitest"), ("jest", "jest"), ("mocha", "mocha")),
        "database": (
            ("pg", "postgresql"),
            ("mysql2", "mysql"),
            ("sqlite3", "sqlite"),
            ("mongodb", "mongodb"),
            ("mongoose", "mongodb"),
        ),
    },
    "java": {
        "framework": (
            ("spring-boot-starter-parent", "spring-boot"),
            ("spring-boot-starter-web", "spring-boot"),
            ("spring-boot-starter", "spring-boot"),
            ("quarkus-resteasy", "quarkus"),
        ),
        "orm": (("spring-boot-starter-data-jpa", "jpa"), ("hibernate-core", "hibernate")),
        "test_framework": (("junit-jupiter", "junit"), ("junit", "junit")),
        "database": (
            ("postgresql", "postgresql"),
            ("mysql-connector-j", "mysql"),
            ("mysql-connector-java", "mysql"),
        ),
    },
    "go": {
        "framework": (
            ("github.com/gin-gonic/gin", "gin"),
            ("github.com/labstack/echo", "echo"),
            ("github.com/gofiber/fiber", "fiber"),
        ),
        "orm": (("gorm.io/gorm", "gorm"),),
        "test_framework": (("github.com/stretchr/testify", "testify"),),
        "database": (
            ("github.com/lib/pq", "postgresql"),
            ("github.com/jackc/pgx", "postgresql"),
            ("github.com/go-sql-driver/mysql", "mysql"),
        ),
    },
    "rust": {
        "framework": (("actix-web", "actix-web"), ("axum", "axum"), ("rocket", "rocket")),
        "orm": (("diesel", "diesel"), ("sqlx", "sqlx"), ("sea-orm", "sea-orm")),
        "test_framework": (),
        "database": (),
    },
}

_PYTHON_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$")
_POM_ARTIFACT_ID = re.compile(r"<artifactId>\s*([^<\s]{1,200})\s*</artifactId>")
_GO_REQUIRE = re.compile(r"^\s*(?:require\s+)?([A-Za-z0-9][\w.\-/]*)\s+(v[\w.+\-]+)", re.M)
_MAX_DEPENDENCIES = project_manifests.MAX_DEPENDENCIES

Confidence = Literal["high", "medium"]


@dataclass(frozen=True)
class ProjectDetection:
    language: str
    framework: str | None
    orm: str | None
    test_framework: str | None
    database: str | None
    dependencies: dict[str, str]
    manifests: list[str]
    language_files: dict[str, int]
    directories: list[str]
    confidence: Confidence
    root_path: str
    detected_at: datetime


@dataclass
class _Scan:
    language_files: Counter[str] = field(default_factory=Counter)
    directories: list[str] = field(default_factory=list)
    manifests: dict[str, Path] = field(default_factory=dict)


def validate_project_path(
    raw_path: str, roots: Sequence[Path], *, reserved_roots: Sequence[Path] = ()
) -> Path:
    """回傳已驗證的專案根目錄；任何不合格都拋 `ProjectPathInvalidError`（訊息不回顯輸入）。"""
    if not raw_path.strip() or "\x00" in raw_path:
        raise ProjectPathInvalidError("The project path is empty or contains invalid characters.")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ProjectPathInvalidError("The project path must be absolute.")
    try:
        if candidate.is_symlink() or candidate.is_junction():
            raise ProjectPathInvalidError("The project path must not be a symbolic link.")
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ProjectPathInvalidError("The project path does not exist.") from None
    # resolve() 會展開路徑中任何一段的 symlink／junction；與輸入不同代表路徑含連結
    if os.path.normcase(str(resolved)) != os.path.normcase(os.path.normpath(raw_path)):
        raise ProjectPathInvalidError("The project path contains a symbolic link.")
    if not resolved.is_dir():
        raise ProjectPathInvalidError("The project path is not a directory.")
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise ProjectPathInvalidError("The project path is not readable.")

    resolved_roots = [root.resolve() for root in roots]
    if not resolved_roots:
        raise ProjectPathInvalidError("No REPOSITORY_ROOTS are configured.")
    if not any(resolved.is_relative_to(root) for root in resolved_roots):
        raise ProjectPathInvalidError("The project path is outside the allowed REPOSITORY_ROOTS.")
    if any(resolved.is_relative_to(reserved.resolve()) for reserved in reserved_roots):
        raise ProjectPathInvalidError("The project path is inside the reserved managed directory.")
    return resolved


def detect_project(root: Path, *, now: datetime | None = None) -> ProjectDetection:
    """掃描已驗證的專案根目錄；沒有任何依賴檔時拋 `ProjectDependenciesNotFoundError`。"""
    scan = _scan(root)
    ecosystems = [eco for eco in _ECOSYSTEM_ORDER if _has_manifest(scan, eco)]
    if not ecosystems:
        language = _dominant_language(scan.language_files, None)
        suggestions = [_SUGGESTIONS[language]] if language else sorted(set(_SUGGESTIONS.values()))
        raise ProjectDependenciesNotFoundError(
            "No dependency file was found in the project root.",
            detected_language=language,
            suggestions=suggestions,
        )

    # 多個生態系並存時，選原始碼檔案最多的；平手依固定順序
    ecosystem = max(
        ecosystems,
        key=lambda eco: (
            sum(scan.language_files[lang] for lang in _ECOSYSTEM_LANGUAGES[eco]),
            -_ECOSYSTEM_ORDER.index(eco),
        ),
    )
    dependencies = _read_dependencies(scan, ecosystem)
    language = _language_of(ecosystem, scan.language_files, dependencies)
    framework = _match(ecosystem, "framework", dependencies)
    manifest_names = sorted(
        name for name in scan.manifests if MANIFEST_ECOSYSTEMS[name] == ecosystem
    )
    has_sources = scan.language_files[language] > 0
    return ProjectDetection(
        language=language,
        framework=framework,
        orm=_match(ecosystem, "orm", dependencies),
        test_framework=_match(ecosystem, "test_framework", dependencies),
        database=_match(ecosystem, "database", dependencies),
        dependencies=dependencies,
        manifests=manifest_names,
        language_files=dict(scan.language_files.most_common()),
        directories=sorted(scan.directories),
        confidence="high" if framework is not None and has_sources else "medium",
        root_path=str(root),
        detected_at=now or datetime.now(UTC),
    )


def _has_manifest(scan: _Scan, ecosystem: str) -> bool:
    return any(MANIFEST_ECOSYSTEMS[name] == ecosystem for name in scan.manifests)


def _scan(root: Path) -> _Scan:
    scan = _Scan()
    try:
        top_entries = list(os.scandir(root))
    except OSError:
        raise ProjectPathInvalidError("The project path is not readable.") from None
    for entry in top_entries:
        if entry.name in MANIFEST_ECOSYSTEMS and _is_plain_file(entry):
            scan.manifests[entry.name] = Path(entry.path)
        elif entry.name in COMMON_DIRS and _is_plain_dir(entry):
            scan.directories.append(entry.name)

    visited = 0
    pending: list[tuple[str, int]] = [(str(root), 0)]
    while pending and visited < MAX_SCAN_FILES:
        directory, depth = pending.pop()
        for entry in _entries(directory):
            if _is_plain_dir(entry):
                if entry.name not in SKIPPED_DIRS and depth < MAX_SCAN_DEPTH:
                    pending.append((entry.path, depth + 1))
            elif _is_plain_file(entry):
                visited += 1
                language = LANGUAGE_EXTENSIONS.get(os.path.splitext(entry.name)[1].lower())
                if language is not None:
                    scan.language_files[language] += 1
                if visited >= MAX_SCAN_FILES:
                    break
    return scan


def _entries(directory: str) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as iterator:
            yield from list(iterator)
    except OSError:
        return


def _is_plain_file(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _is_plain_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False) and not entry.is_junction()
    except OSError:
        return False


def _dominant_language(counts: Counter[str], ecosystem: str | None) -> str | None:
    candidates = _ECOSYSTEM_LANGUAGES[ecosystem] if ecosystem else tuple(counts)
    ranked = [(counts[lang], lang) for lang in candidates if counts[lang] > 0]
    if not ranked:
        return None
    return max(ranked, key=lambda item: (item[0], item[1] == "typescript"))[1]


def _language_of(ecosystem: str, counts: Counter[str], dependencies: dict[str, str]) -> str:
    if ecosystem != "node":
        return _ECOSYSTEM_LANGUAGES[ecosystem][0]
    if counts["typescript"] > counts["javascript"] or "typescript" in dependencies:
        return "typescript"
    return "javascript"


def _match(ecosystem: str, category: Category, dependencies: dict[str, str]) -> str | None:
    for name, value in _MARKERS[ecosystem][category]:
        if name in dependencies:
            return value
    return None


def _read_text(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(project_manifests.MAX_MANIFEST_BYTES + 1)
    except OSError:
        return None
    if len(data) > project_manifests.MAX_MANIFEST_BYTES:
        return None
    return data.decode("utf-8", errors="replace").removeprefix("\ufeff")


def _read_dependencies(scan: _Scan, ecosystem: str) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for name, path in sorted(scan.manifests.items()):
        if MANIFEST_ECOSYSTEMS[name] != ecosystem:
            continue
        text = _read_text(path)
        if text is None:
            continue
        for dep_name, spec in _parse_manifest(name, text):
            if len(dependencies) >= _MAX_DEPENDENCIES:
                return dependencies
            dependencies.setdefault(dep_name, spec)
    return dependencies


def _parse_manifest(name: str, text: str) -> Iterator[tuple[str, str]]:
    if name in ("requirements.txt", "pyproject.toml"):
        kind: project_manifests.ManifestKind = (
            "requirements" if name == "requirements.txt" else "pyproject"
        )
        info = project_manifests.parse_manifest(project_manifests.ManifestRef(kind, name, 0), text)
        for raw in info.dependencies:
            match = _PYTHON_NAME.match(raw)
            if match:
                normalized = re.sub(r"[._-]+", "-", match.group(1)).lower()
                yield normalized, match.group(2).strip()
    elif name == "package.json":
        info = project_manifests.parse_manifest(
            project_manifests.ManifestRef("package_json", name, 0), text
        )
        for raw in info.dependencies:
            if raw.startswith("@") and raw.count("@") == 1:
                yield raw.lower(), ""
                continue
            dep_name, _, spec = raw.rpartition("@")
            yield (dep_name or raw).lower(), spec if dep_name else ""
    elif name == "pom.xml":
        for artifact in _POM_ARTIFACT_ID.findall(text):
            yield artifact.lower(), ""
    elif name == "go.mod":
        for module, version in _GO_REQUIRE.findall(text):
            yield re.sub(r"/v\d+$", "", module.lower()), version
    elif name == "Cargo.toml":
        yield from _cargo_dependencies(text)


def _cargo_dependencies(text: str) -> Iterator[tuple[str, str]]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        return
    for section in ("dependencies", "dev-dependencies"):
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for crate, spec in table.items():
            if isinstance(spec, dict):
                spec = spec.get("version")
            yield str(crate).lower(), spec if isinstance(spec, str) else ""
