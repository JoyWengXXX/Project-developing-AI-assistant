import logging
import os
import signal
import subprocess
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repopilot.errors import GitCommandError, GitTimeoutError
from repopilot.git.redaction import redact_secrets
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


def _git_env(overrides: Mapping[str, str | None] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_SELECTING_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _subcommand(args: list[str]) -> str:
    """略過開頭的 `-c key=value`，回傳 Git 子指令名稱（僅供日誌）。"""
    index = 0
    while index + 1 < len(args) and args[index] == "-c":
        index += 2
    return args[index] if index < len(args) else ""


def run_git(
    repo_path: Path,
    args: list[str],
    *,
    timeout: float,
    stdin: bytes | None = None,
    env: Mapping[str, str | None] | None = None,
    kill_process_tree: bool = False,
    secrets: Collection[str] = (),
) -> GitResult:
    """在指定目錄執行 Git 指令；不檢查 return code，由呼叫端依指令語意判斷。

    stdout 保留 bytes；stderr 截斷後僅供日誌與錯誤判斷。逾時與無法啟動 Git 轉為領域例外。
    `stdin` 供 `cat-file --batch` 這類從標準輸入讀取物件 ID 的指令使用。
    `env` 是額外的環境變數覆寫（值為 None 表示移除）；秘密（例如 GitHub token）只能經由這裡
    傳入，不得放進 `args`（argv 在程序列表中可被其他使用者看見）。
    `kill_process_tree`：網路指令（clone／fetch）使用。`git` 會再啟動 `git-remote-https` 等子程序
    （Windows 的 `git.exe` 啟動器也會再啟動真正的 git），`subprocess.run` 逾時只殺最外層，
    子程序仍握著輸出管線而讓呼叫卡到子程序自己結束；因此逾時時殺掉整個程序樹。
    `secrets`：須從 stderr 遮蔽的秘密字串；在截斷**之前**遮蔽，避免秘密被截斷線切成殘片而漏網。
    """
    command = ["git", "-C", str(repo_path), *args]
    final_env = _git_env(env)
    try:
        if kill_process_tree:
            returncode, stdout, raw_stderr = _run_killing_tree(
                command, timeout=timeout, stdin=stdin, env=final_env
            )
        else:
            completed = subprocess.run(  # noqa: S603 - 參數陣列、shell=False，指令名稱固定
                command,
                shell=False,
                capture_output=True,
                timeout=timeout,
                check=False,
                input=stdin,
                env=final_env,
            )
            returncode, stdout, raw_stderr = (
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
    except subprocess.TimeoutExpired:
        logger.error("git_timeout", extra={"git_subcommand": _subcommand(args)})
        raise GitTimeoutError("Git command timed out.") from None
    except OSError as exc:
        logger.error("git_unavailable", extra={"error_type": type(exc).__name__})
        raise GitCommandError("Git executable could not be started.") from None
    decoded = raw_stderr.decode("utf-8", errors="replace")
    if secrets:
        decoded = redact_secrets(decoded, secrets)
    return GitResult(returncode, stdout, decoded[:STDERR_LIMIT])


def _run_killing_tree(
    command: list[str], *, timeout: float, stdin: bytes | None, env: dict[str, str]
) -> tuple[int, bytes, bytes]:
    """執行指令；逾時時殺掉整個程序樹後拋出 `subprocess.TimeoutExpired`。"""
    group: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with subprocess.Popen(  # noqa: S603 - 參數陣列、shell=False，指令名稱固定
        command,
        shell=False,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **group,
    ) as process:
        try:
            stdout, stderr = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error("git_tree_kill_incomplete")
            raise
        return process.returncode, stdout, stderr


def _kill_tree(process: "subprocess.Popen[bytes]") -> None:
    try:
        if sys.platform == "win32":
            # /T 連同子孫一起結束；pid 是整數，不含使用者輸入
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)  # start_new_session 使 pgid == pid
    except (OSError, subprocess.SubprocessError):
        pass
    process.kill()  # 保險：至少結束最外層程序（已結束時無作用）
