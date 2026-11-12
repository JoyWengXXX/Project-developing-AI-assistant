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
