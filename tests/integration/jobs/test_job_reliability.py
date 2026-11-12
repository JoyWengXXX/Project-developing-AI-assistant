"""worker 的可靠性行為（第二階段切片 4，規格 5.7～5.9、11.2，D43）。

涵蓋：取消、重試與退避、毒藥單位、重送。

直接呼叫 `job_runner.run_job`（真實 PostgreSQL＋真實暫存 Git repo，「容器端」就是本機檔案系統，
與 `test_job_runner.py` 相同）。故障以最小的方式注入：
- 「做到一半」的位置是**批次之間**（`read_blobs` 的前後；blob 以分批方式讀取，D43）。
- 暫時性錯誤＝在批次之間拋 `OperationalError`；崩潰＝拋 `BaseException` 子類別（`except Exception`
  攔不到，等於程序直接消失：租約留著、快照留在 importing，由復原掃描處理）。
- 時間一律以 `now` 參數注入或讀資料庫裡的 `next_attempt_at`，不使用 sleep；心跳間隔設為很大的值，
  讓「取消在批次之間被偵測」只可能來自進度回報本身（而不是心跳執行緒碰巧跑到）。
不涉及 Celery、Redis 與容器（那是 `stack` 測試）。
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.errors import GitCommandError
from repopilot.services import job_dispatch, job_runner
from repopilot.services import jobs as job_service
from repopilot.services import snapshots as _snapshots_module
from tests.git_helpers import init_repo
from tests.integration.jobs.conftest import (
    WorkerEnv,
    dispatch_rows,
    job_state,
    register_repo,
    scalar,
    snapshot_row,
    submit_import_job,
)

snapshot_service: Any = _snapshots_module

# 9 個不同的 blob（＋README）＋被排除的 .env：每批 2 個 → 5 批
FILES = {f"src/m{number}.py": f"value = {number}\n" for number in range(9)} | {
    "README.md": "hello\n",
}
ENTRY_COUNT = len(FILES) + 1  # 含 .env（skipped/excluded，不讀取）
BATCHES = 5
NEVER = 3600.0  # 心跳間隔：測試期間心跳執行緒不會跑，偵測只能靠進度回報與檢查點


class WorkerKilled(BaseException):
    """模擬 worker 被 SIGKILL：`except Exception` 攔不到，租約與 importing 快照原樣留下。"""


class DispatcherKilled(BaseException):
    """模擬 dispatcher 在發布之後、標記之前崩潰。"""


def _future(extra_seconds: float = 0) -> datetime:
    return datetime.now(UTC) + timedelta(days=1, seconds=extra_seconds)


@pytest.fixture
def repo(worker_env: WorkerEnv) -> Path:
    return init_repo(worker_env.roots / "demo", files={**FILES, ".env": "SECRET=FAKE\n"})


@pytest.fixture
def repository_id(mapped_client: TestClient, repo: Path) -> int:
    return register_repo(mapped_client, repo)


@pytest.fixture
def job_id(mapped_client: TestClient, repository_id: int) -> int:
    return int(submit_import_job(mapped_client, repository_id).json()["job"]["id"])


@pytest.fixture
def worker_settings(worker_env: WorkerEnv) -> Settings:
    batched = worker_env.settings.model_copy(update={"import_blob_batch_max_files": 2})
    return job_runner.apply_worker_timeouts(batched)


class ReadHook:
    """包住 `read_blobs`：數呼叫次數，並可在每次讀取前／後執行注入的動作。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.real = snapshot_service.read_blobs
        self.count = 0
        self.before: Callable[[int], None] | None = None
        self.after: Callable[[int], None] | None = None
        monkeypatch.setattr(snapshot_service, "read_blobs", self)

    def reset(
        self,
        *,
        before: Callable[[int], None] | None = None,
        after: Callable[[int], None] | None = None,
    ) -> None:
        self.count, self.before, self.after = 0, before, after

    def __call__(self, repo: Path, expected: dict[str, int], *, timeout: float) -> Any:
        self.count += 1
        if self.before is not None:
            self.before(self.count)
        result = self.real(repo, expected, timeout=timeout)
        if self.after is not None:
            self.after(self.count)
        return result


def _run(job_id: int, engine: Engine, settings: Settings) -> str:
    return str(job_runner.run_job(job_id, engine, settings, heartbeat_interval_seconds=NEVER))


def _advisory_locks(engine: Engine) -> int:
    return int(scalar(engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))


