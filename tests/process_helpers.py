"""多程序測試的子程序入口（切片 5，D44）：`python -m tests.process_helpers register <repo>`。

為什麼需要真的子程序：D32 的行程內鎖在多個程序（多個 uvicorn worker、API 與 worker 容器）之間不
共用，原本的測試用 `monkeypatch` 關閉行程內鎖來「模擬」多程序。鎖改成 PostgreSQL advisory lock 後，
測試改成真的啟動多個 Python 程序同時註冊同一個 repo，驗證跨程序互斥。

子程序以環境變數取得設定（不在命令列放資料庫連線字串）：`HELPER_DATABASE_URL`、
`HELPER_MANAGED_DIR`、`HELPER_REMOTES_URI`、`HELPER_CANONICAL`（假 GitHub 回的標準名稱）、
`HELPER_START_AT`（epoch 秒，到時間才開始，讓各程序盡量同時出發）。
最後一行標準輸出是 JSON 結果。GitHub API 以 `MockTransport` 模擬。
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.errors import RepoPilotError
from repopilot.git.remote import clone_repository
from repopilot.github.client import GitHubClient
from repopilot.services import github_repositories


def _fake_github(request: httpx.Request) -> httpx.Response:
    # 與真實 GitHub 一樣：不論請求怎麼拼大小寫，都回標準大小寫的名稱
    return httpx.Response(
        200,
        json={
            "full_name": os.environ["HELPER_CANONICAL"],
            "private": False,
            "default_branch": "main",
        },
    )


def register(raw_repo: str) -> dict[str, object]:
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(os.environ["HELPER_DATABASE_URL"]),
        managed_repos_dir=Path(os.environ["HELPER_MANAGED_DIR"]),
        github_clone_base_url=os.environ["HELPER_REMOTES_URI"],
        github_clone_timeout_seconds=120,
    )
    client = GitHubClient(
        base_url="https://api.github.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(_fake_github),
    )
    engine = create_engine(settings.database_url.get_secret_value())
    wait = float(os.environ.get("HELPER_START_AT", "0")) - time.time()
    if wait > 0:
        time.sleep(wait)
    # 這個程序有沒有真的呼叫 git clone（鎖若失效，多個程序會各自 clone）
    clones = {"count": 0}
    real_clone = clone_repository

    def counting_clone(*args: Any, **kwargs: Any) -> None:
        clones["count"] += 1
        real_clone(*args, **kwargs)

    setattr(github_repositories, "clone_repository", counting_clone)  # noqa: B010
    try:
        with Session(engine) as session:
            repository, created = github_repositories.register_github_repository(
                session, raw_repo=raw_repo, settings=settings, client=client
            )
            return {"ok": True, "created": created, "id": repository.id, "clones": clones["count"]}
    except RepoPilotError as exc:
        return {"ok": False, "code": exc.code, "clones": clones["count"]}
    finally:
        engine.dispose()


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "register":
        print(json.dumps(register(argv[2])))  # 子程序的輸出協定：最後一行是 JSON
        return 0
    print("usage: python -m tests.process_helpers register <owner/name>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
