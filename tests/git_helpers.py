import subprocess
from pathlib import Path

_IDENTITY = [
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    # 避免使用者的 autocrlf 設定改變 blob 位元組，使測試結果與本機設定無關
    "-c",
    "core.autocrlf=false",
]


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


def commit_files(repo: Path, files: dict[str, str | bytes], *, message: str = "update") -> str:
    """寫入（或覆寫）檔案並提交，回傳新 commit 的 SHA；str 以 UTF-8 編碼，bytes 原樣寫入。"""
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        target.write_bytes(data)
    return commit_all(repo, message=message)


def commit_all(repo: Path, *, message: str = "update") -> str:
    git(repo, "add", "--all")
    git(repo, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def write_blob(repo: Path, data: bytes) -> str:
    """直接寫入 Git 物件資料庫，回傳 blob ID。"""
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],  # noqa: S607
        input=data,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.decode("ascii").strip()


def commit_special_entries(
    repo: Path, *, symlinks: dict[str, str] | None = None, gitlinks: dict[str, str] | None = None
) -> str:
    """以 index 直接加入 symlink／gitlink 項目，不依賴作業系統的 symlink 權限。"""
    for path, target in (symlinks or {}).items():
        oid = write_blob(repo, target.encode("utf-8"))
        git(repo, "update-index", "--add", "--cacheinfo", f"120000,{oid},{path}")
    for path, sha in (gitlinks or {}).items():
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")
    git(repo, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "special entries")
    return git(repo, "rev-parse", "HEAD")