def _make_due(engine: Engine, job_id: int) -> None:
    """讓退避時間已到（改資料庫裡的 next_attempt_at；run_job 取得租約用的是資料庫時鐘）。"""
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = :i"),
            {"i": job_id},
        )


def _file_count(engine: Engine) -> int:
    return int(scalar(engine, "SELECT count(*) FROM snapshot_files"))


def _raise_on(call_number: int, error: Callable[[], BaseException]) -> Callable[[int], None]:
    def before(count: int) -> None:
        if count == call_number:
            raise error()

    return before


def _outage() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection reset by FAKE_HOST"))


# --- 取消：在批次之間被偵測，之後不再讀取任何 blob ---


@pytest.mark.parametrize("cancel_after", [1, 2, BATCHES], ids=["first", "second", "last"])
def test_a_cancel_request_is_noticed_between_batches_and_no_further_blob_is_read(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    cancel_after: int,
) -> None:
    hook = ReadHook(monkeypatch)

    def cancel(count: int) -> None:
        if count == cancel_after:
            assert mapped_client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 202

    hook.reset(after=cancel)

    assert _run(job_id, db_engine, worker_settings) == "cancelled"

    # 取消延遲上限＝一批：旗標在讀完第 N 批後的進度回報被偵測，第 N+1 批從未被讀取
    assert hook.count == cancel_after
    state = job_state(db_engine, job_id)
    assert (state["status"], state["snapshot_id"]) == ("cancelled", None)
    body = mapped_client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "cancelled"
    assert body["cancel_requested"] is True and body["cancel_requested_at"] is not None
    assert body["finished_at"] is not None  # 結束時間已設定
    assert body["progress"]["total_files"] == ENTRY_COUNT  # 總數在讀取前就已知
    if cancel_after < BATCHES:
        assert body["progress"]["processed_files"] < ENTRY_COUNT  # 沒有跑完
    # 沒有半套快照：標記 failed（可被後續匯入接手）、沒有檔案列、查詢一律拒絕
    snapshot_id = scalar(db_engine, "SELECT id FROM snapshots")
    assert snapshot_row(db_engine, snapshot_id) == ("failed", 0, "The job was cancelled.")
    assert _file_count(db_engine) == 0
    assert mapped_client.get(f"/api/v1/snapshots/{snapshot_id}/files").status_code == 409
    assert dispatch_rows(db_engine, job_id) == []
    assert _advisory_locks(db_engine) == 0


def test_a_cancel_during_ls_tree_is_noticed_by_the_first_report_without_the_heartbeat(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)
    real_list_tree = snapshot_service.list_tree

    def cancel_during_ls_tree(*args: Any, **kwargs: Any) -> Any:
        entries = real_list_tree(*args, **kwargs)
        assert mapped_client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 202
        return entries

    monkeypatch.setattr(snapshot_service, "list_tree", cancel_during_ls_tree)

    assert _run(job_id, db_engine, worker_settings) == "cancelled"

    assert hook.count == 0  # 一個 blob 都沒讀
    assert job_state(db_engine, job_id)["status"] == "cancelled"
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"
    assert _file_count(db_engine) == 0


def test_a_cancelled_import_can_be_resubmitted_and_completes_by_taking_over_the_snapshot(
    mapped_client: TestClient,
    job_id: int,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)

    def cancel_after_the_second_batch(count: int) -> None:
        if count == 2:
            assert mapped_client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 202

    hook.reset(after=cancel_after_the_second_batch)
    assert _run(job_id, db_engine, worker_settings) == "cancelled"
    cancelled_snapshot = scalar(db_engine, "SELECT id FROM snapshots")
    hook.reset()  # 之後不再取消

    resubmitted = submit_import_job(mapped_client, repository_id)

    assert resubmitted.status_code == 202
    body = resubmitted.json()
    assert body["job"]["id"] == job_id  # 同一個 job（同鍵可重新提交）
    assert (body["job"]["status"], body["job"]["attempt"], body["job"]["max_attempts"]) == (
        "queued",
        1,
        4,
    )
    assert body["job"]["finished_at"] is None and body["job"]["cancel_requested"] is False
    assert dispatch_rows(db_engine, job_id) == [(2, "pending", 0, 0)]

    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    final = job_state(db_engine, job_id)
    assert (final["status"], final["attempt"], final["snapshot_id"]) == (
        "succeeded",
        2,
        cancelled_snapshot,
    )
    assert scalar(db_engine, "SELECT count(*) FROM snapshots") == 1  # 接手 failed 的快照，沒有另建
    assert snapshot_row(db_engine, cancelled_snapshot) == ("completed", len(FILES), None)
    assert _file_count(db_engine) == ENTRY_COUNT
    assert _advisory_locks(db_engine) == 0


