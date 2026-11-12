import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from repopilot.config import Settings
from repopilot.errors import InvalidRepositoryPathError
from repopilot.services.repositories import resolve_repository_path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "repos"
    directory.mkdir()
    return directory


def test_repository_roots_are_split_by_path_separator(tmp_path: Path) -> None:
    raw = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])

    settings = Settings(repository_roots=raw)

    assert settings.repository_roots == [tmp_path / "a", tmp_path / "b"]


def test_repository_roots_default_to_empty() -> None:
    assert (
        Settings(_env_file=None).repository_roots == []
    )  # 不讀本機 .env，避免開發者設定影響預設值測試


def test_git_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(git_timeout_seconds=0)


def test_path_inside_root_is_resolved(root: Path) -> None:
    project = root / "demo"
    project.mkdir()

    assert resolve_repository_path(str(project), [root]) == project.resolve()


def test_root_itself_is_allowed(root: Path) -> None:
    assert resolve_repository_path(str(root), [root]) == root.resolve()


def test_dotdot_is_normalized_before_root_check(root: Path) -> None:
    (root / "demo").mkdir()

    resolved = resolve_repository_path(str(root / "demo" / ".." / "demo"), [root])

    assert resolved == (root / "demo").resolve()


def test_sibling_directory_with_common_prefix_is_rejected(tmp_path: Path, root: Path) -> None:
    evil = tmp_path / "repos-evil"
    evil.mkdir()

    with pytest.raises(InvalidRepositoryPathError, match="outside"):
        resolve_repository_path(str(evil), [root])


def test_dotdot_escaping_root_is_rejected(tmp_path: Path, root: Path) -> None:
    with pytest.raises(InvalidRepositoryPathError, match="outside"):
        resolve_repository_path(str(root / ".."), [root])


@pytest.mark.skipif(os.name != "nt", reason="Windows 路徑大小寫不敏感")
def test_windows_path_case_is_normalized_to_actual_case(root: Path) -> None:
    project = root / "DemoRepo"
    project.mkdir()

    resolved = resolve_repository_path(str(root / "demorepo"), [Path(str(root).upper())])

    assert str(resolved) == str(project.resolve())


def test_symlink_pointing_outside_root_is_rejected(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("目前環境無權限建立 symlink")
        # Windows 無 symlink 權限時改用 junction（resolve 同樣會展開）
        subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],  # noqa: S607
            check=True,
            capture_output=True,
        )

    with pytest.raises(InvalidRepositoryPathError, match="outside"):
        resolve_repository_path(str(link), [root])


@pytest.mark.parametrize("raw", ["", "   ", "relative/path", "bad\x00path"])
def test_invalid_path_text_is_rejected(root: Path, raw: str) -> None:
    with pytest.raises(InvalidRepositoryPathError):
        resolve_repository_path(raw, [root])


def test_missing_path_is_rejected(root: Path) -> None:
    with pytest.raises(InvalidRepositoryPathError, match="does not exist"):
        resolve_repository_path(str(root / "missing"), [root])


def test_file_path_is_rejected(root: Path) -> None:
    file = root / "a.txt"
    file.write_text("x", encoding="utf-8")

    with pytest.raises(InvalidRepositoryPathError, match="not a directory"):
        resolve_repository_path(str(file), [root])


def test_no_configured_roots_rejects_everything(root: Path) -> None:
    with pytest.raises(InvalidRepositoryPathError, match="No REPOSITORY_ROOTS"):
        resolve_repository_path(str(root), [])


def test_missing_root_is_ignored_and_does_not_allow_anything(tmp_path: Path, root: Path) -> None:
    with pytest.raises(InvalidRepositoryPathError, match="No REPOSITORY_ROOTS"):
        resolve_repository_path(str(root), [tmp_path / "gone"])
