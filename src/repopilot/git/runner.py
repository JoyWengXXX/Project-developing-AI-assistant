import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from repopilot.errors import GitCommandError, GitTimeoutError
from repopilot.logging_config import APP_LOGGER_NAME

logger = logging.getLogger(f"{APP_LOGGER_NAME}.git")

STDERR_LIMIT = 500
# 這些環境變數會讓 Git 忽略 -C 指定的 repo，必須移除
_REPO_SELECTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: str


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_SELECTING_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(
    repo_path: Path, args: list[str], *, timeout: float, stdin: bytes | None = None
) -> GitResult:
    """在指定目錄執行唯讀 Git 指令；不檢查 return code，由呼叫端依指令語意判斷。

    stdout 保留 bytes；stderr 截斷後僅供日誌與錯誤判斷。逾時與無法啟動 Git 轉為領域例外。
    `stdin` 供 `cat-file --batch` 這類從標準輸入讀取物件 ID 的指令使用。
    """
    command = ["git", "-C", str(repo_path), *args]
    try:
        completed = subprocess.run(  # noqa: S603 - 參數陣列、shell=False，指令名稱固定
            command,
            shell=False,
            capture_output=True,
            timeout=timeout,
            check=False,
            input=stdin,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        logger.error("git_timeout", extra={"git_subcommand": args[0] if args else ""})
        raise GitTimeoutError("Git command timed out.") from None
    except OSError as exc:
        logger.error("git_unavailable", extra={"error_type": type(exc).__name__})
        raise GitCommandError("Git executable could not be started.") from None
    stderr = completed.stderr.decode("utf-8", errors="replace")[:STDERR_LIMIT]
    return GitResult(completed.returncode, completed.stdout, stderr)
