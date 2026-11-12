"""真實堆疊的故障注入（規格 11.2「派送故障」「worker 中斷」「多程序鎖」）。

故障都以真實操作完成：`docker kill`（SIGKILL）、`docker stop`、`docker pause`、清空 Redis；
「worker 做到一半」以資料庫鎖確定性地製造——測試先在**未提交的交易**中寫入同一個快照鍵，
worker 的 `INSERT INTO snapshots` 會卡在唯一索引的鎖上（`pg_stat_activity` 看得到），此時
worker 一定持有租約、repo 共享鎖與快照鎖，而且不會因為匯入太快而錯過。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from repopilot.errors import JobLeaseLostError
from repopilot.services import jobs as job_service
from repopilot.services.import_profile import ImportProfile
from tests.git_helpers import git
from tests.integration.stack.conftest import (
    Stack,
    register,
    submit,
    wait_for_status,
    wait_until,
)

pytestmark = pytest.mark.stack

FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n", "docs/guide.md": "guide\n"}


def _advisory_lock_count(stack: Stack) -> int:
    return int(stack.scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))


def _worker_is_blocked_inserting_the_snapshot(stack: Stack) -> bool:
    return (
        int(
            stack.scalar(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query ILIKE 'INSERT INTO snapshots%'"
            )
        )
        >= 1
    )


@contextmanager
def import_held_midway(stack: Stack, repository_id: int, repo: Path) -> Iterator[Connection]:
    """在未提交的交易中寫入同一個快照鍵：worker 匯入時會卡在 INSERT。

    離開區塊時**回復**交易（worker 若還活著就順利繼續）；需要「留下殘留的 importing 快照」的測試
    自己對回傳的連線 `commit()`。
    """
    profile_hash = ImportProfile.from_settings(stack.api_settings).hash()
    with stack.engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            text(
                "INSERT INTO snapshots (repository_id, commit_sha, import_profile_hash, status, "
                "file_count, skipped_count, total_bytes, started_at) "
                "VALUES (:r, :s, :h, 'importing', 0, 0, 0, now())"
            ),
            {"r": repository_id, "s": git(repo, "rev-parse", "HEAD"), "h": profile_hash},
        )
        try:
            yield connection
        finally:
            if transaction.is_active:
                transaction.rollback()


# --- worker 中斷 ---


def test_a_killed_worker_is_recovered_and_the_new_attempt_takes_over_the_leftover_snapshot(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("kill-worker", FILES)
    repository_id = register(client, repo)

    with import_held_midway(stack, repository_id, repo) as blocker:
        job = submit(client, repository_id)["job"]
        wait_until(lambda: _worker_is_blocked_inserting_the_snapshot(stack), "worker mid-import")
        running = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert running["status"] == "running"
        old_token = stack.scalar("SELECT lease_token FROM jobs WHERE id = :i", i=job["id"])
        assert old_token is not None
        assert _advisory_lock_count(stack) >= 2  # worker 持有 repo 共享鎖與快照鎖

        stack.kill("worker-test")  # SIGKILL：沒有任何清理機會
        # 留下殘留的 importing 快照（started_at 很新：第一階段的規則會讓重試被擋 30 分鐘）
        blocker.commit()

    # 崩潰時持有的 advisory lock 由資料庫釋放（連線結束），不留過期鎖
    wait_until(lambda: _advisory_lock_count(stack) == 0, "the dead worker's locks to be released")
    # 租約過期（12 秒）後，dispatcher 的復原掃描重新排隊並標記 worker_lost
    wait_until(
        lambda: client.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "queued",
        "the recovery scan to requeue the job",
        timeout=60,
    )
    requeued = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert requeued["error"]["code"] == "worker_lost"
    assert requeued["attempt"] == 1
    leftover = int(stack.scalar("SELECT id FROM snapshots"))

    stack.start("worker-test")

    final = wait_for_status(client, job["id"], {"succeeded", "failed"}, timeout=120)
    assert final["status"] == "succeeded"
    assert final["attempt"] == 2
    assert final["error"] is None  # 成功後清除先前的錯誤
    # 持有快照鎖的新 attempt 立即接手殘留的 importing，不是另建、也沒有等 IMPORT_STALE_SECONDS
    assert final["snapshot_id"] == leftover
    assert stack.scalar("SELECT count(*) FROM snapshots") == 1
    assert stack.scalar("SELECT status FROM snapshots") == "completed"
    assert stack.scalar("SELECT count(*) FROM snapshot_files") == len(FILES)
    # 舊令牌不能覆寫新 attempt 的結果（令牌柵欄）
    with Session(stack.engine) as session, pytest.raises(JobLeaseLostError):
        job_service.complete_job(session, job["id"], old_token, result={"forged": True})
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["result"] != {"forged": True}


def test_a_worker_that_exhausts_its_attempts_is_marked_worker_lost(
    stack: Stack, client: TestClient
) -> None:
    """每次都被殺：達到 JOB_MAX_ATTEMPTS 後明確 failed（worker_lost），不永遠卡住。"""
    repo = stack.new_repo("always-killed", FILES)
    repository_id = register(client, repo)

    with import_held_midway(stack, repository_id, repo):
        job = submit(client, repository_id)["job"]
        with stack.engine.begin() as connection:  # 只允許這一次嘗試
            connection.execute(
                text("UPDATE jobs SET max_attempts = 1 WHERE id = :i"), {"i": job["id"]}
            )
        wait_until(lambda: _worker_is_blocked_inserting_the_snapshot(stack), "worker mid-import")
        stack.kill("worker-test")

    final = wait_for_status(client, job["id"], {"failed"}, timeout=90)
    assert final["error"]["code"] == "worker_lost"
    assert final["attempt"] == 1


# --- 多程序鎖（跨容器） ---


def test_a_repo_removal_and_the_sync_endpoint_are_refused_while_the_container_imports(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("locks", FILES)
    repository_id = register(client, repo)

    with import_held_midway(stack, repository_id, repo):
        job = submit(client, repository_id)["job"]
        wait_until(lambda: _worker_is_blocked_inserting_the_snapshot(stack), "worker mid-import")

        removal = client.delete(f"/api/v1/repositories/{repository_id}")
        sync = client.post(f"/api/v1/repositories/{repository_id}/imports", json={})

        assert (removal.status_code, removal.json()["error"]["code"]) == (409, "repository_busy")
        assert sync.status_code == 409  # 容器內的 worker 持有快照鎖（或快照正 importing）
    # 區塊結束回復了阻塞交易：worker 繼續並完成
    final = wait_for_status(client, job["id"], {"succeeded", "failed"}, timeout=120)
    assert final["status"] == "succeeded"
    assert client.delete(f"/api/v1/repositories/{repository_id}").status_code == 200
    assert _advisory_lock_count(stack) == 0


# --- 派送故障 ---


def test_a_dispatcher_that_is_down_leaves_the_event_pending_until_it_returns(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("dispatcher-down", FILES)
    repository_id = register(client, repo)
    stack.kill("dispatcher-test")

    job = submit(client, repository_id)["job"]  # API 照常接受（不連 Redis）

    assert job["status"] == "queued"
    status = client.get("/api/v1/jobs/status").json()
    assert status["queued"] == 1
    wait_until(
        lambda: client.get("/api/v1/jobs/status").json()["dispatcher"]["status"] != "online",
        "the dead dispatcher to be reported offline",
        timeout=30,
    )
    assert stack.scalar("SELECT status FROM job_dispatches WHERE job_id = :i", i=job["id"]) == (
        "pending"
    )
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "queued"

    stack.start("dispatcher-test")

    final = wait_for_status(client, job["id"], {"succeeded", "failed"}, timeout=120)
    assert final["status"] == "succeeded"
    assert final["attempt"] == 1


def test_a_dispatched_message_lost_from_redis_is_resent(stack: Stack, client: TestClient) -> None:
    repo = stack.new_repo("lost-message", FILES)
    repository_id = register(client, repo)
    stack.stop("worker-test")
    job = submit(client, repository_id)["job"]
    wait_until(
        lambda: (
            stack.scalar("SELECT status FROM job_dispatches WHERE job_id = :i", i=job["id"])
            == "sent"
        ),
        "the dispatch",
    )

    stack.flush_redis()  # 訊息遺失：事件已 sent，但 Redis 裡什麼都沒有
    stack.start("worker-test")

    # DISPATCH_RESEND_AFTER_SECONDS（測試堆疊 15 秒）後，復原掃描重設為 pending，dispatcher 重送
    final = wait_for_status(client, job["id"], {"succeeded", "failed"}, timeout=120)
    assert final["status"] == "succeeded"
    assert final["attempt"] == 1  # 只執行一次，沒有重複發布
    assert stack.scalar("SELECT count(*) FROM snapshots") == 1


def test_a_paused_broker_keeps_the_job_queued_and_pending_until_it_returns(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("broker-paused", FILES)
    repository_id = register(client, repo)
    stack.pause("redis-test")
    try:
        job = submit(client, repository_id)["job"]  # API 不連 Redis：照常 202

        assert job["status"] == "queued"
        # dispatcher 的發布因 socket 逾時失敗；事件保持 pending 並記錄失敗，job 保持 queued
        wait_until(
            lambda: (
                int(
                    stack.scalar(
                        "SELECT failure_count FROM job_dispatches WHERE job_id = :i", i=job["id"]
                    )
                    or 0
                )
                >= 1
            ),
            "the dispatcher to record a failed publish",
            timeout=60,
        )
        assert (
            stack.scalar("SELECT status FROM job_dispatches WHERE job_id = :i", i=job["id"])
            == "pending"
        )
        assert client.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "queued"
    finally:
        stack.unpause("redis-test")

    final = wait_for_status(client, job["id"], {"succeeded", "failed"}, timeout=120)
    assert final["status"] == "succeeded"
    assert stack.scalar("SELECT count(*) FROM snapshots") == 1
