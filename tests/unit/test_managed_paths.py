"""MANAGED_REPOS_DIR 設定與「依 source_type 決定允許根目錄」的路徑驗證（D28 擴充，D32）。"""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from repopilot.config import Settings
from repopilot.db.models import Repository
from repopilot.errors import InvalidRepositoryPathError
from repopilot.services.repositories import resolve_registered_path, resolve_repository_path

# --- 設定 ---


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_managed_dir_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANAGED_REPOS_DIR", raising=False)

    settings = _settings()

    assert settings.managed_repos_dir is None
    assert settings.github_clone_timeout_seconds == 300
    assert settings.github_clone_base_url == "https://github.com"


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_managed_dir_means_disabled_not_current_directory(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MANAGED_REPOS_DIR", raw)

    assert _settings().managed_repos_dir is None


def test_managed_dir_must_be_absolute() -> None:
    with pytest.raises(ValidationError):
        _settings(managed_repos_dir="relative/dir")


def test_managed_dir_is_read_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MANAGED_REPOS_DIR", str(tmp_path / "managed"))

    assert _settings().managed_repos_dir == tmp_path / "managed"


def test_creating_settings_does_not_touch_the_filesystem(tmp_path: Path) -> None:
    target = tmp_path / "never-created"

    _settings(managed_repos_dir=target)

    assert not target.exists()


@pytest.mark.parametrize("timeout", [0, -1, 3601])
def test_clone_timeout_range(timeout: float) -> None:
    with pytest.raises(ValidationError):
        _settings(github_clone_timeout_seconds=timeout)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com",
        "https://github.com/",
        "file:///tmp/remotes",
        "http://127.0.0.1:8123",
        "http://localhost:8123/",
    ],
)
def test_clone_base_url_accepts_https_file_and_loopback_http(url: str) -> None:
    assert _settings(github_clone_base_url=url).github_clone_base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com",  # 明文 http：token 會以明文送出
        "http://evil.example.com",
        "git://github.com",
        "ssh://git@github.com",
        "ftp://github.com",
        "https://user:pass@github.com",
        "https://github.com?x=1",
        "github.com",
        "",
    ],
)
def test_clone_base_url_rejects_other_transports_and_credentials(url: str) -> None:
    with pytest.raises(ValidationError):
        _settings(github_clone_base_url=url)


# --- 依來源決定允許的根目錄 ---


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    directory = tmp_path / "repos"
    directory.mkdir()
    return directory


@pytest.fixture
def managed(tmp_path: Path) -> Path:
    directory = tmp_path / "managed"
    (directory / "github" / "Octo" / "Repo").mkdir(parents=True)
    return directory


def _github_row(path: Path, owner: str = "Octo", name: str = "Repo") -> Repository:
    return Repository(
        canonical_local_path=str(path),
        source_type="github",
        github_owner=owner,
        github_name=name,
    )


def _local_row(path: Path) -> Repository:
    return Repository(canonical_local_path=str(path), source_type="local")


def test_github_repository_must_be_at_its_managed_location(roots: Path, managed: Path) -> None:
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)
    path = managed / "github" / "Octo" / "Repo"

    assert resolve_registered_path(_github_row(path), settings) == path.resolve()


def test_github_repository_outside_managed_dir_is_rejected_even_if_in_repository_roots(
    roots: Path, managed: Path
) -> None:
    (roots / "elsewhere").mkdir()
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)

    with pytest.raises(InvalidRepositoryPathError):
        resolve_registered_path(_github_row(roots / "elsewhere"), settings)


def test_github_row_pointing_to_another_directory_inside_managed_dir_is_rejected(
    roots: Path, managed: Path
) -> None:
    other = managed / "github" / "Other" / "Thing"
    other.mkdir(parents=True)
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)

    with pytest.raises(InvalidRepositoryPathError):
        resolve_registered_path(_github_row(other), settings)  # owner/name 是 Octo/Repo


def test_github_row_pointing_to_managed_root_or_tmp_is_rejected(roots: Path, managed: Path) -> None:
    (managed / ".tmp").mkdir()
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)

    for path in (managed, managed / ".tmp", managed / "github"):
        with pytest.raises(InvalidRepositoryPathError):
            resolve_registered_path(_github_row(path), settings)


def test_github_repository_requires_managed_dir_configuration(roots: Path, managed: Path) -> None:
    settings = _settings(repository_roots=[roots])

    with pytest.raises(InvalidRepositoryPathError):
        resolve_registered_path(_github_row(managed / "github" / "Octo" / "Repo"), settings)


def test_github_row_cannot_use_a_path_that_escapes_via_dotdot(roots: Path, managed: Path) -> None:
    (roots / "evil").mkdir()
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)
    sneaky = managed / "github" / "Octo" / "Repo" / ".." / ".." / ".." / ".." / "repos" / "evil"

    with pytest.raises(InvalidRepositoryPathError):
        resolve_registered_path(_github_row(sneaky), settings)


def test_github_repository_path_comparison_ignores_case_only_on_case_insensitive_systems(
    roots: Path, managed: Path
) -> None:
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)
    path = managed / "github" / "Octo" / "Repo"

    # 資料列的 owner／name 大小寫與實際目錄不同：Windows 為同一個目錄（通過）；
    # Linux 為不同目錄（拒絕）
    row = _github_row(path, owner="octo", name="repo")
    if os.name == "nt":
        assert resolve_registered_path(row, settings) == path.resolve()
    else:
        with pytest.raises(InvalidRepositoryPathError):
            resolve_registered_path(row, settings)


def test_local_repository_must_be_in_repository_roots(roots: Path, managed: Path) -> None:
    project = roots / "demo"
    project.mkdir()
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)

    assert resolve_registered_path(_local_row(project), settings) == project.resolve()
    with pytest.raises(InvalidRepositoryPathError):
        resolve_registered_path(_local_row(managed / "github" / "Octo" / "Repo"), settings)


def test_local_repository_inside_managed_dir_is_rejected_even_if_roots_contain_it(
    tmp_path: Path,
) -> None:
    # 設定者把受管目錄放在 REPOSITORY_ROOTS 之內：本機來源仍不得使用受管目錄
    roots = tmp_path / "repos"
    managed = roots / "managed"
    clone = managed / "github" / "Octo" / "Repo"
    clone.mkdir(parents=True)
    settings = _settings(repository_roots=[roots], managed_repos_dir=managed)

    with pytest.raises(InvalidRepositoryPathError, match="MANAGED_REPOS_DIR"):
        resolve_registered_path(_local_row(clone), settings)
    with pytest.raises(InvalidRepositoryPathError):
        resolve_repository_path(str(clone), [roots], reserved_roots=[managed])


def test_reserved_roots_default_keeps_existing_behavior(roots: Path) -> None:
    project = roots / "demo"
    project.mkdir()

    assert resolve_repository_path(str(project), [roots]) == project.resolve()
