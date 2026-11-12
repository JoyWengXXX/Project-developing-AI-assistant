"""測試堆疊（`docker-compose.yml` 的 `jobs-test` profile）的控制輔助（第二階段切片 3，規格 9.4）。

為什麼需要它：整合測試的 session fixture 會 `DROP SCHEMA public CASCADE` 重建資料庫（D16）。如果此時
`worker-test`／`dispatcher-test` 還在跑，它們的連線會指向被清空的 schema，而且**它們會去處理測試用
假 job**（例如單元整合測試建立的 `queued` job 與待派送事件，被真正的 worker 執行）。所以：

1. `db_engine`（所有整合測試共用）在重建 schema 前呼叫 `stop_job_services_if_running()`：
   只要這兩個容器在跑就先停止。沒有 Docker 或容器沒在跑時什麼都不做。
2. 只有 `stack` 測試（`tests/integration/stack/`）會在**整個 package 期間**啟動它們，結束後再停止
   （package 範圍的 fixture），所以一般整合測試執行時它們一定是停止的。

compose 的環境變數 `WORKER_TEST_*_HOST_DIR` 是專案內 `.test-repos`、`.test-managed` 的絕對路徑
（正斜線），由這裡設定；手動操作測試堆疊時需自行設定（見 README）。
"""

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_REPOS_DIR = PROJECT_ROOT / ".test-repos"
TEST_MANAGED_DIR = PROJECT_ROOT / ".test-managed"
JOB_SERVICES = ("worker-test", "dispatcher-test")
COMPOSE_BASE = ["docker", "compose", "--profile", "jobs-test"]


def stack_env() -> dict[str, str]:
    env = dict(os.environ)
    env["WORKER_TEST_REPOS_HOST_DIR"] = TEST_REPOS_DIR.as_posix()
    env["WORKER_TEST_MANAGED_HOST_DIR"] = TEST_MANAGED_DIR.as_posix()
    return env


def compose(
    *args: str, timeout: float = 240, check: bool = True
) -> "subprocess.CompletedProcess[str]":
    """在專案根目錄執行 `docker compose --profile jobs-test <args>`。"""
    return subprocess.run(  # noqa: S603
        [*COMPOSE_BASE, *args],
        cwd=PROJECT_ROOT,
        env=stack_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def stop_job_services_if_running() -> None:
    """worker-test／dispatcher-test 在跑就停止（重建 schema 前）；沒有 Docker 或沒在跑時無動作。"""
    try:
        running = compose("ps", "--status", "running", "-q", *JOB_SERVICES, timeout=60, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if running.returncode == 0 and running.stdout.strip():
        compose("stop", *JOB_SERVICES, timeout=120, check=False)
