"""主機路徑 → worker 容器路徑的映射（第二階段切片 3，規格 9.6）。

資料庫的 `repositories.canonical_local_path` 是 Windows 路徑（例如 `D:\\repos\\demo`）；worker 在
Linux 容器內，只看得到 bind mount 後的路徑（例如 `/mnt/repos/demo`）。這個模組做**純字串運算**
——不碰檔案系統、不呼叫 `resolve()`——把前者換成後者：

- 以 Windows 語意解析（磁碟機代號、不分大小寫），不支援 UNC。
- 逐段比對 `host` 前綴（`D:\\repos` 不會誤中 `D:\\repos-evil`），取**最長**的符合項。
- 相對部分的每一段都必須是「正常的 Windows 目錄名稱」：拒絕 `..`、`.`、空區段
  （雙反斜線、尾端分隔符）、含 `:`／`/`／萬用字元／控制字元者、尾端是空白或句點者、
  裝置保留名稱（`CON`、`NUL`…）。
- 以 `PurePosixPath` 接上 `container`；相對部分保留資料庫的標準大小寫（容器端 `resolve()`
  不還原實際大小寫，所以大小寫以主機端 D18 還原的字串為準，不在容器端「修正」）。

這只決定「路徑長什麼樣」；路徑是否真的存在、是否越出允許的根、是否為 symlink／junction，
由 `services/repositories.py::resolve_worker_path` 在容器內重新驗證（規格 9.6 的第 3 層）。

.NET 對照：像一個不碰 `File.Exists` 的 `Uri` 轉換函式，輸入不合格就丟例外，不會悄悄「修正」。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from repopilot.errors import PathNotMappedError

_DRIVE = re.compile(r"^[A-Za-z]:$")
_FORBIDDEN_CHARS = frozenset('<>:"/|?*')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def _segment_problem(segment: str) -> str | None:
    """回傳目錄名稱不合格的原因（固定文字，不含輸入）；合格回傳 None。"""
    if segment == "":
        return "empty segment"
    if segment in (".", ".."):
        return "dot segment"
    if any(ord(char) < 0x20 or char in _FORBIDDEN_CHARS for char in segment):
        return "forbidden character"
    if segment.endswith((" ", ".")):
        return "trailing space or dot"
    if segment.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES:
        return "reserved device name"
    return None


def parse_host_path(raw: str, *, lenient_separators: bool = False) -> tuple[str, tuple[str, ...]]:
    """把 Windows 絕對路徑拆成 (大寫磁碟機代號如 `D:`, 目錄區段)；不合格拋 `ValueError`。

    `lenient_separators=True` 只給**設定檔**的 `host` 使用（允許 `D:/repos` 與尾端分隔符，
    因為 Docker 與 `.env` 習慣用正斜線）；資料庫的路徑一律用嚴格模式（只接受反斜線、
    沒有尾端分隔符）。錯誤訊息是固定文字，不回顯輸入。
    """
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("The path is empty or contains a NUL character.")
    text = raw
    if lenient_separators:
        text = text.replace("/", "\\")
        if len(text) > 3:
            text = text.rstrip("\\")
    if text.startswith("\\\\"):
        raise ValueError("UNC and device paths are not supported.")
    if len(text) < 3 or not _DRIVE.match(text[:2]) or text[2] != "\\":
        raise ValueError("The path must be an absolute Windows path with a drive letter.")
    rest = text[3:]
    segments = tuple(rest.split("\\")) if rest else ()
    for segment in segments:
        problem = _segment_problem(segment)
        if problem is not None:
            raise ValueError(f"Invalid path segment: {problem}.")
    return text[:2].upper(), segments


class PathMapping(BaseModel):
    """`WORKER_PATH_MAPPINGS` 的一項：主機目錄 ↔ 容器內掛載點（對應 Compose 的 bind mount）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    container: str
    mode: Literal["ro", "rw"]

    @field_validator("host")
    @classmethod
    def _host_must_be_windows_absolute(cls, value: str) -> str:
        parse_host_path(value, lenient_separators=True)
        return value

    @field_validator("container")
    @classmethod
    def _container_must_be_absolute(cls, value: str) -> str:
        # 容器是 Linux，掛載點為 `/mnt/...`；`C:/...` 形式只讓 Windows 開發機能在本機目錄上
        # 測試容器端重驗邏輯（測試把「容器根」指到 pytest 的暫存目錄）。Linux 容器內它不存在，
        # 等於無效。
        if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            raise ValueError("container must be an absolute path.")
        if "\x00" in value or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("container must not contain NUL or '..' segments.")
        return value.rstrip("/\\") or "/"

    @property
    def host_parts(self) -> tuple[str, tuple[str, ...]]:
        return parse_host_path(self.host, lenient_separators=True)


@dataclass(frozen=True)
class MappedPath:
    mapping: PathMapping
    container_path: PurePosixPath


def map_host_path(host_path: str, mappings: Sequence[PathMapping]) -> MappedPath:
    """把資料庫中的主機路徑換成容器路徑；任何問題一律拋 `PathNotMappedError`。"""
    try:
        drive, segments = parse_host_path(host_path)
    except ValueError:
        raise PathNotMappedError(
            "The repository path is not a valid absolute Windows path."
        ) from None

    best: tuple[PathMapping, int] | None = None
    for mapping in mappings:
        map_drive, map_segments = mapping.host_parts
        if map_drive != drive or len(map_segments) > len(segments):
            continue
        # 逐段、不分大小寫比對（NTFS）；`D:\repos` 不會符合 `D:\repos-evil`
        prefix = segments[: len(map_segments)]
        same_prefix = all(
            a.casefold() == b.casefold() for a, b in zip(map_segments, prefix, strict=True)
        )
        if same_prefix and (best is None or len(map_segments) > best[1]):
            best = (mapping, len(map_segments))
    if best is None:
        raise PathNotMappedError(
            "The repository path is not covered by any WORKER_PATH_MAPPINGS entry."
        )
    mapping, depth = best
    return MappedPath(mapping, PurePosixPath(mapping.container, *segments[depth:]))


def to_container_path(host_path: str, mappings: Sequence[PathMapping]) -> PurePosixPath:
    """`map_host_path` 的簡寫：只要容器路徑。"""
    return map_host_path(host_path, mappings).container_path
