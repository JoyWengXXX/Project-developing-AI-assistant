"""GitHub 來源 repo 的 `git clone`／`git fetch`；本專案唯一會存取網路的 Git 指令。

安全約定（規則 3、6）：
- 參數陣列、`shell=False`、逾時、檢查 return code、轉成領域例外。
- **token 只經環境變數傳入**（`http.<base>/.extraheader`），不進 argv、不寫入 `.git/config`；
  stderr 與例外訊息先遮蔽可能含 token 的字串，且回給使用者的訊息是固定文字，不含 stderr 原文。
- 不繼承使用者的 Git 設定：`GIT_CONFIG_GLOBAL` 指向空裝置（跳過 `~/.gitconfig` 與
  `$XDG_CONFIG_HOME/git/config`，避免 `url.insteadOf`、`credential.helper`、`core.hooksPath` 等
  影響 clone）；系統層設定保留（Git for Windows 的 CA／SSL 後端設定在這裡），但以
  `-c credential.helper=` 清除其憑證輔助程式，避免 token 或登入視窗進入作業系統憑證庫。
"""

import base64
import logging
import os
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from repopilot.errors import (
    GitCommandError,
    GitHubCloneError,
    GitHubCloneTimeoutError,
    GitHubFetchError,
    GitHubFetchTimeoutError,
    GitTimeoutError,
)
from repopilot.git.redaction import redact_secrets
from repopilot.git.runner import STDERR_LIMIT, run_git
from repopilot.logging_config import APP_LOGGER_NAME

logger = logging.getLogger(f"{APP_LOGGER_NAME}.git.remote")

__all__ = [
    "RefChange",
    "build_clone_url",
    "clone_repository",
    "fetch_origin",
    "redact_secrets",
]

# 網路指令共用的 `-c` 選項（非秘密，可以出現在 argv）：
# - credential.helper=：清除系統層設定的憑證輔助程式（例如 Windows 的 Git Credential Manager）
# - core.hooksPath=<空裝置>：不執行任何 hook（`reference-transaction` 等可能在 fetch 時觸發）
# - gc.autoDetach／maintenance.autoDetach=false：fetch 後的自動整理在前景進行並受逾時限制，
#   不留下背景程序（背景程序會占用檔案，讓 Windows 上的清理失敗）
_NETWORK_CONFIG: tuple[str, ...] = (
    "-c",
    "credential.helper=",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "gc.autoDetach=false",
    "-c",
    "maintenance.autoDetach=false",
)
# 這些變數可能讓 Git 呼叫外部程式詢問帳密；移除之（`GCM_INTERACTIVE=never` 另見下方）
_DROPPED_ENV = ("GIT_ASKPASS", "SSH_ASKPASS")


def build_clone_url(base_url: str, owner: str, name: str) -> str:
    """由系統組成 clone URL；owner／name 須已通過驗證，使用者輸入不會直接成為 URL。"""
    return f"{base_url.rstrip('/')}/{owner}/{name}.git"


@dataclass(frozen=True)
class _RemoteCommand:
    env: dict[str, str | None]
    secrets: tuple[str, ...]


def _remote_command(base_url: str, token: str | None) -> _RemoteCommand:
    """組成網路指令的環境變數；token 以 `http.<base>/.extraheader` 傳入，不進 argv。

    `GIT_ALLOW_PROTOCOL` 取自 base URL 的 scheme：預設（https://github.com）只允許 https；
    只有操作者把 `GITHUB_CLONE_BASE_URL` 改成本機 fixture 時才會放寬，且放寬的只有該 scheme。
    token 的標頭範圍限於 base URL，因此不會送往其他主機。
    """
    # 先清掉繼承來的 GIT_CONFIG_*（COUNT／KEY_n／VALUE_n／PARAMETERS），避免與下面的設定混合
    env: dict[str, str | None] = {k: None for k in os.environ if k.upper().startswith("GIT_CONFIG")}
    env.update({name: None for name in _DROPPED_ENV})
    env.update(
        {
            "GIT_ALLOW_PROTOCOL": urlsplit(base_url).scheme,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GCM_INTERACTIVE": "never",
        }
    )
    secrets: tuple[str, ...] = ()
    if token:
        credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{base_url.rstrip('/')}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: basic {credentials}",
            }
        )
        secrets = (token, credentials)
    return _RemoteCommand(env=env, secrets=secrets)


