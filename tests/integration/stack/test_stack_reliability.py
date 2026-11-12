"""真實堆疊的可靠性故障注入（第二階段切片 4，規格 11.2「取消競態」「重試與退避」「worker 中斷」）。

補充 `test_stack_faults.py`（殺 worker、派送故障、多程序鎖）沒有涵蓋的三件事：
1. 取消**執行中**的匯入（真實 worker 容器）：旗標被偵測、收尾、同一份工作可重新提交；
2. 暫時性失敗走完整條路：資料庫連線被終止 → 退避 → 重試成功（故障由測試以 `pg_terminate_backend`
   注入，worker 程式裡沒有任何測試鉤子或環境變數）；
3. 租約過期的舊 worker「復活」：容器被凍結（`docker pause`）超過租約，醒來後它的寫入被令牌柵欄拒絕。
「做到一半」同樣以未提交的交易讓 worker 卡在 `INSERT INTO snapshots`
（見 test_stack_faults 的說明）。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from repopilot.errors import JobLeaseLostError
from repopilot.services import jobs as job_service
from tests.integration.stack.conftest import (
    Stack,
    register,
    submit,
    wait_for_status,
    wait_until,
)
from tests.integration.stack.test_stack_faults import (
    FILES,
    _advisory_lock_count,
    _worker_is_blocked_inserting_the_snapshot,
    import_held_midway,
)

pytestmark = pytest.mark.stack


def _job(client: TestClient, job_id: int) -> dict[str, Any]:
    body: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}").json()
    return body


def _rescheduled_after_a_failure(client: TestClient, job_id: int) -> dict[str, Any] | None:
    """job 已回到 queued 且保留著上一次失敗的錯誤（退避中）時回傳它，否則 None。"""
    body = _job(client, job_id)
    return body if body["status"] == "queued" and body["error"] is not None else None


def test_cancelling_a_running_import_stops_it_cleanly_and_the_work_can_be_resubmitted(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("cancel-running", FILES)
    repository_id = register(client, repo)

    with import_held_midway(stack, repository_id, repo):
        job = submit(client, repository_id)["job"]
        wait_until(lambda: _worker_is_blocked_inserting_the_snapshot(stack), "worker mid-import")

        cancel = client.post(f"/api/v1/jobs/{job['id']}/cancel")

        assert cancel.status_code == 202
        assert cancel.headers["Location"] == f"/api/v1/jobs/{job['id']}"
        assert cancel.json()["cancel_requested"] is True
        assert _job(client, job["id"])["status"] == "running"  # 合作式：要等 worker 的檢查點
    # 阻塞交易回復，worker 繼續；在第一個進度回報（ls-tree 完成後）發現旗標並收尾

    final = wait_for_status(client, job["id"], {"cancelled", "succeeded", "failed"}, timeout=90)

    assert final["status"] == "cancelled"
    assert final["finished_at"] is not None
    assert final["cancel_requested_at"] is not None
    assert final["snapshot_id"] is None
    assert final["progress"]["total_files"] == len(FILES)  # 總數在讀取前就已知
    # 沒有半套快照：failed（可被後續匯入接手），沒有檔案列，查詢一律 409
    snapshot_id = int(stack.scalar("SELECT id FROM snapshots"))
    assert stack.scalar("SELECT status FROM snapshots") == "failed"
    assert stack.scalar("SELECT count(*) FROM snapshot_files") == 0
    assert client.get(f"/api/v1/snapshots/{snapshot_id}/files").status_code == 409
    wait_until(lambda: _advisory_lock_count(stack) == 0, "the worker's locks to be released")
    assert stack.scalar("SELECT count(*) FROM job_dispatches") == 0

    # 取消後同鍵可重新提交：同一個 job，worker 接手失敗的快照並完成
    resubmitted = client.post(f"/api/v1/repositories/{repository_id}/import-jobs", json={})
    assert resubmitted.status_code == 202
    assert resubmitted.json()["job"]["id"] == job["id"]
    again = wait_for_status(client, job["id"], {"succeeded", "failed", "cancelled"}, timeout=120)
    assert again["status"] == "succeeded"
    assert (again["attempt"], again["snapshot_id"]) == (2, snapshot_id)
    assert stack.scalar("SELECT count(*) FROM snapshots") == 1
    assert stack.scalar("SELECT status FROM snapshots") == "completed"
    assert stack.scalar("SELECT count(*) FROM snapshot_files") == len(FILES)


def test_a_dropped_database_connection_is_retried_after_a_backoff_and_then_succeeds(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("db-dropped", FILES)
    repository_id = register(client, repo)

    with import_held_midway(stack, repository_id, repo):
        job = submit(client, repository_id)["job"]
        wait_until(lambda: _worker_is_blocked_inserting_the_snapshot(stack), "worker mid-import")
        # 讓退避期間的狀態可以被確定性地觀察：dispatcher 停止後，重新排隊的事件不會被送出
        stack.stop("dispatcher-test")
        blocked_pid = stack.scalar(
            "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query ILIKE 'INSERT INTO snapshots%'"
        )
        assert stack.scalar("SELECT pg_terminate_backend(:p)", p=blocked_pid) is True  # 連線中斷

        retrying = wait_until(
            lambda: _rescheduled_after_a_failure(client, job["id"]),
            "the job to be rescheduled after the dropped connection",
        )
        assert retrying["error"]["code"] == "database_unavailable"
        assert retrying["attempt"] == 1
        assert retrying["next_attempt_at"] is not None  # 退避：到期前不會被重新執行
        assert retrying["finished_at"] is None
        dispatch = stack.scalar("SELECT status FROM job_dispatches WHERE job_id = :i", i=job["id"])
        assert dispatch == "pending"
        assert stack.scalar("SELECT count(*) FROM snapshot_files") == 0
        assert stack.scalar("SELECT status FROM snapshots") != "completed"
    # 阻塞交易回復（資料庫「恢復」）；dispatcher 回來後在退避時間到時送出，第二次嘗試成功
    stack.start("dispatcher-test")

    final = wait_for_status(client, job["id"], {"succeeded", "failed", "cancelled"}, timeout=120)

    assert final["status"] == "succeeded"
    assert final["attempt"] == 2
    assert final["error"] is None
    assert stack.scalar("SELECT count(*) FROM snapshots") == 1
    assert stack.scalar("SELECT status FROM snapshots") == "completed"
    assert stack.scalar("SELECT count(*) FROM snapshot_files") == len(FILES)
    wait_until(lambda: _advisory_lock_count(stack) == 0, "the worker's locks to be released")


def test_a_worker_frozen_past_its_lease_cannot_write_anything_after_it_wakes_up(
    stack: Stack, client: TestClient
) -> None:
    repo = stack.new_repo("frozen-worker", FILES)
    repository_id = register(client, repo)

    with import_held_midway(stack, repository_id, repo):
        job = submit(client, repository_id)["job"]
        wait_until(lambda: _worker_is_blocked_inserting_the_snapshot(stack), "worker mid-import")
        old_token = stack.scalar("SELECT lease_token FROM jobs WHERE id = :i", i=job["id"])
        assert old_token is not None

        stack.pause("worker-test")  # 凍結整個容器：心跳也停了，租約會過期
        try:
            wait_until(
                lambda: _job(client, job["id"])["status"] == "queued",
                "the recovery scan to requeue the frozen worker's job",
                timeout=60,
            )
            requeued = _job(client, job["id"])
            assert requeued["error"]["code"] == "worker_lost"
        finally:
            stack.unpause("worker-test")
    # 舊 worker 醒來、阻塞交易回復：它繼續到第一個進度回報，發現自己已不是擁有者（令牌不符），
    # 不寫任何結果就結束；新的 attempt 接手失敗的快照並完成

    final = wait_for_status(client, job["id"], {"succeeded", "failed", "cancelled"}, timeout=120)

    assert final["status"] == "succeeded"
    assert final["attempt"] == 2  # 沒有被舊 worker 的寫入搞亂（attempt 不多算、結果不被覆寫）
    assert final["error"] is None
    assert stack.scalar("SELECT count(*) FROM snapshots") == 1
    assert stack.scalar("SELECT status FROM snapshots") == "completed"
    assert stack.scalar("SELECT count(*) FROM snapshot_files") == len(FILES)
    assert '"job_lease_lost"' in stack.logs("worker-test")  # 舊 worker 確實試過寫入，並被拒絕
    # 舊令牌仍然不能發布結果
    with Session(stack.engine) as session, pytest.raises(JobLeaseLostError):
        job_service.complete_job(session, job["id"], old_token, result={"forged": True})
    assert _job(client, job["id"])["result"] != {"forged": True}
    wait_until(lambda: _advisory_lock_count(stack) == 0, "all locks to be released")
