"""遮蔽錯誤輸出中的秘密（GitHub token 與其 base64 形式）。

純函式、不做 I/O；git 的 stderr 在截斷、記錄或放入例外之前都先經過這裡。
"""

import re
from collections.abc import Collection

MASK = "***"
# 即使不知道確切的 token，也遮蔽常見的 GitHub token 前綴與 Authorization 標頭值
_TOKEN_PATTERN = re.compile(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{6,}")
_AUTH_HEADER_PATTERN = re.compile(r"(?i)(authorization:\s*)(?:basic|bearer)?\s*\S+")


def redact_secrets(text: str, secrets: Collection[str] = ()) -> str:
    """遮蔽文字中的秘密：已知的秘密字串、Authorization 標頭值，以及常見的 token 前綴。"""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, MASK)
    text = _AUTH_HEADER_PATTERN.sub(rf"\1{MASK}", text)
    return _TOKEN_PATTERN.sub(MASK, text)
