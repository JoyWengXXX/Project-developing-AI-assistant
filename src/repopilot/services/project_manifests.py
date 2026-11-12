"""專案描述檔（manifest）的純函式解析（切片 5d-1，D35）。

支援 pyproject.toml、requirements.txt、package.json、*.csproj。
全部是「文字進、結果出」的純函式：不讀資料庫、不讀檔案系統、不執行任何東西。輸入是來自 repo 的
**不可信**內容，因此：

- 解析失敗不拋出例外，而是回傳帶有穩定 `parse_error` 代碼的項目（不含原文或例外訊息）。
- `*.csproj` 刻意**不使用任何 XML 解析器**：只用有界的正規表達式與字串搜尋擷取少數欄位，
  所以 DOCTYPE／實體宣告（billion laughs、外部實體）只是被忽略的普通文字，絕不會被展開或
  載入。所有搜尋都有長度上限或單趟掃描，惡意內容無法造成二次方時間。
- 每個字串欄位截斷為 `MAX_ITEM_CHARS`；`dependencies` 最多 `MAX_DEPENDENCIES` 筆並標示 `truncated`。
"""

import json
import re
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

ManifestKind = Literal["pyproject", "requirements", "package_json", "csproj"]

MAX_MANIFEST_BYTES = 256 * 1024
MAX_DEPENDENCIES = 50
MAX_CSPROJ_FILES = 20
MAX_ITEM_CHARS = 200
MAX_TARGET_FRAMEWORKS = 20

SKIPPED_TOO_LARGE = "too_large"
ERROR_INVALID_TOML = "invalid_toml"
ERROR_INVALID_JSON = "invalid_json"
ERROR_NOT_AN_OBJECT = "not_an_object"

# 根目錄的固定檔名（區分大小寫）→ 種類
_ROOT_MANIFESTS: dict[str, ManifestKind] = {
    "pyproject.toml": "pyproject",
    "requirements.txt": "requirements",
    "package.json": "package_json",
}
_KIND_ORDER: dict[ManifestKind, int] = {
    "pyproject": 0,
    "requirements": 1,
    "package_json": 2,
    "csproj": 3,
}


@dataclass(frozen=True)
class ManifestRef:
    """快照中要解析的檔案；`byte_size` 用來在讀取內容前判斷是否過大。"""

    kind: ManifestKind
    path: str
    byte_size: int


@dataclass(frozen=True)
class ProjectInfo:
    kind: ManifestKind
    path: str
    name: str | None = None
    version: str | None = None
    dependencies: list[str] = field(default_factory=list)
    truncated: bool = False
    # 只有 csproj 會有值
    target_frameworks: list[str] = field(default_factory=list)
    # 檔案過大而未讀取（`too_large`）；解析失敗的穩定代碼（`invalid_toml` 等）。兩者互斥
    skipped_reason: str | None = None
    parse_error: str | None = None


def find_manifests(files: Iterable[tuple[str, int | None]]) -> list[ManifestRef]:
    """從 (path, byte_size) 清單挑出要解析的檔案；輸出順序固定（種類順序，csproj 依 path 排序）。

    根目錄的 `pyproject.toml`、`requirements.txt`、`package.json`（區分大小寫）；任意路徑下的
    `*.csproj`（副檔名不分大小寫）依 path 排序後最多取 `MAX_CSPROJ_FILES` 個。
    """
    roots: list[ManifestRef] = []
    csprojs: list[ManifestRef] = []
    for path, byte_size in files:
        size = byte_size or 0
        root_kind = _ROOT_MANIFESTS.get(path)
        if root_kind is not None:
            roots.append(ManifestRef(root_kind, path, size))
        elif path.lower().endswith(".csproj"):
            csprojs.append(ManifestRef("csproj", path, size))
    csprojs.sort(key=lambda ref: ref.path)
    roots.sort(key=lambda ref: _KIND_ORDER[ref.kind])
    return roots + csprojs[:MAX_CSPROJ_FILES]


def skipped_info(ref: ManifestRef, reason: str) -> ProjectInfo:
    return ProjectInfo(kind=ref.kind, path=ref.path, skipped_reason=reason)


def parse_manifest(ref: ManifestRef, content: str) -> ProjectInfo:
    """依種類分派；呼叫端須先以 `byte_size` 排除過大的檔案。"""
    text = content.removeprefix("\ufeff")
    if ref.kind == "pyproject":
        return parse_pyproject(ref.path, text)
    if ref.kind == "requirements":
        return parse_requirements(ref.path, text)
    if ref.kind == "package_json":
        return parse_package_json(ref.path, text)
    return parse_csproj(ref.path, text)


# --- 共用 ---


def _clip(value: str) -> str:
    return value[:MAX_ITEM_CHARS]


def _text_or_none(value: object) -> str | None:
    return _clip(value) if isinstance(value, str) else None


def _cap(items: Iterable[str]) -> tuple[list[str], bool]:
    """最多取 `MAX_DEPENDENCIES` 筆（逐筆截斷長度）；多於上限時回傳 truncated=True。

    只多消耗一筆來判斷是否超過，不會走完整個（可能很大的）輸入。
    """
    result: list[str] = []
    for item in items:
        if len(result) == MAX_DEPENDENCIES:
            return result, True
        result.append(_clip(item))
    return result, False


# --- pyproject.toml ---


def parse_pyproject(path: str, text: str) -> ProjectInfo:
    """`project.name／version／dependencies`；沒有 `project` 表時退而求其次讀 `tool.poetry`。"""
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        return ProjectInfo("pyproject", path, parse_error=ERROR_INVALID_TOML)

    project = data.get("project")
    if isinstance(project, dict):
        raw = project.get("dependencies")
        dependencies = [d for d in raw if isinstance(d, str)] if isinstance(raw, list) else []
        name, version = project.get("name"), project.get("version")
    else:
        tool = data.get("tool")
        poetry = tool.get("poetry") if isinstance(tool, dict) else None
        if not isinstance(poetry, dict):
            # 合法的 TOML 但沒有可辨識的專案資訊：仍列出此檔案，欄位為空
            return ProjectInfo("pyproject", path)
        dependencies = list(_poetry_dependencies(poetry.get("dependencies")))
        name, version = poetry.get("name"), poetry.get("version")

    capped, truncated = _cap(dependencies)
    return ProjectInfo(
        "pyproject",
        path,
        name=_text_or_none(name),
        version=_text_or_none(version),
        dependencies=capped,
        truncated=truncated,
    )


def _poetry_dependencies(raw: object) -> Iterator[str]:
    """Poetry 的依賴表：`name = "^1.0"` 或 `name = {version = "^1.0", ...}`；略過 `python`。"""
    if not isinstance(raw, dict):
        return
    for name, spec in raw.items():
        if name == "python" or not isinstance(name, str):
            continue
        if isinstance(spec, dict):
            spec = spec.get("version")
        yield f"{name} {spec}" if isinstance(spec, str) else name


# --- requirements.txt ---

_INLINE_COMMENT = re.compile(r"\s+#.*$")


def parse_requirements(path: str, text: str) -> ProjectInfo:
    """逐行：略過空行、`#` 註解與以 `-` 開頭的選項行（`-r`、`-e`、`--index-url` 等）。

    其餘整行原樣保留（含 extras 與環境標記，例如 `pkg[x]>=1; python_version < "3.9"`）；
    不處理反斜線續行，也不解析版本。
    """

    def lines() -> Iterator[str]:
        for raw in text.splitlines():
            line = _INLINE_COMMENT.sub("", raw.strip()).strip()
            if line and not line.startswith(("#", "-")):
                yield line

    dependencies, truncated = _cap(lines())
    return ProjectInfo("requirements", path, dependencies=dependencies, truncated=truncated)


# --- package.json ---


def parse_package_json(path: str, text: str) -> ProjectInfo:
    """`name／version／dependencies／devDependencies`；依賴以 `name@range` 列出（先一般後 dev）。"""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # JSONDecodeError 是 ValueError 的子類別
        return ProjectInfo("package_json", path, parse_error=ERROR_INVALID_JSON)
    if not isinstance(data, dict):
        return ProjectInfo("package_json", path, parse_error=ERROR_NOT_AN_OBJECT)

    def entries() -> Iterator[str]:
        for key in ("dependencies", "devDependencies"):
            section = data.get(key)
            if not isinstance(section, dict):
                continue
            for dep_name, spec in section.items():
                yield f"{dep_name}@{spec}" if isinstance(spec, str) else str(dep_name)

    dependencies, truncated = _cap(entries())
    return ProjectInfo(
        "package_json",
        path,
        name=_text_or_none(data.get("name")),
        version=_text_or_none(data.get("version")),
        dependencies=dependencies,
        truncated=truncated,
    )


