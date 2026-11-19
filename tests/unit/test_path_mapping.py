"""路徑映射（規格 9.6）：Windows 主機路徑 → 容器路徑，純字串運算，惡意輸入一律拒絕。"""

from pathlib import PurePosixPath

import pytest
from pydantic import ValidationError

from repopilot.errors import PathNotMappedError
from repopilot.path_mapping import (
    PathMapping,
    map_host_path,
    parse_host_path,
    to_container_path,
    to_host_path,
)

REPOS = PathMapping(host="D:/repos", container="/mnt/repos", mode="ro")
MANAGED = PathMapping(host=r"D:\repopilot-managed", container="/mnt/managed", mode="rw")
MAPPINGS = [REPOS, MANAGED]


def _map(path: str, mappings: list[PathMapping] | None = None) -> str:
    return str(to_container_path(path, MAPPINGS if mappings is None else mappings))


# --- 正常轉換 ---


@pytest.mark.parametrize(
    ("host_path", "expected"),
    [
        (r"D:\repos\demo", "/mnt/repos/demo"),
        (r"D:\repos\a\b\c", "/mnt/repos/a/b/c"),
        (r"D:\repos", "/mnt/repos"),  # 映射根本身
        (r"D:\repopilot-managed\github\octo\hello", "/mnt/managed/github/octo/hello"),
        (r"D:\repos\含 空白\專案", "/mnt/repos/含 空白/專案"),  # 空白與 CJK 保留
    ],
)
def test_maps_a_windows_path_into_the_container_path(host_path: str, expected: str) -> None:
    assert _map(host_path) == expected


def test_result_is_a_posix_path() -> None:
    assert isinstance(to_container_path(r"D:\repos\demo", MAPPINGS), PurePosixPath)


@pytest.mark.parametrize("drive", ["d:", "D:"])
@pytest.mark.parametrize("root", [r"repos", "REPOS", "Repos"])
def test_prefix_match_ignores_case_but_keeps_the_canonical_case_of_the_remainder(
    drive: str, root: str
) -> None:
    # 前綴不分大小寫（NTFS）；相對部分保留資料庫的標準大小寫，不在容器端「修正」
    assert _map(rf"{drive}\{root}\Demo\SubDir") == "/mnt/repos/Demo/SubDir"


def test_mapping_host_accepts_forward_slashes_and_a_trailing_separator() -> None:
    mapping = PathMapping(host="D:/repos/", container="/mnt/repos/", mode="ro")
    assert mapping.container == "/mnt/repos"
    assert _map(r"D:\repos\demo", [mapping]) == "/mnt/repos/demo"


def test_the_longest_matching_mapping_wins() -> None:
    nested = PathMapping(host=r"D:\repos\special", container="/mnt/special", mode="ro")
    assert _map(r"D:\repos\special\x", [REPOS, nested]) == "/mnt/special/x"
    assert _map(r"D:\repos\other\x", [REPOS, nested]) == "/mnt/repos/other/x"
    assert _map(r"D:\repos\special\x", [nested, REPOS]) == "/mnt/special/x"  # 與順序無關


def test_the_matched_mapping_is_reported() -> None:
    mapped = map_host_path(r"D:\repopilot-managed\github\a\b", MAPPINGS)
    assert mapped.mapping.mode == "rw"


# --- 找不到對應 ---


@pytest.mark.parametrize(
    "host_path",
    [
        r"D:\repos-evil\demo",  # 字串前綴相同但不是同一個目錄
        r"D:\reposx",
        r"D:\other\demo",
        r"E:\repos\demo",  # 不同磁碟機
        r"D:\Repo\demo",
        "D:\\",
    ],
)
def test_unmapped_paths_raise_path_not_mapped(host_path: str) -> None:
    with pytest.raises(PathNotMappedError) as caught:
        _map(host_path)
    assert caught.value.code == "path_not_mapped"


def test_empty_mapping_list_maps_nothing() -> None:
    with pytest.raises(PathNotMappedError):
        _map(r"D:\repos\demo", [])


def test_a_path_shorter_than_the_mapping_does_not_match() -> None:
    deep = PathMapping(host=r"D:\repos\deep\er", container="/mnt/deep", mode="ro")
    with pytest.raises(PathNotMappedError):
        _map(r"D:\repos\deep", [deep])


# --- 惡意或不合格的輸入 ---


@pytest.mark.parametrize(
    "host_path",
    [
        r"D:\repos\..\secret",  # 上層
        r"D:\repos\demo\..",
        r"D:\repos\.\demo",  # 單點區段
        r"D:\repos\\demo",  # 空區段（雙反斜線）
        "D:\\repos\\demo\\",  # 尾端分隔符
        "D:/repos/demo",  # 正斜線（資料庫路徑一律反斜線）
        r"D:\repos\de/mo",
        r"D:\repos\demo:stream",  # NTFS alternate data stream
        r"D:\repos\C:\x",
        r"D:\repos\demo.",  # Windows 會吃掉尾端句點與空白
        "D:\\repos\\demo ",
        r"D:\repos\de*mo",  # 萬用字元
        r"D:\repos\de?mo",
        'D:\\repos\\de"mo',
        r"D:\repos\de<mo",
        "D:\\repos\\de\x00mo",  # NUL
        "D:\\repos\\de\nmo",  # 控制字元
        r"D:\repos\CON",  # 裝置保留名稱
        r"D:\repos\nul.txt",
        r"D:\repos\com1",
        r"D:\repos\LPT9",
    ],
)
def test_malicious_or_malformed_host_paths_are_rejected(host_path: str) -> None:
    with pytest.raises(PathNotMappedError):
        _map(host_path)


