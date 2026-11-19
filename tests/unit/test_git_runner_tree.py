import itertools
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from repopilot.git.runner import _run_killing_tree, run_git
from tests.github_helpers import FAKE_TOKEN

# 父程序啟動一個握著輸出管線的孫程序，然後各自睡覺；孫程序若存活，會在 3 秒後寫出標記檔
_PARENT = textwrap.dedent(
    """
    import subprocess, sys, time
    marker = sys.argv[1]
    child = (
        "import pathlib, sys, time; time.sleep(3); "
        "pathlib.Path(sys.argv[1]).write_text('survived')"
    )
    subprocess.Popen([sys.executable, "-c", child, marker])
    time.sleep(60)
    """
)


def test_timeout_kills_the_whole_process_tree_and_returns_promptly(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "grandchild-survived.txt"
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        _run_killing_tree(
            [sys.executable, "-c", _PARENT, str(marker)],
            timeout=1,
            stdin=None,
            env=dict(os.environ),
        )
    elapsed = time.monotonic() - started
    time.sleep(4)  # 若孫程序沒被殺，它會在這段時間內寫出標記檔

    assert elapsed < 20
    assert not marker.exists()


def test_run_killing_tree_returns_output_and_exit_code() -> None:
    code, out, err = _run_killing_tree(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        timeout=30,
        stdin=None,
        env=dict(os.environ),
    )

    assert (code, out.strip(), err.strip()) == (3, b"out", b"err")


def test_run_git_redacts_secrets_before_truncating_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    # 秘密剛好跨過 500 字元的截斷線：先截斷再遮蔽的話，殘片會漏網
    noisy = ("x" * 490 + FAKE_TOKEN).encode()
    monkeypatch.setattr(
        "repopilot.git.runner._run_killing_tree",
        lambda command, *, timeout, stdin, env: (128, b"", noisy),
    )

    result = run_git(
        Path("repo"), ["clone"], timeout=1, kill_process_tree=True, secrets=[FAKE_TOKEN]
    )

    assert "github" not in result.stderr
    assert result.stderr.startswith("x" * 490)


# --- poll：取消與租約的輪詢（第二階段切片 5，D44）---

_SLEEPER = "import time; time.sleep(60)"


def test_poll_is_called_repeatedly_while_the_command_runs_and_not_after_it_ends() -> None:
    calls: list[float] = []
    started = time.monotonic()

    code, out, _ = _run_killing_tree(
        [sys.executable, "-c", "import time; time.sleep(1.2); print('done')"],
        timeout=30,
        stdin=None,
        env=dict(os.environ),
        poll=lambda: calls.append(time.monotonic() - started),
        poll_interval=0.2,
    )

    assert (code, out.strip()) == (0, b"done")
    assert 3 <= len(calls) <= 7  # 約每 0.2 秒一次（約 1.2 秒）
    assert all(b - a >= 0.1 for a, b in itertools.pairwise(calls))
    assert calls[-1] < 1.6  # 指令結束後不再輪詢


def test_a_poll_exception_kills_the_process_tree_and_propagates_unchanged(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "grandchild-survived.txt"

    class StopError(Exception):
        pass

    def poll() -> None:
        raise StopError("cancel")

    started = time.monotonic()
    with pytest.raises(StopError):
        _run_killing_tree(
            [sys.executable, "-c", _PARENT, str(marker)],
            timeout=60,
            stdin=None,
            env=dict(os.environ),
            poll=poll,
            poll_interval=0.2,
        )
    elapsed = time.monotonic() - started
    time.sleep(4)  # 孫程序若沒被殺，會在這段時間內寫出標記檔

    assert elapsed < 20  # 不是等 60 秒的指令結束
    assert not marker.exists()


def test_the_overall_timeout_still_applies_while_polling() -> None:
    polls: list[int] = []

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_killing_tree(
            [sys.executable, "-c", _SLEEPER],
            timeout=1.0,
            stdin=None,
            env=dict(os.environ),
            poll=lambda: polls.append(1),
            poll_interval=0.2,
        )

    assert time.monotonic() - started < 20
    assert polls  # 逾時前有輪詢


def test_output_is_complete_when_polling_with_input() -> None:
    code, out, _ = _run_killing_tree(
        [
            sys.executable,
            "-c",
            "import sys, time; time.sleep(0.6); sys.stdout.write(sys.stdin.read())",
        ],
        timeout=30,
        stdin=b"hello through stdin",
        env=dict(os.environ),
        poll=lambda: None,
        poll_interval=0.1,
    )

    assert (code, out) == (0, b"hello through stdin")


def test_run_git_passes_poll_only_when_given_and_requires_kill_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def fake(command: list[str], **kwargs: object) -> tuple[int, bytes, bytes]:
        seen.append(kwargs)
        return 0, b"", b""

    monkeypatch.setattr("repopilot.git.runner._run_killing_tree", fake)

    run_git(Path("repo"), ["fetch"], timeout=1, kill_process_tree=True)
    poll = lambda: None  # noqa: E731
    run_git(Path("repo"), ["fetch"], timeout=1, kill_process_tree=True, poll=poll, poll_interval=2)

    assert "poll" not in seen[0]
    assert seen[1]["poll"] is poll
    assert seen[1]["poll_interval"] == 2
    with pytest.raises(ValueError, match="kill_process_tree"):
        run_git(Path("repo"), ["fetch"], timeout=1, poll=poll)