# --- 有限重試與退避（真實 worker 路徑） ---


def test_a_database_outage_between_batches_is_retried_with_backoff_and_then_succeeds(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)
    hook.reset(before=_raise_on(2, _outage))  # 第一批讀完、第二批開始前，資料庫連線中斷

    assert _run(job_id, db_engine, worker_settings) == "retry_scheduled"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"], state["attempt"]) == (
        "queued",
        "database_unavailable",
        1,
    )
    assert "FAKE_HOST" not in str(state)  # 例外訊息（可能含連線資訊）不外洩
    assert (state["processed_files"], state["total_files"]) == (0, None)  # 新的嘗試，進度歸零
    delay = scalar(
        db_engine, "SELECT next_attempt_at - updated_at FROM jobs WHERE id = :i", i=job_id
    )
    assert timedelta(seconds=24 - 0.5) <= delay <= timedelta(seconds=36 + 0.5)  # 30 秒 ±20%
    assert dispatch_rows(db_engine, job_id) == [(2, "pending", 0, 0)]
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"  # 未完成的快照不可見
    assert _file_count(db_engine) == 0
    assert _advisory_locks(db_engine) == 0
    failed_snapshot = scalar(db_engine, "SELECT id FROM snapshots")

    hook.reset()  # 資料庫恢復
    assert _run(job_id, db_engine, worker_settings) == "discarded"  # 退避時間還沒到：訊息被丟棄
    _make_due(db_engine, job_id)
    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    final = job_state(db_engine, job_id)
    assert (final["attempt"], final["snapshot_id"], final["error_code"]) == (
        2,
        failed_snapshot,
        None,
    )
    assert _file_count(db_engine) == ENTRY_COUNT


def test_a_git_failure_between_batches_is_permanent_and_is_not_retried(
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)
    hook.reset(before=_raise_on(2, lambda: GitCommandError("git cat-file failed: boom")))

    assert _run(job_id, db_engine, worker_settings) == "failed"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"], state["attempt"]) == (
        "failed",
        "git_command_failed",
        1,
    )
    assert "boom" not in str(state)  # Git 的 stderr 只進日誌
    assert dispatch_rows(db_engine, job_id) == []  # 永久性：沒有下一次的事件
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"
    assert _file_count(db_engine) == 0


def test_a_transient_error_that_never_clears_ends_failed_after_the_last_attempt(
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)
    hook.reset(before=_raise_on(1, _outage))
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET max_attempts = 2 WHERE id = :i"), {"i": job_id})

    assert _run(job_id, db_engine, worker_settings) == "retry_scheduled"
    hook.reset(before=_raise_on(1, _outage))  # 第二次嘗試時資料庫仍然不可用
    _make_due(db_engine, job_id)
    assert _run(job_id, db_engine, worker_settings) == "failed"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"], state["attempt"]) == (
        "failed",
        "database_unavailable",
        2,
    )
    assert dispatch_rows(db_engine, job_id) == []


class _Messages:
    """假的 broker 發布器：只記錄 job id。"""

    def __init__(self) -> None:
        self.ids: list[int] = []

    def publish(self, job_id: int) -> None:
        self.ids.append(job_id)


