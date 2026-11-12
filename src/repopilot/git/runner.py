import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping
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
    poll: Callable[[], None] | None = None,
    poll_interval: float = 1.0,
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
    `poll`（第二階段切片 5，D44；只能搭配 `kill_process_tree=True`）：指令執行期間每 `poll_interval`
    秒呼叫一次（在呼叫端的執行緒內），供 worker 檢查取消旗標與租約、回報進度。`poll` 拋出任何例外
    就終止整個程序樹（POSIX 先 SIGTERM 讓 git 自己清理鎖檔與暫存檔，寬限後才 SIGKILL）並把例外
    原樣往外拋；沒有給 `poll` 時行為與切片 5 之前完全相同。
    """
    command = ["git", "-C", str(repo_path), *args]
    final_env = _git_env(env)
    if poll is not None and not kill_process_tree:
        raise ValueError("poll requires kill_process_tree=True.")
    try:
        if kill_process_tree:
            # 只有需要輪詢時才多傳兩個參數（既有測試以固定簽章的函式取代 _run_killing_tree）
            polling: dict[str, Any] = (
                {"poll": poll, "poll_interval": poll_interval} if poll is not None else {}
            )
            returncode, stdout, raw_stderr = _run_killing_tree(
                command, timeout=timeout, stdin=stdin, env=final_env, **polling
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
    command: list[str],
    *,
    timeout: float,
    stdin: bytes | None,
    env: dict[str, str],
    poll: Callable[[], None] | None = None,
    poll_interval: float = 1.0,
) -> tuple[int, bytes, bytes]:
    """執行指令；逾時時殺掉整個程序樹後拋出 `subprocess.TimeoutExpired`。

    給了 `poll` 時，等待輸出的過程切成 `poll_interval` 秒一段，每段結束呼叫一次 `poll()`；
    `poll` 拋出的例外會先終止程序樹（寬限式，見 `_kill_tree`）再原樣往外拋。
    """
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
            if poll is None:
                stdout, stderr = process.communicate(stdin, timeout=timeout)
            else:
                stdout, stderr = _communicate_polling(process, stdin, timeout, poll, poll_interval)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            _reap(process)
            raise
        except BaseException:
            # poll 要求中止（取消、租約遺失）或呼叫端被打斷：不留下任何 git 程序
            _kill_tree(process, grace_seconds=_TERMINATE_GRACE_SECONDS)
            _reap(process)
            raise
        return process.returncode, stdout, stderr


# 寬限式終止：先送 SIGTERM（git 會清理自己的鎖檔、暫存檔，clone 會移除半成品目錄），
# 這麼多秒內沒結束才 SIGKILL
_TERMINATE_GRACE_SECONDS = 5.0


def _communicate_polling(
    process: "subprocess.Popen[bytes]",
    stdin: bytes | None,
    timeout: float,
    poll: Callable[[], None],
    interval: float,
) -> tuple[bytes, bytes]:
    deadline = time.monotonic() + timeout
    first = True
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            # 逾時後重試 communicate 不會遺失輸出；輸入只能在第一次傳入
            return process.communicate(stdin if first else None, timeout=min(interval, remaining))
        except subprocess.TimeoutExpired:
            first = False
            poll()


def _reap(process: "subprocess.Popen[bytes]") -> None:
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        logger.error("git_tree_kill_incomplete")


def _kill_tree(process: "subprocess.Popen[bytes]", *, grace_seconds: float = 0.0) -> None:
    try:
        if sys.platform == "win32":
            # /T 連同子孫一起結束；pid 是整數，不含使用者輸入。Windows 沒有可靠的寬限式終止
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],  # noqa: S607
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            if grace_seconds > 0:
                os.killpg(process.pid, signal.SIGTERM)  # start_new_session 使 pgid == pid
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=grace_seconds)
            os.killpg(process.pid, signal.SIGKILL)  # 結束寬限後仍存活的成員（群組已空時 OSError）
    except (OSError, subprocess.SubprocessError):
        pass
    process.kill()  # 保險：至少結束最外層程序（已結束時無作用）