# --- *.csproj（不使用 XML 解析器）---

# 搜尋視窗與標籤長度都有上限，避免惡意內容（大量未閉合的標籤）造成二次方時間
_TAG_ATTRIBUTE_LIMIT = 1000
_CHILD_WINDOW = 1000
# MSBuild 的元素與屬性名稱不分大小寫
_PACKAGE_REFERENCE = re.compile(rf"<PackageReference\b([^>]{{0,{_TAG_ATTRIBUTE_LIMIT}}})>", re.I)
_ATTRIBUTE_INCLUDE = re.compile(r"""\bInclude\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ATTRIBUTE_VERSION = re.compile(r"""\bVersion\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_CHILD_VERSION = re.compile(r"<Version\s*>([^<]{0,200})</Version\s*>", re.I)
_TARGET_FRAMEWORKS = re.compile(
    r"<TargetFrameworks?(?:\s[^>]{0,200})?>([^<]{0,500})</TargetFrameworks?\s*>", re.I
)


def parse_csproj(path: str, text: str) -> ProjectInfo:
    """擷取 `<PackageReference Include=… Version=…>` 與 `<TargetFramework(s)>`；不會有 parse_error。

    `Version` 可以是屬性、子元素（`<Version>…</Version>`）或缺漏（缺漏時只列名稱）。
    依賴格式為 `名稱@版本`；`name` 取自檔名（不含 `.csproj`），不讀專案內的屬性。
    內容中的 `&entity;` 不會被展開，原樣當成文字。
    """
    cleaned = _strip_xml_comments(text)
    dependencies, truncated = _cap(_package_references(cleaned))
    frameworks = _target_frameworks(cleaned)
    stem = path.rsplit("/", 1)[-1]
    return ProjectInfo(
        "csproj",
        path,
        name=_clip(stem[: -len(".csproj")] if stem.lower().endswith(".csproj") else stem),
        dependencies=dependencies,
        truncated=truncated,
        target_frameworks=frameworks,
    )


def _strip_xml_comments(text: str) -> str:
    """移除 `<!-- ... -->`（被註解掉的套件不算數）；單趟 `str.find`，未閉合的註解吃到結尾。"""
    parts: list[str] = []
    position = 0
    while True:
        start = text.find("<!--", position)
        if start == -1:
            parts.append(text[position:])
            break
        parts.append(text[position:start])
        end = text.find("-->", start + 4)
        if end == -1:
            break
        position = end + 3
    return "".join(parts)


def _attribute(pattern: re.Pattern[str], attributes: str) -> str | None:
    found = pattern.search(attributes)
    if found is None:
        return None
    return found.group(1) if found.group(1) is not None else found.group(2)


def _package_references(text: str) -> Iterator[str]:
    for match in _PACKAGE_REFERENCE.finditer(text):
        attributes = match.group(1)
        name = _attribute(_ATTRIBUTE_INCLUDE, attributes)
        if not name or not name.strip():
            continue
        version = _attribute(_ATTRIBUTE_VERSION, attributes)
        if version is None and not attributes.rstrip().endswith("/"):
            version = _child_version(text, match.end())
        version = version.strip() if version else None
        yield f"{name.strip()}@{version}" if version else name.strip()


def _child_version(text: str, start: int) -> str | None:
    """非自閉合的 `<PackageReference ...>` 之後、結束標籤之前的 `<Version>` 子元素。"""
    window = text[start : start + _CHILD_WINDOW]
    closing = window.lower().find("</packagereference")
    body = window if closing == -1 else window[:closing]
    found = _CHILD_VERSION.search(body)
    return found.group(1) if found is not None else None


def _target_frameworks(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in _TARGET_FRAMEWORKS.finditer(text):
        for item in match.group(1).split(";"):
            value = item.strip()
            if value:
                seen.setdefault(_clip(value), None)
            if len(seen) >= MAX_TARGET_FRAMEWORKS:
                return list(seen)
    return list(seen)
