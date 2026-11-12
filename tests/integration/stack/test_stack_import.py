"""真實堆疊的匯入流程（規格 11.2「背景匯入」「固定版本」「重複提交／重複投遞」）。

PostgreSQL（db-test）＋Redis（redis-test）＋dispatcher-test＋worker-test 容器；API 在主機內
（TestClient）。fixture repo 在 `.test-repos\\`，以唯讀掛載給 worker 容器，路徑經
`WORKER_PATH_MAPPINGS` 映射。
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.git_helpers import commit_files, git, link_directory
from tests.integration.stack.conftest import (
    Stack,
    register,
    submit,
    wait_for_status,
    wait_until,
)

pytestmark = pytest.mark.stack

FILES = {
    "README.md": "# Demo\nhello\n",
    "src/app.py": "print('hi')\n",
    "src/util/helpers.py": "def f():\n    return 1\n",
    "docs/含 空白/說明.md": "中文內容\n",
    "data/blob.bin": "text-but-listed\n",
}


def _snapshot_files(stack: Stack, snapshot_id: int) -> dict[str, str | None]:
    with stack.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT path, content FROM snapshot_files WHERE snapshot_id = :i "
                "AND status = 'imported'"
            ),
            {"i": snapshot_id},
        )
        return {row[0]: row[1] for row in rows}


def _dispatch_status(stack: Stack, job_id: int) -> str | None:
    value = stack.scalar("SELECT status FROM job_dispatches WHERE job_id = :i", i=job_id)
    return None if value is None else str(value)


def test_jobs_status_shows_the_worker_and_dispatcher_online(
    stack: Stack, client: TestClient
) -> None:
    payload = client.get("/api/v1/jobs/status").json()

    assert payload["dispatcher"]["status"] == "online"
    assert payload["worker"]["status"] == "online"
    assert payload["dispatcher"]["instances_online"] >= 1
    assert payload["worker"]["instances_online"] >= 1
    assert payload["heartbeat_timeout_seconds"] == 9


def test_a_job_is_accepted_while_the_worker_is_blocked_and_completes_when_released(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("blocked-then-released", FILES)
    repository_id = register(client, repo)
    stack.stop("worker-test")  # 阻擋 worker：訊息只會留在 Redis

    accepted = submit(client, repository_id, revision="HEAD")

    job = accepted["job"]
    assert (job["status"], job["attempt"]) == ("queued", 0)  # 立即取得 job ID，不等匯入
    wait_until(lambda: _dispatch_status(stack, job["id"]) == "sent", "the event to be dispatched")
    still_waiting = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert (still_waiting["status"], still_waiting["attempt"]) == ("queued", 0)

    stack.start("worker-test")  # 釋放
    processed: list[int] = []

    def watch() -> bool:
        current = client.get(f"/api/v1/jobs/{job['id']}").json()
        processed.append(current["progress"]["processed_files"])
        return bool(current["status"] == "succeeded")

    wait_until(watch, "the job to succeed", timeout=120, interval=0.1)

    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert processed == sorted(processed)  # 進度單調不倒退
    assert final["phase"] == "completed"
    assert final["progress"]["processed_files"] == final["progress"]["total_files"] == len(FILES)
    assert final["attempt"] == 1
    # 快照 completed、檔案內容與主機端 Git 一致（比對全部少量檔案與計數）
    snapshot_id = final["snapshot_id"]
    assert snapshot_id is not None
    snapshot = client.get(f"/api/v1/snapshots/{snapshot_id}").json()
    assert snapshot["status"] == "completed"
    assert snapshot["commit_sha"] == git(repo, "rev-parse", "HEAD")
    assert snapshot["file_count"] == len(FILES)
    files = _snapshot_files(stack, snapshot_id)
    assert set(files) == set(FILES)
    for path, expected in FILES.items():
        assert files[path] == expected == git(repo, "show", f"HEAD:{path}") + "\n"
    assert client.get(f"/api/v1/snapshots/{snapshot_id}/files").json()["total"] == len(FILES)


def test_the_worker_imports_the_commit_pinned_at_submission_not_the_moved_head(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("pinned", {"version.txt": "v1\n", "keep.txt": "keep\n"})
    repository_id = register(client, repo)
    first_sha = git(repo, "rev-parse", "HEAD")
    stack.stop("worker-test")

    job = submit(client, repository_id, revision="HEAD")["job"]
    # 提交後：HEAD 前進、已追蹤檔案被修改、出現未追蹤檔案
    second_sha = commit_files(repo, {"version.txt": "v2\n"})
    (repo / "version.txt").write_text("dirty working tree\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    assert first_sha != second_sha
    stack.start("worker-test")

    final = wait_for_status(client, job["id"], {"succeeded", "failed"}, timeout=120)

    assert final["status"] == "succeeded"
    assert final["params"]["commit_sha"] == first_sha
    snapshot = client.get(f"/api/v1/snapshots/{final['snapshot_id']}").json()
    assert snapshot["commit_sha"] == first_sha
    files = _snapshot_files(stack, final["snapshot_id"])
    assert files == {"version.txt": "v1\n", "keep.txt": "keep\n"}  # 沒有髒內容、沒有未追蹤檔案


def test_duplicate_submissions_and_duplicate_messages_yield_one_snapshot(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("duplicates", FILES)
    repository_id = register(client, repo)
    stack.stop("worker-test")

    ids = {submit(client, repository_id)["job"]["id"] for _ in range(3)}
    assert len(ids) == 1  # 重複提交：同一個 job
    job_id = ids.pop()
    wait_until(lambda: _dispatch_status(stack, job_id) == "sent", "the first dispatch")
    # 製造重複訊息：把事件重設為 pending，dispatcher 會再送一次（共 3 則訊息在 Redis）
    for expected_sent in (2, 3):
        with stack.engine.begin() as connection:
            connection.execute(
                text("UPDATE job_dispatches SET status = 'pending' WHERE job_id = :i"),
                {"i": job_id},
            )

        def sent_exactly(expected: int = expected_sent) -> bool:
            count = stack.scalar(
                "SELECT send_count FROM job_dispatches WHERE job_id = :i AND status = 'sent'",
                i=job_id,
            )
            return bool(count == expected)

        wait_until(sent_exactly, f"dispatch number {expected_sent}")

    stack.start("worker-test")

    final = wait_for_status(client, job_id, {"succeeded", "failed"}, timeout=120)
    assert final["status"] == "succeeded"
    assert final["attempt"] == 1  # 只執行了一次
    assert stack.scalar("SELECT count(*) FROM jobs") == 1
    assert stack.scalar("SELECT count(*) FROM snapshots") == 1
    assert stack.scalar("SELECT count(*) FROM snapshots WHERE status = 'completed'") == 1
    # 重複訊息被租約丟棄（worker 日誌），不是各做一遍
    wait_until(
        lambda: stack.logs("worker-test").count("job_message_discarded") >= 2,
        "the duplicate messages to be discarded",
    )


def test_a_completed_snapshot_is_reused_by_the_next_submission(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("reuse", FILES)
    repository_id = register(client, repo)
    first = submit(client, repository_id)["job"]
    wait_for_status(client, first["id"], {"succeeded"}, timeout=120)

    response = client.post(f"/api/v1/repositories/{repository_id}/import-jobs", json={})

    assert response.status_code == 200
    assert response.json()["reused"] is True
    assert response.json()["job"]["id"] == first["id"]
    assert stack.scalar("SELECT count(*) FROM jobs") == 1


def test_a_directory_replaced_by_a_junction_is_rejected_inside_the_container(
    stack: Stack, client: TestClient
) -> None:
    """repo 註冊後被換成連結：主機端（D28）仍通過，容器端看到懸空符號連結（fail-closed）。

    切片 1 實測：junction 在容器內是目標被改寫成 `/mnt/host/d/…` 的符號連結，`resolve(strict=True)`
    失敗；一律拒絕，永久性錯誤，不重試。
    """
    real = stack.new_repo("junction-link", FILES)
    repository_id = register(client, real)
    moved = stack.repos_dir / "junction-target"
    if moved.exists():
        pytest.fail("fixture 目錄殘留")
    os.rename(real, moved)
    link_directory(real, moved)

    job = submit(client, repository_id)["job"]

    final = wait_for_status(client, job["id"], {"succeeded", "failed"}, timeout=90)
    assert final["status"] == "failed"
    assert final["error"]["code"] == "invalid_repository_path"
    assert final["attempt"] == 1  # 永久性：不重試
    assert str(real) not in final["error"]["message"]
    assert stack.scalar("SELECT count(*) FROM snapshots") == 0
