import base64
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from repopilot.errors import (
    GitHubCloneError,
    GitHubCloneTimeoutError,
    GitHubFetchError,
    GitHubFetchTimeoutError,
)
from repopilot.git.remote import (
    _diff_refs,
    build_clone_url,
    clone_repository,
    fetch_origin,
    redact_secrets,
)
from tests.github_helpers import FAKE_TOKEN, capture_logs

BASE = "https://github.com"
B64_TOKEN = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()


class FakeRun:
    """取代 `subprocess.run`：記錄 argv 與環境變數，回傳預先設定的結果。

    `only` 非 None 時，失敗／逾時設定只套用在 argv 含該子指令的呼叫（其他呼叫成功、無輸出）。
    """

    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raise_timeout = False
        self.only: str | None = None

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """取代 `subprocess.run`（唯讀的本機指令，例如 for-each-ref）。"""
        self.calls.append((command, kwargs["env"]))
        returncode, stdout, stderr = self._outcome(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def tree(
        self, command: list[str], *, timeout: float, stdin: bytes | None, env: dict[str, str]
    ) -> tuple[int, bytes, bytes]:
        """取代 `_run_killing_tree`（網路指令 clone／fetch）。"""
        self.calls.append((command, env))
        return self._outcome(command, timeout)

    def _outcome(self, command: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        if self.only is not None and self.only not in command:
            return 0, b"", b""
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(command, timeout)
        return self.returncode, self.stdout, self.stderr.encode()


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr("repopilot.git.runner.subprocess.run", fake)
    monkeypatch.setattr("repopilot.git.runner._run_killing_tree", fake.tree)
    return fake


def _clone(token: str | None, base_url: str = BASE) -> None:
    clone_repository(
        Path("work"),
        Path("work") / "dest",
        base_url=base_url,
        owner="octocat",
        name="Hello-World",
        token=token,
        timeout=5,
    )


# --- 遮蔽函式 ---


def test_redact_masks_known_secrets_in_any_position() -> None:
    text = f"fatal: token {FAKE_TOKEN} and encoded {B64_TOKEN} here"

    masked = redact_secrets(text, [FAKE_TOKEN, B64_TOKEN])

    assert FAKE_TOKEN not in masked
    assert B64_TOKEN not in masked
    assert masked.startswith("fatal: token ***")


def test_redact_masks_authorization_header_values() -> None:
    text = "> Authorization: basic SGVsbG8gV29ybGQ=\nAUTHORIZATION: Bearer abc.def"

    masked = redact_secrets(text)

    assert "SGVsbG8" not in masked
    assert "abc.def" not in masked
    assert "Authorization: ***" in masked


@pytest.mark.parametrize(
    "token",
    ["github_pat_FAKEFAKEFAKE1234567890", "ghp_FAKEFAKEFAKE1234567890", "ghs_FAKEFAKEFAKE123456"],
)
def test_redact_masks_token_shaped_strings_even_when_unknown(token: str) -> None:
    assert token not in redact_secrets(f"remote: invalid credentials {token}.")


def test_redact_masks_longer_secret_first_and_ignores_empty_secrets() -> None:
    masked = redact_secrets("value=abcdef and abc", ["abc", "abcdef", ""])

    assert masked == "value=*** and ***"


def test_redact_leaves_ordinary_text_unchanged() -> None:
    text = "fatal: repository 'https://github.com/o/n.git/' not found"

    assert redact_secrets(text, [FAKE_TOKEN]) == text


# --- clone 指令組成 ---


def test_clone_command_never_contains_the_token(fake_run: FakeRun) -> None:
    _clone(FAKE_TOKEN)

    argv, _ = fake_run.calls[0]
    joined = "\0".join(argv)
    assert FAKE_TOKEN not in joined
    assert B64_TOKEN not in joined
    assert "x-access-token" not in joined
    assert "Authorization" not in joined


def test_clone_command_shape_uses_double_dash_and_system_built_url(fake_run: FakeRun) -> None:
    _clone(None)

    argv, _ = fake_run.calls[0]
    assert argv[:3] == ["git", "-C", "work"]
    separator = argv.index("--")
    assert argv[separator + 1 :] == [
        "https://github.com/octocat/Hello-World.git",
        str(Path("work") / "dest"),
    ]
    for option in ("--no-checkout", "--quiet", "--template="):
        assert argv.index(option) < separator
    assert argv.index("clone") < argv.index("--no-checkout")
    # 完整 clone：沒有 shallow／partial 選項
    assert not any(
        a.startswith(("--depth", "--filter", "--shallow", "--single-branch")) for a in argv
    )
    assert not any(a == "--bare" for a in argv)


def test_clone_disables_credential_helper_and_hooks(fake_run: FakeRun) -> None:
    _clone(None)

    argv, _ = fake_run.calls[0]
    assert "credential.helper=" in argv
    assert f"core.hooksPath={os.devnull}" in argv
    # -c 選項必須在子指令之前才生效
    assert argv.index("credential.helper=") < argv.index("clone")


def test_clone_env_carries_token_as_extraheader_scoped_to_base_url(fake_run: FakeRun) -> None:
    _clone(FAKE_TOKEN)

    _, env = fake_run.calls[0]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: basic {B64_TOKEN}"
    assert base64.b64decode(B64_TOKEN).decode() == f"x-access-token:{FAKE_TOKEN}"


def test_clone_env_has_no_token_settings_without_a_token(fake_run: FakeRun) -> None:
    _clone(None)

    _, env = fake_run.calls[0]
    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env


def test_clone_env_restricts_protocol_prompts_and_user_config(fake_run: FakeRun) -> None:
    _clone(None)

    _, env = fake_run.calls[0]
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GCM_INTERACTIVE"] == "never"


def test_allowed_protocol_follows_the_base_url_scheme_only(fake_run: FakeRun) -> None:
    _clone(None, base_url="file:///tmp/remotes")

    _, env = fake_run.calls[0]
    assert env["GIT_ALLOW_PROTOCOL"] == "file"
    assert fake_run.calls[0][0][-2] == "file:///tmp/remotes/octocat/Hello-World.git"


def test_inherited_git_config_and_askpass_variables_are_removed(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.ssh://evil/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.sshcommand=evil'")
    monkeypatch.setenv("GIT_ASKPASS", "evil-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "evil-askpass")
    monkeypatch.setenv("GIT_DIR", "elsewhere")

    _clone(None)

    _, env = fake_run.calls[0]
    for name in (
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_DIR",
    ):
        assert name not in env


def test_build_clone_url_appends_dot_git_and_normalizes_trailing_slash() -> None:
    assert build_clone_url("https://github.com/", "o", "n") == "https://github.com/o/n.git"


# --- 失敗處理 ---


def test_clone_failure_raises_domain_error_without_stderr_or_token(fake_run: FakeRun) -> None:
    fake_run.returncode = 128
    fake_run.stderr = f"fatal: unable to access: {FAKE_TOKEN} {B64_TOKEN}"

    with capture_logs() as logs, pytest.raises(GitHubCloneError) as exc_info:
        _clone(FAKE_TOKEN)

    assert exc_info.value.code == "github_clone_failed"
    text = logs.text() + exc_info.value.message
    assert FAKE_TOKEN not in text
    assert B64_TOKEN not in text
    # 遮蔽後的 stderr 仍會被記錄，方便排錯
    assert "github_clone_failed" in logs.text()
    assert "unable to access" in logs.text()


def test_clone_timeout_maps_to_clone_timeout_error(fake_run: FakeRun) -> None:
    fake_run.raise_timeout = True

    with pytest.raises(GitHubCloneTimeoutError) as exc_info:
        _clone(FAKE_TOKEN)

    assert exc_info.value.code == "github_clone_timeout"


def test_fetch_failure_maps_to_fetch_error_without_leaking_the_token(fake_run: FakeRun) -> None:
    fake_run.only = "fetch"
    fake_run.returncode = 1
    fake_run.stderr = f"fatal: {FAKE_TOKEN}"

    with capture_logs() as logs, pytest.raises(GitHubFetchError) as exc_info:
        fetch_origin(Path("repo"), base_url=BASE, token=FAKE_TOKEN, timeout=5)

    assert exc_info.value.code == "github_fetch_failed"
    assert FAKE_TOKEN not in logs.text() + exc_info.value.message


def test_fetch_timeout_maps_to_fetch_timeout_error(fake_run: FakeRun) -> None:
    fake_run.only = "fetch"
    fake_run.raise_timeout = True

    with pytest.raises(GitHubFetchTimeoutError) as exc_info:
        fetch_origin(Path("repo"), base_url=BASE, token=None, timeout=5)

    assert exc_info.value.code == "github_fetch_timeout"


def test_fetch_command_uses_origin_prune_and_env_token(fake_run: FakeRun) -> None:
    fetch_origin(Path("repo"), base_url=BASE, token=FAKE_TOKEN, timeout=5)

    fetch_calls = [c for c in fake_run.calls if "fetch" in c[0]]
    argv, env = fetch_calls[0]
    assert argv[argv.index("fetch") :] == [
        "fetch",
        "--prune",
        "--quiet",
        "--no-recurse-submodules",
        "origin",
    ]
    assert FAKE_TOKEN not in "\0".join(argv)
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: basic {B64_TOKEN}"


# --- ref 差異 ---


def test_diff_refs_reports_added_updated_removed_sorted() -> None:
    before = {
        "refs/remotes/origin/main": "a" * 40,
        "refs/remotes/origin/gone": "b" * 40,
        "refs/tags/v1": "c" * 40,
        "refs/remotes/origin/same": "d" * 40,
    }
    after = {
        "refs/remotes/origin/main": "e" * 40,
        "refs/remotes/origin/new": "f" * 40,
        "refs/tags/v1": "c" * 40,
        "refs/tags/v2": "1" * 40,
        "refs/remotes/origin/same": "d" * 40,
    }

    changes = [(c.name, c.kind, c.change) for c in _diff_refs(before, after)]

    assert changes == [
        ("origin/gone", "branch", "removed"),
        ("origin/main", "branch", "updated"),
        ("origin/new", "branch", "added"),
        ("v2", "tag", "added"),
    ]
