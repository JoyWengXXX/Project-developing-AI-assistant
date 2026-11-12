import logging

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from repopilot.api.schemas import GitHubTokenSubmit
from repopilot.config import Settings
from repopilot.errors import TokenEncryptionNotConfiguredError
from repopilot.logging_config import configure_logging
from repopilot.services.github_credentials import (
    CLASSIC_WARNING,
    _fernet,
    detect_token_type,
    warning_for,
)
from tests.github_helpers import FAKE_CLASSIC_TOKEN, FAKE_TOKEN


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("github_pat_TESTONLY_abc", "fine_grained"),
        ("ghp_TESTONLYabc", "classic"),
        ("gho_TESTONLYabc", "unknown"),
        ("ghs_TESTONLYabc", "unknown"),
        ("TESTONLY-no-prefix", "unknown"),
        # 前綴區分大小寫；比對只看開頭
        ("GITHUB_PAT_TESTONLY", "unknown"),
        ("xx_github_pat_TESTONLY", "unknown"),
        ("ghp", "unknown"),
    ],
)
def test_detect_token_type_by_prefix(token: str, expected: str) -> None:
    assert detect_token_type(token) == expected


def test_only_classic_tokens_get_a_warning() -> None:
    assert warning_for("classic") == CLASSIC_WARNING
    assert warning_for("fine_grained") is None
    assert warning_for("unknown") is None


def test_token_input_is_stripped() -> None:
    assert GitHubTokenSubmit(token=f"  {FAKE_TOKEN}\r\n").token == FAKE_TOKEN


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "\n",
        "has space inside",
        "line1\nline2",
        "tab\tinside",
        "nul\x00inside",
        "全形字元token",
        "tok\u00e9n",
        "a" * 256,
    ],
    ids=[
        "empty",
        "spaces",
        "newline",
        "inner-space",
        "inner-newline",
        "tab",
        "nul",
        "cjk",
        "latin1",
        "too-long",
    ],
)
def test_invalid_token_input_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        GitHubTokenSubmit(token=value)


def test_token_at_max_length_is_accepted() -> None:
    assert len(GitHubTokenSubmit(token="a" * 255).token) == 255


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        GitHubTokenSubmit.model_validate({"token": FAKE_TOKEN, "login": "x"})


def test_fernet_requires_a_key() -> None:
    with pytest.raises(TokenEncryptionNotConfiguredError):
        _fernet(Settings(token_encryption_key=SecretStr("")))


@pytest.mark.parametrize("key", ["not-a-fernet-key", "   ", "短", "a" * 44, "x" * 10])
def test_fernet_rejects_invalid_key_without_echoing_it(key: str) -> None:
    with pytest.raises(TokenEncryptionNotConfiguredError) as exc_info:
        _fernet(Settings(token_encryption_key=SecretStr(key)))

    assert key.strip() == "" or key not in exc_info.value.message


def test_fernet_accepts_generated_key() -> None:
    key = Fernet.generate_key().decode()

    fernet = _fernet(Settings(token_encryption_key=SecretStr(key)))

    assert fernet.decrypt(fernet.encrypt(b"x")) == b"x"


def test_settings_default_has_no_encryption_key_and_hides_it_in_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    key = Fernet.generate_key().decode()
    settings = Settings(token_encryption_key=SecretStr(key))

    assert Settings(_env_file=None).token_encryption_key.get_secret_value() == ""
    assert key not in repr(settings)
    assert key not in str(settings.model_dump())


def test_github_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_API_BASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_API_TIMEOUT_SECONDS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.github_api_base_url == "https://api.github.com"
    assert settings.github_api_timeout_seconds == 10


def test_configure_logging_keeps_httpx_loggers_at_warning_or_above() -> None:
    names = ("httpx", "httpcore")
    previous = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names:
            logging.getLogger(name).setLevel(logging.DEBUG)

        configure_logging("DEBUG")

        for name in names:
            assert logging.getLogger(name).level >= logging.WARNING
    finally:
        for name in names:
            logging.getLogger(name).setLevel(previous[name])


def test_classic_fake_token_is_detected_as_classic() -> None:
    # 確認測試用的假 token 前綴符合其用途，避免整合測試的前提悄悄失效
    assert detect_token_type(FAKE_CLASSIC_TOKEN) == "classic"
    assert detect_token_type(FAKE_TOKEN) == "fine_grained"
