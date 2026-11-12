import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

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


def commit_files(repo: Path, files: Mapping[str, str | bytes], *, message: str = "update") -> str:
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


def commit_raw_paths(
    repo: Path, files: Mapping[bytes, bytes], *, message: str = "raw paths", branch: str = "main"
) -> str:
    """以 `mktree` 直接建立 commit，路徑以原始位元組指定，繞過作業系統與 Git 的路徑檢查。

    用來做惡意或極端的 tree：`..`、`.git` 區段、Windows 保留名、尾端空白、非 UTF-8 位元組、
    超長路徑等，這些在 Windows 的工作目錄與 `git add` 都無法（或不允許）建立。
    `files` 的 key 以 `/` 分隔巢狀路徑；commit 成為 `branch` 的新 tip，不碰工作目錄與 index。
    """
    tree: dict[bytes, Any] = {}
    for path, data in files.items():
        parts = path.split(b"/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = write_blob(repo, data)

    def write_tree(node: dict[bytes, Any]) -> str:
        lines = b""
        for name, value in node.items():
            if isinstance(value, dict):
                lines += b"040000 tree " + write_tree(value).encode() + b"\t" + name + b"\x00"
            else:
                lines += b"100644 blob " + value.encode() + b"\t" + name + b"\x00"
        completed = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "mktree", "-z"],  # noqa: S607
            input=lines,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return completed.stdout.decode("ascii").strip()

    root = write_tree(tree)
    commit = git(repo, "commit-tree", root, "-m", message)
    git(repo, "update-ref", f"refs/heads/{branch}", commit)
    return commit


def link_directory(link: Path, target: Path) -> None:
    """建立指向目錄的 symlink；Windows 無權限時改用 junction，都不行就 skip（不靜默通過）。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("無法建立 symlink")
        completed = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip("無法建立 symlink 或 junction")


class RemoteRepo:
    """以本機 bare repo 模擬 GitHub 上的遠端：`<base>/<owner>/<name>.git`。

    `work` 是用來產生 commit 的工作樹；`push()` 把結果推到 bare repo。受測程式 clone 的是 bare repo
    （以 file:// 或本機 HTTP 伺服器存取）。
    """

    def __init__(self, base: Path, owner: str, name: str, files: dict[str, str] | None = None):
        self.work = init_repo(base.parent / f"{base.name}-work" / owner / name, files=files)
        self.bare = base / owner / f"{name}.git"
        self.bare.parent.mkdir(parents=True, exist_ok=True)
        git(self.work, "clone", "--bare", "--quiet", str(self.work), str(self.bare))

    def commit(self, files: Mapping[str, str | bytes], *, message: str = "update") -> str:
        sha = commit_files(self.work, files, message=message)
        self.push()
        return sha

    def push(self, *refspecs: str) -> None:
        git(self.work, "push", "--quiet", str(self.bare), *(refspecs or ("HEAD:refs/heads/main",)))

    def head(self) -> str:
        return git(self.work, "rev-parse", "HEAD")

    def add_tags(self, names: list[str]) -> None:
        """在 bare repo 一次建立多個 tag（`update-ref --stdin`，避免數百次 subprocess）。"""
        sha = self.head()
        commands = "".join(f"create refs/tags/{name} {sha}\n" for name in names)
        subprocess.run(  # noqa: S603
            ["git", "--git-dir", str(self.bare), "update-ref", "--stdin"],  # noqa: S607
            input=commands.encode(),
            capture_output=True,
            check=True,
            timeout=30,
        )

    def delete_branch(self, name: str) -> None:
        # 使用 --git-dir：使用者若設定 safe.bareRepository=explicit，`-C <bare>` 會被 Git 拒絕
        subprocess.run(  # noqa: S603
            ["git", "--git-dir", str(self.bare), "update-ref", "-d", f"refs/heads/{name}"],  # noqa: S607
            capture_output=True,
            check=True,
            timeout=30,
        )