def clone_repository(
    work_dir: Path,
    destination: Path,
    *,
    base_url: str,
    owner: str,
    name: str,
    token: str | None,
    timeout: float,
    poll: Callable[[], None] | None = None,
    poll_interval: float = 1.0,
) -> None:
    """完整 clone（非 shallow、非 partial）到 `destination`，不 checkout 工作樹。

    `--no-checkout`：不產生工作目錄檔案，避免 checkout 階段的檔名與符號連結風險；仍是非 bare
    repo，符合 D19 的 repo 根目錄檢查。`--template=`：不複製範本（含範例 hook）。
    `destination` 須不存在，或為空目錄（Git 自行建立內容）。

    `poll`／`poll_interval`（D44）：worker 用來在 clone 期間每隔幾秒檢查取消旗標、租約並回報
    進度；`poll` 拋出的例外會終止 git 程序樹並原樣往外拋（不轉成領域例外）。
    """
    command = _remote_command(base_url, token)
    url = build_clone_url(base_url, owner, name)
    args = [
        *_NETWORK_CONFIG,
        "clone",
        "--no-checkout",
        "--quiet",
        "--template=",
        "--",
        url,
        str(destination),
    ]
    try:
        result = run_git(
            work_dir,
            args,
            timeout=timeout,
            env=command.env,
            kill_process_tree=True,
            secrets=command.secrets,
            **_polling(poll, poll_interval),
        )
    except GitTimeoutError:
        raise GitHubCloneTimeoutError("git clone timed out.") from None
    if result.returncode != 0:
        _log_failure("github_clone_failed", result.returncode, result.stderr, command.secrets)
        raise GitHubCloneError(
            "git clone failed (the repository may be unreachable, or the token may lack access)."
        )


def _polling(poll: Callable[[], None] | None, interval: float) -> dict[str, Any]:
    """只有需要輪詢時才帶 poll 參數（既有測試以固定簽章的函式取代 run_git 的底層）。"""
    return {} if poll is None else {"poll": poll, "poll_interval": interval}


@dataclass(frozen=True)
class RefChange:
    """fetch 前後的 ref 差異；`name` 可直接作為匯入的 revision（`origin/main`、`v1.0`）。"""

    name: str
    kind: Literal["branch", "tag"]
    change: Literal["added", "updated", "removed"]
    old_sha: str | None
    new_sha: str | None


def fetch_origin(
    repo_path: Path,
    *,
    base_url: str,
    token: str | None,
    timeout: float,
    poll: Callable[[], None] | None = None,
    poll_interval: float = 1.0,
) -> list[RefChange]:
    """對 `origin` 執行 `git fetch --prune`，回傳新增／更新／移除的 ref（依名稱排序）。

    只能對受管目錄內的 repo 呼叫（呼叫端已驗證路徑）。ref 差異由 fetch 前後的
    `for-each-ref` 比較得出。`poll`／`poll_interval` 同 `clone_repository`。
    """
    command = _remote_command(base_url, token)
    before = _list_refs(repo_path, timeout)
    args = [
        *_NETWORK_CONFIG,
        "fetch",
        "--prune",
        "--quiet",
        "--no-recurse-submodules",
        "origin",
    ]
    try:
        result = run_git(
            repo_path,
            args,
            timeout=timeout,
            env=command.env,
            kill_process_tree=True,
            secrets=command.secrets,
            **_polling(poll, poll_interval),
        )
    except GitTimeoutError:
        raise GitHubFetchTimeoutError("git fetch timed out.") from None
    if result.returncode != 0:
        _log_failure("github_fetch_failed", result.returncode, result.stderr, command.secrets)
        raise GitHubFetchError(
            "git fetch failed (the repository may be unreachable, or the token may lack access)."
        )
    return _diff_refs(before, _list_refs(repo_path, timeout))


def _log_failure(event: str, returncode: int, stderr: str, secrets: Collection[str]) -> None:
    # stderr 已被 runner 截斷；遮蔽後才記錄，且不回給使用者
    logger.warning(
        event,
        extra={
            "returncode": returncode,
            "stderr": redact_secrets(stderr, secrets)[:STDERR_LIMIT],
        },
    )


def _list_refs(repo_path: Path, timeout: float) -> dict[str, str]:
    """列出 `refs/remotes/origin/*`（排除 HEAD 符號參照）與 `refs/tags/*` → object id。"""
    result = run_git(
        repo_path,
        ["for-each-ref", "--format=%(objectname) %(refname)", "refs/remotes/origin", "refs/tags"],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise GitCommandError("Could not list the repository refs.")
    refs: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        sha, _, refname = line.partition(" ")
        if sha and refname and refname != "refs/remotes/origin/HEAD":
            refs[refname] = sha
    return refs


def _diff_refs(before: Mapping[str, str], after: Mapping[str, str]) -> list[RefChange]:
    changes: list[RefChange] = []
    for refname in sorted(before.keys() | after.keys()):
        old, new = before.get(refname), after.get(refname)
        if old == new:
            continue
        change: Literal["added", "updated", "removed"] = (
            "added" if old is None else "removed" if new is None else "updated"
        )
        if refname.startswith("refs/tags/"):
            kind: Literal["branch", "tag"] = "tag"
            display = refname.removeprefix("refs/tags/")
        else:
            kind = "branch"
            display = refname.removeprefix("refs/remotes/")
        changes.append(RefChange(display, kind, change, old, new))
    return changes
