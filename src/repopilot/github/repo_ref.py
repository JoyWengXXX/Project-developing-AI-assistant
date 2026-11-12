"""GitHub repo 參考（`owner/name` 或 `https://github.com/owner/name`）的驗證與正規化。

純函式、不做 I/O。這裡的結果會成為 API 路徑、clone URL 與受管目錄名稱，因此規則刻意比 GitHub
實際允許的更窄（見各檢查的註解）；使用者輸入絕不直接拼進 URL、路徑或命令選項。
"""

import re
from dataclasses import dataclass

from repopilot.errors import InvalidGitHubRepoError

# 比任何合法輸入都長（URL 形式最長約 164 字元）；先擋掉超長輸入，不進入正規表達式，也不回顯
_MAX_INPUT_LENGTH = 256
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
# 只接受 https://github.com/...（scheme 與主機不分大小寫）；不接受 http、ssh、其他主機、帳密、埠
_URL_PREFIX_RE = re.compile(r"https://github\.com/", re.IGNORECASE)
# Windows 保留的裝置名稱（含帶副檔名者，例如 `nul.txt`）；用作目錄名稱會出錯或指向裝置
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

INVALID_MESSAGE = "Invalid GitHub repository. Use 'owner/name' or 'https://github.com/owner/name'."


@dataclass(frozen=True)
class GitHubRepoRef:
    owner: str
    name: str


def parse_github_repo(raw: str) -> GitHubRepoRef:
    """驗證並正規化使用者輸入；不合法一律拋出 `InvalidGitHubRepoError`（訊息不回顯輸入）。

    - 接受 `owner/name`，或 `https://github.com/owner/name(.git)`（可有結尾斜線）。
    - 不接受空白、控制字元（含換行）、query／fragment、帳密、多餘路徑段、其他主機或協定。
    - 短格式不剝除 `.git`：`owner/name.git` 視為無效；URL 形式先剝除一個 `.git`。
    """
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_INPUT_LENGTH:
        raise InvalidGitHubRepoError(INVALID_MESSAGE)
    # 嚴格限定可見 ASCII：擋掉空白、換行、NUL 等控制字元與非 ASCII 字元（`%` 本身可見，但之後的
    # 字元集檢查不接受，所以百分比編碼的 `%2e%2e` 也會被拒絕）
    if not all("\x21" <= char <= "\x7e" for char in raw):
        raise InvalidGitHubRepoError(INVALID_MESSAGE)

    prefix = _URL_PREFIX_RE.match(raw)
    if prefix is not None:
        path = raw[prefix.end() :]
        is_url = True
    else:
        path = raw
        is_url = False

    if is_url:
        # 容許單一結尾斜線；query、fragment、帳密、第三段路徑都會讓下面的檢查失敗
        path = path.removesuffix("/")

    parts = path.split("/")
    if len(parts) != 2:
        raise InvalidGitHubRepoError(INVALID_MESSAGE)
    owner, name = parts
    if is_url and name.lower().endswith(".git"):
        name = name[: -len(".git")]

    if not _is_valid_owner(owner) or not _is_valid_name(name):
        raise InvalidGitHubRepoError(INVALID_MESSAGE)
    return GitHubRepoRef(owner=owner, name=name)


def is_valid_owner_and_name(owner: str, name: str) -> bool:
    """供 GitHub API 回應的二次驗證：回應中的名稱同樣會成為目錄名稱。"""
    return _is_valid_owner(owner) and _is_valid_name(name)


def _is_valid_owner(owner: str) -> bool:
    return _OWNER_RE.fullmatch(owner) is not None and not _is_reserved(owner)


def _is_valid_name(name: str) -> bool:
    if _NAME_RE.fullmatch(name) is None:
        return False
    # 不以 `-` 開頭（避免被當成選項）；不以 `.` 結尾（排除 `.`、`..`；Windows 會悄悄去掉結尾的點，
    # 造成路徑與名稱不一致）；不以 `.git` 結尾（clone URL 由系統自行附加 `.git`）。
    # GitHub 允許以 `.` 開頭的名稱（例如 `.github`），所以不排除；
    # 目錄名稱位於 github\<owner>\ 之下，不會與受管目錄根部的 `.tmp` 衝突。
    if name.startswith("-") or name.endswith(".") or name.lower().endswith(".git"):
        return False
    return not _is_reserved(name)


def _is_reserved(segment: str) -> bool:
    return segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED
