import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.models import GithubCredential
from repopilot.errors import TokenEncryptionNotConfiguredError
from repopilot.github.client import GitHubClient
from repopilot.logging_config import APP_LOGGER_NAME

logger = logging.getLogger(f"{APP_LOGGER_NAME}.services.github_credentials")

TokenType = Literal["fine_grained", "classic", "unknown"]

# 單列資料表的固定主鍵（資料庫以 CHECK id = 1 保證）
_SINGLE_ROW_ID = 1

CLASSIC_WARNING = (
    "Classic personal access tokens grant broad permissions (the repo scope covers read and "
    "write access to every accessible repository). Prefer a fine-grained token limited to "
    "the repositories you need with read-only Contents access."
)
UNUSABLE_WARNING = (
    "The stored token cannot be decrypted (TOKEN_ENCRYPTION_KEY is missing or has changed). "
    "Submit the token again."
)


@dataclass(frozen=True)
class CredentialStatus:
    authenticated: bool
    login: str | None = None
    token_type: TokenType | None = None
    warning: str | None = None


def detect_token_type(token: str) -> TokenType:
    """依前綴猜測 token 類型；只用於提示，**不是**安全邊界（任何人都能偽造前綴）。"""
    if token.startswith("github_pat_"):
        return "fine_grained"
    if token.startswith("ghp_"):
        return "classic"
    return "unknown"


def warning_for(kind: TokenType) -> str | None:
    return CLASSIC_WARNING if kind == "classic" else None


def _fernet(settings: Settings) -> Fernet:
    """依設定建立 Fernet；沒有預設金鑰，未設定或格式無效就拒絕。"""
    raw_key = settings.token_encryption_key.get_secret_value().strip()
    if not raw_key:
        raise TokenEncryptionNotConfiguredError(
            "TOKEN_ENCRYPTION_KEY is not set or is not a valid Fernet key."
        )
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        # 不把金鑰內容放進訊息或日誌
        raise TokenEncryptionNotConfiguredError(
            "TOKEN_ENCRYPTION_KEY is not set or is not a valid Fernet key."
        ) from None


def connect_token(
    session: Session, *, token: str, settings: Settings, client: GitHubClient
) -> CredentialStatus:
    """驗證 token 並加密保存（覆蓋舊的）；驗證失敗時資料庫完全不動。

    順序刻意如此：先確認金鑰可用（否則不該把 token 送出去）→ 呼叫 GitHub 驗證 →
    成功後才以單一 upsert 寫入。
    """
    fernet = _fernet(settings)
    user = client.get_authenticated_user(token)
    token_type = detect_token_type(token)
    ciphertext = fernet.encrypt(token.encode("utf-8")).decode("ascii")

    values = {
        "encrypted_token": ciphertext,
        "login": user.login,
        "token_type": token_type,
        "created_at": datetime.now(UTC),
    }
    # 單一語句的 upsert：併發 PUT 不會產生第二列，也不會出現「刪了舊的、新的還沒寫」的空窗
    statement = insert(GithubCredential).values(id=_SINGLE_ROW_ID, **values)
    session.execute(
        statement.on_conflict_do_update(index_elements=[GithubCredential.id], set_=values)
    )
    session.commit()
    logger.info("github_token_saved", extra={"token_type": token_type})
    return CredentialStatus(
        authenticated=True, login=user.login, token_type=token_type, warning=warning_for(token_type)
    )


def _decrypt(credential: GithubCredential, settings: Settings) -> str | None:
    """解密已保存的 token；金鑰缺失或不符時回 None 並記錄警告（不記錄任何內容）。"""
    try:
        fernet = _fernet(settings)
    except TokenEncryptionNotConfiguredError:
        logger.warning("github_token_unusable", extra={"reason": "key_not_configured"})
        return None
    try:
        return fernet.decrypt(credential.encrypted_token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("github_token_unusable", extra={"reason": "decrypt_failed"})
        return None


def get_status(session: Session, settings: Settings) -> CredentialStatus:
    """目前保存的 token 狀態；不呼叫 GitHub，也永不回傳 token（含密文）。

    已保存但無法解密（金鑰更換或移除）視同未認證，並附提示，避免回 500。
    """
    credential = session.get(GithubCredential, _SINGLE_ROW_ID)
    if credential is None:
        return CredentialStatus(authenticated=False)
    if _decrypt(credential, settings) is None:
        return CredentialStatus(authenticated=False, warning=UNUSABLE_WARNING)
    token_type = _as_token_type(credential.token_type)
    return CredentialStatus(
        authenticated=True,
        login=credential.login,
        token_type=token_type,
        warning=warning_for(token_type),
    )


def _as_token_type(value: str) -> TokenType:
    # 資料庫 CHECK 約束已限制值域；這裡只是讓型別檢查器與執行期一致
    if value in ("fine_grained", "classic"):
        return cast(TokenType, value)
    return "unknown"


def delete_credential(session: Session) -> None:
    """刪除保存的 token；沒有也視為成功（冪等）。不會在 GitHub 端撤銷 token。"""
    session.execute(delete(GithubCredential))
    session.commit()
    logger.info("github_token_deleted")


def get_decrypted_token(session: Session, settings: Settings) -> str | None:
    """供內部（5c-2 的 clone／fetch）取得明文 token；**不得**經 API 回傳或寫入日誌。

    沒有保存、或保存的密文無法解密時回 None（與 `get_status` 同一策略）。
    呼叫端應只在需要的瞬間取用，並避免把值放進 argv、日誌或例外訊息。
    """
    credential = session.get(GithubCredential, _SINGLE_ROW_ID)
    if credential is None:
        return None
    return _decrypt(credential, settings)


def get_required_token(session: Session, settings: Settings) -> str | None:
    """worker 版（D44）：沒有保存 token 回 None（公開 repo 可匿名）；**有保存但無法解密就拒絕**。

    同步端點遇到「金鑰缺失或更換」時退回匿名（D31）；worker 不這樣做：私有 repo 會被誤報成
    404，使用者看不出是設定問題。所以拋 `TokenEncryptionNotConfiguredError`（永久性錯誤，
    不重試；訊息固定，不含金鑰或密文）。
    """
    credential = session.get(GithubCredential, _SINGLE_ROW_ID)
    if credential is None:
        return None
    token = _decrypt(credential, settings)
    if token is None:
        raise TokenEncryptionNotConfiguredError(
            "A GitHub token is saved but cannot be decrypted: set the same "
            "TOKEN_ENCRYPTION_KEY in the worker environment (.env.worker), "
            "or submit the token again."
        )
    return token