def test_a_retried_job_is_not_dispatched_or_leased_before_next_attempt_at_but_is_at_it(
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退避的兩道閘門（派送與取得租約）在 `next_attempt_at` 前一微秒關閉、當下開啟（注入時鐘）。"""
    hook = ReadHook(monkeypatch)
    hook.reset(before=_raise_on(1, _outage))
    assert _run(job_id, db_engine, worker_settings) == "retry_scheduled"
    due = job_state(db_engine, job_id)["next_attempt_at"]
    just_before = due - timedelta(microseconds=1)
    publisher = _Messages()

    with Session(db_engine) as session:
        assert job_dispatch.dispatch_pending(session, publisher, now=just_before).sent == 0
        assert job_service.acquire_lease(session, job_id, worker_settings, now=just_before) is None
        assert dispatch_rows(db_engine, job_id) == [(2, "pending", 0, 0)]
        assert job_state(db_engine, job_id)["status"] == "queued"

        assert job_dispatch.dispatch_pending(session, publisher, now=due).sent == 1
        leased = job_service.acquire_lease(session, job_id, worker_settings, now=due)

    assert publisher.ids == [job_id]
    assert leased is not None and leased.attempt == 2


# --- 毒藥單位框架：匯入的行為 ---


def _crash_and_recover(
    db_engine: Engine, worker_settings: Settings, job_id: int
) -> tuple[int, int, int]:
    """worker 崩潰後：租約過期→復原掃描重新排隊（`now` 注入）→ 讓退避時間到。"""
    with Session(db_engine) as session:
        result = job_dispatch.recover_expired_leases(session, worker_settings, now=_future())
    _make_due(db_engine, job_id)
    return result


def test_a_batch_that_crashes_the_worker_is_recorded_and_the_import_still_completes_in_full(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)

    # attempt 1：第二批讀取時 worker 被殺
    hook.reset(before=_raise_on(2, WorkerKilled))
    with pytest.raises(WorkerKilled):
        _run(job_id, db_engine, worker_settings)
    running = mapped_client.get(f"/api/v1/jobs/{job_id}").json()
    assert running["status"] == "running"  # 租約留著，沒有人收尾
    assert running["diagnostics"]["current_unit"] == {"kind": "blob_batch", "index": 1, "count": 5}
    assert scalar(db_engine, "SELECT status FROM snapshots") == "importing"
    assert _advisory_locks(db_engine) == 0  # 持鎖的連線已結束
    assert _crash_and_recover(db_engine, worker_settings, job_id) == (1, 0, 0)
    assert job_state(db_engine, job_id)["error_code"] == "worker_lost"

    # attempt 2：重試同一批，再次崩潰；開始時已結算第一次崩潰
    hook.reset(before=_raise_on(2, WorkerKilled))
    with pytest.raises(WorkerKilled):
        _run(job_id, db_engine, worker_settings)
    second = mapped_client.get(f"/api/v1/jobs/{job_id}").json()
    assert second["attempt"] == 2
    assert second["diagnostics"]["poison"] == {
        "suspect_crashes": 1,
        "skipped_units": 0,
        "total_crashes": 1,
    }
    assert _crash_and_recover(db_engine, worker_settings, job_id) == (1, 0, 0)

    # attempt 3：這次沒有崩潰。開始時結算第二次崩潰（同一批連續兩次）；匯入不可略過任何一批，
    # 所以照常完整匯入，快照完整、job 成功
    hook.reset()
    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    body = mapped_client.get(f"/api/v1/jobs/{job_id}").json()
    assert (body["status"], body["attempt"], body["error"]) == ("succeeded", 3, None)
    assert body["diagnostics"] == {
        "current_unit": None,  # 已結束：不顯示過時的指標
        "poison": {"suspect_crashes": 2, "skipped_units": 0, "total_crashes": 2},
    }
    assert scalar(db_engine, "SELECT count(*) FROM snapshots") == 1
    assert snapshot_row(db_engine, body["snapshot_id"]) == ("completed", len(FILES), None)
    assert _file_count(db_engine) == ENTRY_COUNT  # 一個檔案都沒少


def test_a_crash_at_a_different_batch_restarts_the_suspect_count(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)
    for crash_at in (2, 3):  # 第一次崩潰在第 2 批，第二次在第 3 批：第 2 批顯然撐過去了
        hook.reset(before=_raise_on(crash_at, WorkerKilled))
        with pytest.raises(WorkerKilled):
            _run(job_id, db_engine, worker_settings)
        _crash_and_recover(db_engine, worker_settings, job_id)
    hook.reset()

    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    poison = mapped_client.get(f"/api/v1/jobs/{job_id}").json()["diagnostics"]["poison"]
    assert poison == {"suspect_crashes": 1, "skipped_units": 0, "total_crashes": 2}


def test_a_batch_that_always_crashes_ends_failed_with_worker_lost_and_is_never_resent(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = ReadHook(monkeypatch)
    outcomes = []
    for _attempt in range(3):  # JOB_MAX_ATTEMPTS = 3
        hook.reset(before=_raise_on(2, WorkerKilled))
        with pytest.raises(WorkerKilled):
            _run(job_id, db_engine, worker_settings)
        outcomes.append(_crash_and_recover(db_engine, worker_settings, job_id))

    assert outcomes == [(1, 0, 0), (1, 0, 0), (0, 1, 0)]  # 前兩次重新排隊，第三次達上限 → failed
    body = mapped_client.get(f"/api/v1/jobs/{job_id}").json()
    assert (body["status"], body["attempt"], body["error"]["code"]) == ("failed", 3, "worker_lost")
    assert body["finished_at"] is not None
    assert body["diagnostics"]["poison"] == {
        "suspect_crashes": 2,  # 第三次崩潰沒有下一個 attempt 來結算
        "skipped_units": 0,
        "total_crashes": 2,
    }
    # 不會無限重送：沒有事件、派送器沒東西可送、遲到的訊息被丟棄
    assert dispatch_rows(db_engine, job_id) == []
    publisher = _Messages()
    with Session(db_engine) as session:
        assert job_dispatch.dispatch_pending(session, publisher).sent == 0
        assert job_dispatch.run_recovery_scan(session, worker_settings).requeued == 0
    assert _run(job_id, db_engine, worker_settings) == "discarded"
    assert publisher.ids == []
    # 未完成資料不可見
    assert scalar(db_engine, "SELECT count(*) FROM snapshots WHERE status = 'completed'") == 0
    assert _file_count(db_engine) == 0

    # 使用者手動重試（新額度）：崩潰紀錄跨 attempt 保留；匯入仍然不略過，這次完整成功
    hook.reset()
    assert mapped_client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 202
    assert _run(job_id, db_engine, worker_settings) == "succeeded"
    final = mapped_client.get(f"/api/v1/jobs/{job_id}").json()
    assert final["diagnostics"]["poison"] == {
        "suspect_crashes": 3,
        "skipped_units": 0,
        "total_crashes": 3,
    }
    assert _file_count(db_engine) == ENTRY_COUNT


def test_a_recovered_job_waits_for_its_backoff_before_dispatch_and_lease(
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """復原掃描重新排隊的退避，與一般重試同樣：`next_attempt_at` 前不派送、不取得租約。"""
    hook = ReadHook(monkeypatch)
    hook.reset(before=_raise_on(2, WorkerKilled))
    with pytest.raises(WorkerKilled):
        _run(job_id, db_engine, worker_settings)
    with Session(db_engine) as session:
        assert job_dispatch.recover_expired_leases(session, worker_settings, now=_future()) == (
            1,
            0,
            0,
        )
    due = job_state(db_engine, job_id)["next_attempt_at"]
    publisher = _Messages()

    with Session(db_engine) as session:
        just_before = due - timedelta(microseconds=1)
        assert job_dispatch.dispatch_pending(session, publisher, now=just_before).sent == 0
        assert job_service.acquire_lease(session, job_id, worker_settings, now=just_before) is None
        assert job_dispatch.dispatch_pending(session, publisher, now=due).sent == 1
        assert job_service.acquire_lease(session, job_id, worker_settings, now=due) is not None

    assert publisher.ids == [job_id]


# --- 派送後、標記前崩潰：重送，但只執行一次 ---


class _PublishesThenDies:
    """訊息已交給 broker，dispatcher 在標記 `sent` 之前崩潰（BaseException）。"""

    def __init__(self, messages: _Messages) -> None:
        self.messages = messages

    def publish(self, job_id: int) -> None:
        self.messages.publish(job_id)
        raise DispatcherKilled


def test_a_message_resent_after_a_dispatcher_crash_runs_the_job_exactly_once(
    job_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    broker = _Messages()

    with Session(db_engine) as session, pytest.raises(DispatcherKilled):
        job_dispatch.dispatch_pending(session, _PublishesThenDies(broker))
    # 訊息已經在 broker 裡，但事件沒有被標記（交易回復）：仍是 pending，下一輪會重送
    assert broker.ids == [job_id]
    assert dispatch_rows(db_engine, job_id) == [(1, "pending", 0, 0)]

    with Session(db_engine) as session:
        assert job_dispatch.dispatch_pending(session, broker).sent == 1
    assert broker.ids == [job_id, job_id]  # 同一個 job 兩則訊息
    assert dispatch_rows(db_engine, job_id) == [(1, "sent", 1, 0)]

    outcomes = [_run(message, db_engine, worker_settings) for message in broker.ids]

    assert outcomes == ["succeeded", "discarded"]  # 租約是條件式 UPDATE：只有一個贏家
    assert job_state(db_engine, job_id)["attempt"] == 1
    assert scalar(db_engine, "SELECT count(*) FROM snapshots") == 1
    assert _file_count(db_engine) == ENTRY_COUNT
