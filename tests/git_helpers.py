import subprocess
from pathlib import Path

_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]


def git(repo: Path, *args: str) -> str:
    """測試用 Git 指令：固定身分，不依賴也不修改使用者的 Git 設定。"""
    completed = subprocess.run(  # noqa: S603
        ["git", *_IDENTITY, "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        check=True,
        timeout=30,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def init_repo(path: Path, *, files: dict[str, str] | None = None) -> Path:
    """建立含一個 commit 的暫存 Git repo。"""
    path.mkdir(parents=True)
    git(path, "init", "--quiet", "--initial-branch=main")
    for name, content in (files or {"README.md": "hello\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    git(path, "add", "--all")
    git(path, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "initial")
    return path
