import subprocess
from pathlib import Path

import pytest

from repopilot.errors import GitTimeoutError, InvalidRepositoryPathError
from repopilot.git.repository import ensure_repository_root
from repopilot.git.runner import run_git
from tests.git_helpers import git, init_repo


def test_repo_root_is_accepted(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")

    ensure_repository_root(repo.resolve(), timeout=30)


def test_subdirectory_of_repo_is_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo", files={"src/a.py": "x = 1\n"})

    with pytest.raises(InvalidRepositoryPathError, match="subdirectory"):
        ensure_repository_root((repo / "src").resolve(), timeout=30)


def test_plain_directory_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(InvalidRepositoryPathError):
        ensure_repository_root(plain.resolve(), timeout=30)


def test_bare_repo_is_rejected(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    git(bare, "init", "--bare", "--quiet")

    with pytest.raises(InvalidRepositoryPathError, match="Bare"):
        ensure_repository_root(bare.resolve(), timeout=30)


def test_dot_git_directory_is_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")

    with pytest.raises(InvalidRepositoryPathError):
        ensure_repository_root((repo / ".git").resolve(), timeout=30)


def test_git_env_overrides_do_not_redirect_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "demo")
    other = init_repo(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    result = run_git(repo, ["rev-parse", "--show-toplevel"], timeout=30)

    assert Path(result.stdout.decode().strip()).resolve() == repo.resolve()


def test_timeout_is_converted_to_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("repopilot.git.runner.subprocess.run", fake_run)

    with pytest.raises(GitTimeoutError):
        run_git(tmp_path, ["status"], timeout=1)


def test_path_with_spaces_and_unicode_is_accepted(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "my repo 專案")

    ensure_repository_root(repo.resolve(), timeout=30)