@pytest.mark.parametrize(
    "host_path",
    [
        r"\\server\share\repos\demo",  # UNC
        r"\\?\D:\repos\demo",  # 裝置路徑
        r"\\.\D:\repos\demo",
        "repos\\demo",  # 相對路徑
        r"\repos\demo",  # 沒有磁碟機代號
        r"D:repos\demo",  # 磁碟機相對路徑
        "/mnt/repos/demo",  # POSIX 路徑
        "",
        "D:",
        "1:\\repos",
        "DD:\\repos",
    ],
)
def test_non_windows_absolute_forms_are_rejected(host_path: str) -> None:
    with pytest.raises(PathNotMappedError):
        _map(host_path)


def test_an_unc_path_is_rejected_even_if_a_mapping_looks_like_it() -> None:
    with pytest.raises(ValidationError):
        PathMapping(host=r"\\server\share", container="/mnt/x", mode="ro")


@pytest.mark.parametrize("non_text", [None, 123])
def test_non_string_input_is_rejected(non_text: object) -> None:
    with pytest.raises(PathNotMappedError):
        to_container_path(non_text, MAPPINGS)  # type: ignore[arg-type]


def test_the_error_message_does_not_echo_the_path() -> None:
    secret_path = r"D:\repos\..\TOP-SECRET-NAME"
    with pytest.raises(PathNotMappedError) as caught:
        _map(secret_path)
    assert "TOP-SECRET-NAME" not in caught.value.message
    with pytest.raises(PathNotMappedError) as caught_unmapped:
        _map(r"D:\elsewhere\TOP-SECRET-NAME")
    assert "TOP-SECRET-NAME" not in caught_unmapped.value.message


# --- 映射設定本身的驗證 ---


@pytest.mark.parametrize(
    "bad",
    [
        {"host": "repos", "container": "/mnt/repos", "mode": "ro"},  # 相對
        {"host": r"D:\repos\..\x", "container": "/mnt/repos", "mode": "ro"},
        {"host": r"D:\repos", "container": "mnt/repos", "mode": "ro"},  # 容器端須為絕對路徑
        {"host": r"D:\repos", "container": "/mnt/../etc", "mode": "ro"},
        {"host": r"D:\repos", "container": "/mnt/repos", "mode": "rwx"},
        {"host": r"D:\repos", "container": "/mnt/repos", "mode": "ro", "extra": "x"},
        {"host": r"D:\repos", "mode": "ro"},
    ],
)
def test_invalid_mapping_entries_are_rejected_at_load_time(bad: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        PathMapping.model_validate(bad)


def test_parse_host_path_returns_an_uppercase_drive_and_segments() -> None:
    assert parse_host_path(r"d:\Repos\Demo") == ("D:", ("Repos", "Demo"))
    assert parse_host_path("D:\\") == ("D:", ())


# --- 反向：容器路徑 → 主機路徑（worker 寫入資料庫前，切片 5，D44）---


@pytest.mark.parametrize(
    ("container_path", "expected"),
    [
        ("/mnt/managed/github/octo/hello", r"D:\repopilot-managed\github\octo\hello"),
        ("/mnt/managed", r"D:\repopilot-managed"),
        ("/mnt/repos/demo", r"D:\repos\demo"),
        ("/mnt/repos/含 空白/專案", r"D:\repos\含 空白\專案"),
    ],
)
def test_to_host_path_returns_the_windows_path_the_database_stores(
    container_path: str, expected: str
) -> None:
    assert to_host_path(container_path, MAPPINGS) == expected


@pytest.mark.parametrize(
    "host_path",
    [
        r"D:\repopilot-managed\github\Octo\Hello-World",
        r"D:\repos\demo",
        r"D:\repos\a\b\c",
    ],
)
def test_to_host_path_is_the_inverse_of_to_container_path(host_path: str) -> None:
    container = str(to_container_path(host_path, MAPPINGS))

    assert to_host_path(container, MAPPINGS) == host_path


def test_to_host_path_prefers_the_longest_container_prefix() -> None:
    nested = PathMapping(host=r"E:\special", container="/mnt/repos/special", mode="rw")

    assert to_host_path("/mnt/repos/special/x", [REPOS, nested]) == r"E:\special\x"
    assert to_host_path("/mnt/repos/other/x", [REPOS, nested]) == r"D:\repos\other\x"


@pytest.mark.parametrize(
    "container_path",
    [
        "/mnt/other/x",  # 沒有映射
        "/mnt/managed-evil/x",  # 前綴要逐段比對
        "/mnt/managed/../repos/x",  # `..` 區段
        "/mnt/managed/con",  # Windows 保留名稱
        "relative/path",
        "",
    ],
)
def test_to_host_path_rejects_unmapped_or_suspicious_paths(container_path: str) -> None:
    with pytest.raises(PathNotMappedError):
        to_host_path(container_path, MAPPINGS)
