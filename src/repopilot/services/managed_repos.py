"""受管目錄（`MANAGED_REPOS_DIR`）的檔案系統操作：建立暫存目錄、發布為最終位置、安全清理。

目錄結構：

```
<MANAGED_REPOS_DIR>/
  .tmp/<隨機 16 位 hex>/        clone 進行中的暫存目錄（成功後 rename、失敗則清理）
  github/<owner>/<name>/        最終位置（owner／name 為 GitHub 回傳的標準大小寫）
```

刪除只發生在兩種情況：①「本次呼叫自己建立」的目錄（`remove_created_tree`：呼叫端傳入建立時取得的路徑與
預期的父目錄，這裡再驗證位於受管目錄內、父目錄吻合、不是受管目錄本身才遞迴刪除，失敗只記錄）；
②移除已註冊的 GitHub repo（`remove_github_clone`，D33：驗證不過或刪除失敗都拋出例外）。
"""

import contextlib
import logging
import os
import secrets
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from repopilot.errors import InvalidRepositoryPathError, ManagedStorageError
from repopilot.github.repo_ref import is_valid_owner_and_name
from repopilot.logging_config import APP_LOGGER_NAME
from repopilot.services.repositories import GITHUB_DIR_NAME

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.managed_repos")

TEMP_DIR_NAME = ".tmp"
_RENAME_ATTEMPTS = 3


def prepare_managed_root(managed_dir: Path) -> Path:
    """使用時才建立受管目錄（啟動與載入設定時不建立），回傳已 resolve 的路徑。"""
    try:
        managed_dir.mkdir(parents=True, exist_ok=True)
        return managed_dir.resolve(strict=True)
    except OSError as exc:
        logger.error("managed_root_unavailable", extra={"error_type": type(exc).__name__})
        raise ManagedStorageError("The managed repositories directory is unavailable.") from None


def temp_root(managed_root: Path) -> Path:
    return managed_root / TEMP_DIR_NAME


def create_temp_dir(managed_root: Path) -> Path:
    """建立本次專用的暫存目錄；`mkdir` 不帶 exist_ok，確保目錄一定是本次建立的。"""
    try:
        temp_root(managed_root).mkdir(exist_ok=True)
        path = temp_root(managed_root) / secrets.token_hex(8)
        path.mkdir()
    except OSError as exc:
        logger.error("managed_temp_unavailable", extra={"error_type": type(exc).__name__})
        raise ManagedStorageError("Could not create a temporary clone directory.") from None
    return path


def github_destination(managed_root: Path, owner: str, name: str) -> Path:
    return managed_root / GITHUB_DIR_NAME / owner / name


