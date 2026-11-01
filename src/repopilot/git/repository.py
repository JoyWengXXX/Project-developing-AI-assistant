from pathlib import Path

from repopilot.errors import GitCommandError, InvalidRepositoryPathError
from repopilot.git.runner import run_git


def ensure_repository_root(path: Path, *, timeout: float) -> None:
    """確認 `path` 是非 bare Git repo 的根目錄；否則拋出 InvalidRepositoryPathError。

    `path` 必須是已正規化（resolve）的絕對路徑。
    """
    bare = run_git(path, ["rev-parse", "--is-bare-repository"], timeout=timeout)
    if bare.returncode != 0:
        if "dubious ownership" in bare.stderr:
            raise InvalidRepositoryPathError(
                "Git refuses to read this repository because of its ownership "
                "(dubious ownership). Fix the directory ownership; RepoPilot does not "
                "change Git's safe.directory setting."
            )
        # safe.bareRepository=explicit 時，Git 會拒絕在 bare repo 上執行指令
        if "bare repository" in bare.stderr:
            raise InvalidRepositoryPathError("Bare repositories are not supported.")
        raise InvalidRepositoryPathError("The path is not a Git repository.")
    if bare.stdout.strip() == b"true":
        raise InvalidRepositoryPathError("Bare repositories are not supported.")

    top = run_git(path, ["rev-parse", "--show-toplevel"], timeout=timeout)
    if top.returncode != 0:
        # 例如位於 .git 目錄內：有 repo 但沒有工作樹
        raise InvalidRepositoryPathError("The path is not the root of a Git working tree.")
    try:
        toplevel = Path(top.stdout.decode("utf-8").strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError):
        raise GitCommandError("Git returned an unexpected repository location.") from None
    if toplevel != path:
        raise InvalidRepositoryPathError(
            "The path is a subdirectory of a Git repository, not its root."
        )