def publish(temp: Path, destination: Path) -> None:
    """將暫存目錄 rename 為最終位置；最終位置已存在時拋出 `FileExistsError`（不覆蓋）。

    同一磁碟區內的 rename 是原子的。Windows 上防毒或索引服務偶爾短暫占用剛寫入的檔案而造成
    `PermissionError`，因此重試數次。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_RENAME_ATTEMPTS):
        if os.path.lexists(destination):
            raise FileExistsError(destination.name)
        try:
            temp.rename(destination)
        except PermissionError:
            if attempt == _RENAME_ATTEMPTS - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
        else:
            return


def remove_created_tree(path: Path, *, managed_root: Path, expected_parent: Path) -> None:
    """遞迴刪除「本次建立」的目錄；驗證不通過就拒絕（不刪除、記錄錯誤）。

    驗證：父目錄（resolve 後）必須等於 `expected_parent`、位於 `managed_root` 內且不是
    `managed_root` 本身。清理失敗只記錄、不拋出，避免遮蔽原本的錯誤。
    """
    try:
        parent = path.parent.resolve(strict=True)
        expected = expected_parent.resolve(strict=True)
    except OSError:
        return  # 父目錄不存在，代表沒有東西可清理
    if (
        parent != expected
        or parent == managed_root
        or not parent.is_relative_to(managed_root)
        or path.name in ("", ".", "..")
    ):
        logger.error("managed_cleanup_refused")
        return
    try:
        if os.path.lexists(path):
            shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    except OSError as exc:
        logger.warning("managed_cleanup_failed", extra={"error_type": type(exc).__name__})


def remove_github_clone(managed_dir: Path, *, owner: str, name: str, recorded_path: str) -> bool:
    """移除已註冊 GitHub repo 在受管目錄內的 clone；回傳是否真的刪除了目錄（D33）。

    與 `remove_created_tree`（清理本次建立的暫存目錄，失敗只記錄）不同：這裡刪除的是「先前註冊、
    現在要移除」的目錄，所以**任何一項驗證不過都拋出例外且不刪除任何東西**，刪除失敗也拋出，讓呼叫端
    保留資料列、之後可重試。驗證全部通過才會刪除，且只會刪除 `<受管目錄>/github/<owner>/<name>`：

    1. `owner`／`name` 通過與註冊相同的格式規則（擋掉 `.`、`..`、分隔符號、保留名稱）。
    2. 資料列記錄的路徑正好等於由 owner／name 推得的位置（`normcase` 比較，Windows 大小寫不敏感）；
       指向受管目錄外或其他 clone 的竄改資料列會被拒絕。
    3. 路徑（含 `github`、`<owner>` 與 clone 本身）都不是 symlink／junction，且 `resolve` 後仍等於
       預期位置；連結不會被跟隨，也不會被刪除。
    4. 目標是目錄。

    目標不存在（已被手動刪除）視為成功並回傳 False。刪除後 `<owner>` 目錄若變空才以 `rmdir`
    移除（不遞迴）；絕不刪除受管目錄、`github` 目錄或其他 owner／repo。唯讀檔案（Windows 的
    `.git` 物件）以 `_clear_readonly_and_retry` 處理。驗證與刪除之間的競爭（TOCTOU）無法完全排除，
    前提是受管目錄只有本系統寫入。
    """
    if not is_valid_owner_and_name(owner, name):
        raise InvalidRepositoryPathError("The repository's GitHub owner or name is not valid.")
    # 受管目錄可能已不存在，使用非 strict 的 resolve
    managed_root = managed_dir.resolve()
    github_dir = managed_root / GITHUB_DIR_NAME
    owner_dir = github_dir / owner
    target = owner_dir / name
    if os.path.normcase(recorded_path) != os.path.normcase(str(target)):
        raise InvalidRepositoryPathError(
            "The path is not the managed location of this GitHub repository."
        )
    if not os.path.lexists(target):
        return False  # 已被手動刪除：視為成功，資料列仍可移除

    for part in (github_dir, owner_dir, target):
        if os.path.islink(part) or os.path.isjunction(part):
            raise InvalidRepositoryPathError("The managed clone path contains a link.")
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        raise InvalidRepositoryPathError("The managed clone path cannot be resolved.") from None
    if os.path.normcase(str(resolved)) != os.path.normcase(str(target)) or not resolved.is_dir():
        raise InvalidRepositoryPathError("The managed clone path is not a regular directory.")

    try:
        shutil.rmtree(target, onexc=_clear_readonly_and_retry)
    except OSError as exc:
        logger.error("managed_clone_remove_failed", extra={"error_type": type(exc).__name__})
        raise ManagedStorageError(
            "Could not remove the clone directory; close programs that use it and retry."
        ) from None
    with contextlib.suppress(OSError):  # 只有空目錄才會成功；其他 repo 仍在時 OSError，直接忽略
        owner_dir.rmdir()
    return True


def _clear_readonly_and_retry(
    function: Callable[..., Any], path: str, error: BaseException
) -> None:
    """Windows 上 `.git` 內的物件檔為唯讀，`rmtree` 會失敗；清除唯讀屬性後重試一次。"""
    if isinstance(error, PermissionError):
        os.chmod(path, stat.S_IWRITE)
        function(path)
        return
    raise error
