"""結構索引的 job 流程與提交規則（切片 6）：直接呼叫 `job_runner.run_job`。

真實 PostgreSQL＋真實暫存 Git repo；Celery、Redis 與容器由 `stack` 測試驗證（索引階段只讀資料庫，
不碰 Git）。
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from repopilot.db.locks import acquire_advisory_lock, index_lock_key
from repopilot.indexing.profile import IndexProfile
from repopilot.services import job_runner, snapshot_indexing
from repopilot.services import jobs as job_service
from tests.git_helpers import init_repo
from tests.integration.jobs.conftest import (
    WorkerEnv,
    job_state,
    register_repo,
    remove_tree,
    scalar,
    snapshot_row,
    submit_import_job,
)
from tests.integration.snapshot_helpers import add_snapshot

FILES = {
    "README.md": "hello\n",
    "app/models.py": "class Base: ...\nclass User(Base):\n    def name(self): ...\n",
    "app/broken.py": "def oops(:\n",
}


@pytest.fixture
def repo(worker_env: WorkerEnv) -> Path:
    return init_repo(worker_env.roots / "demo", files=FILES)


@pytest.fixture
def repository_id(mapped_client: TestClient, repo: Path) -> int:
    return register_repo(mapped_client, repo)


@pytest.fixture
def worker_settings(worker_env: WorkerEnv) -> Settings:
    return job_runner.apply_worker_timeouts(worker_env.settings)


def _run(job_id: int, engine: Engine, settings: Settings) -> str:
    return str(job_runner.run_job(job_id, engine, settings))


def _import_job(client: TestClient, repository_id: int) -> int:
    return int(submit_import_job(client, repository_id).json()["job"]["id"])


def _index_rows(engine: Engine, snapshot_id: int) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                text("SELECT * FROM snapshot_indexes WHERE snapshot_id = :s ORDER BY id"),
                {"s": snapshot_id},
            )
        ]


def _submit_index(client: TestClient, snapshot_id: int) -> Any:
    return client.post(f"/api/v1/snapshots/{snapshot_id}/index-jobs")


def _imported(
    client: TestClient, engine: Engine, settings: Settings, repository_id: int
) -> tuple[int, int]:
    job_id = _import_job(client, repository_id)
    assert _run(job_id, engine, settings) == "succeeded"
    return job_id, int(job_state(engine, job_id)["snapshot_id"])


# --- 匯入 job 的索引階段 ---


def test_an_import_job_publishes_the_snapshot_then_indexes_it_and_succeeds_together(
    mapped_client: TestClient, repository_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    job_id, snapshot_id = _imported(mapped_client, db_engine, worker_settings, repository_id)

    state = job_state(db_engine, job_id)
    assert (state["status"], state["phase"]) == ("succeeded", "completed")
    (index,) = _index_rows(db_engine, snapshot_id)
    assert index["status"] == "completed" and index["owner_token"] is None
    assert (index["file_count"], index["parse_error_count"]) == (2, 1)  # models.py、broken.py
    result = state["result"]
    assert result["snapshot_id"] == snapshot_id
    assert (result["indexed_files"], result["parse_error_files"]) == (2, 1)
    assert result["symbols"] == index["symbol_count"] == 4  # models.py 的 module、Base、User、name
    assert result["index_reused"] is False
    # 進度：索引階段以 indexed_files／total_files 呈現（total 是可索引的檔案數）
    with db_engine.connect() as connection:
        progress = connection.execute(
            text("SELECT indexed_files, total_files, parse_error_count FROM jobs WHERE id = :i"),
            {"i": job_id},
        ).one()
    assert tuple(progress) == (2, 2, 1)
    assert scalar(db_engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0


def test_a_completed_snapshot_without_an_index_gets_a_job_that_only_indexes(
    mapped_client: TestClient, repository_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    sync = mapped_client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
    assert sync.status_code == 201
    snapshot_id = sync.json()["id"]

    response = submit_import_job(mapped_client, repository_id)

    assert response.status_code == 202  # 規格 4.4：快照 completed、索引缺少 → 建立只含索引的 job
    job_id = response.json()["job"]["id"]
    assert _run(job_id, db_engine, worker_settings) == "succeeded"
    state = job_state(db_engine, job_id)
    assert state["snapshot_id"] == snapshot_id
    assert state["result"]["reused"] is True  # 快照被重用，沒有重新匯入
    assert _index_rows(db_engine, snapshot_id)[0]["status"] == "completed"
    # 兩者都完成之後再提交：200＋reused
    again = submit_import_job(mapped_client, repository_id)
    assert again.status_code == 200
    assert again.json()["reused"] is True and again.json()["snapshot_id"] == snapshot_id


def test_an_indexing_failure_keeps_the_published_snapshot_and_a_retry_completes_the_index(
    mapped_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _import_job(mapped_client, repository_id)
    real = snapshot_indexing._write_batch
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(snapshot_indexing, "_write_batch", flaky)
    outcome = _run(job_id, db_engine, worker_settings)

    assert outcome == "retry_scheduled"  # database_unavailable 是暫時性的
    state = job_state(db_engine, job_id)
    snapshot_id = state["snapshot_id"]
    assert snapshot_id is not None  # 快照已發布：索引失敗不使快照失效
    assert snapshot_row(db_engine, snapshot_id)[0] == "completed"
    (index,) = _index_rows(db_engine, snapshot_id)
    assert index["status"] == "failed" and index["owner_token"] is None
    assert index["error_summary"] == "Indexing failed (OperationalError)."
    assert scalar(db_engine, "SELECT count(*) FROM indexed_files") == 0

    monkeypatch.setattr(snapshot_indexing, "_write_batch", real)
    with db_engine.begin() as connection:  # 退避時間不靠 sleep：直接讓它現在可執行
        connection.execute(
            text("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = :i"),
            {"i": job_id},
        )
    assert _run(job_id, db_engine, worker_settings) == "succeeded"
    (rebuilt,) = _index_rows(db_engine, snapshot_id)
    assert rebuilt["id"] == index["id"] and rebuilt["status"] == "completed"
    assert job_state(db_engine, job_id)["result"]["reused"] is True  # 第二次重用了已發布的快照
    assert calls["n"] == 1


def test_cancelling_during_indexing_keeps_the_snapshot_and_hides_the_index(
    mapped_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _import_job(mapped_client, repository_id)
    real = snapshot_indexing._write_batch

    def cancel_then_write(*args: Any, **kwargs: Any) -> Any:
        written = real(*args, **kwargs)
        mapped_client.post(f"/api/v1/jobs/{job_id}/cancel")  # 索引階段的第一批寫完後使用者取消
        return written

    monkeypatch.setattr(snapshot_indexing, "_write_batch", cancel_then_write)
    outcome = _run(job_id, db_engine, worker_settings)

    assert outcome == "cancelled"
    state = job_state(db_engine, job_id)
    assert state["status"] == "cancelled"
    snapshot_id = state["snapshot_id"]
    assert snapshot_row(db_engine, snapshot_id)[0] == "completed"  # 已發布的快照不被取消
    (index,) = _index_rows(db_engine, snapshot_id)
    assert index["status"] == "failed" and index["error_summary"] == "The job was cancelled."
    assert scalar(db_engine, "SELECT count(*) FROM symbols") == 0
    assert (
        mapped_client.get(f"/api/v1/snapshots/{snapshot_id}/symbols").json()["error"]["code"]
        == "snapshot_index_not_ready"
    )


def test_cancelling_at_the_index_publish_boundary_keeps_the_snapshot_and_never_publishes_the_index(
    mapped_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引的完成交易先看到取消旗標：索引不得發布、job 為 cancelled；快照早已發布，不受影響。"""
    job_id = _import_job(mapped_client, repository_id)
    real = job_service.complete_job

    def cancel_just_before_publish(session: Session, jid: int, token: Any, **kwargs: Any) -> Any:
        mapped_client.post(f"/api/v1/jobs/{jid}/cancel")
        return real(session, jid, token, **kwargs)

    monkeypatch.setattr(job_service, "complete_job", cancel_just_before_publish)
    outcome = _run(job_id, db_engine, worker_settings)

    assert outcome == "cancelled"
    state = job_state(db_engine, job_id)
    assert state["status"] == "cancelled"
    snapshot_id = state["snapshot_id"]
    assert snapshot_row(db_engine, snapshot_id)[0] == "completed"
    (index,) = _index_rows(db_engine, snapshot_id)
    assert index["status"] == "failed" and index["owner_token"] is None
    assert scalar(db_engine, "SELECT count(*) FROM indexed_files") == 0


def test_a_busy_index_lock_defers_the_job_without_using_a_retry(
    mapped_client: TestClient, repository_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    snapshot_id = mapped_client.post(
        f"/api/v1/repositories/{repository_id}/imports", json={}
    ).json()["id"]
    job_id = _submit_index(mapped_client, snapshot_id).json()["job"]["id"]
    # 另一個建構者（例如同一快照的匯入 job）正持有索引鎖
    other_builder = acquire_advisory_lock(
        db_engine, index_lock_key(snapshot_id, IndexProfile.from_settings(worker_settings).hash())
    )
    assert other_builder is not None
    try:
        outcome = _run(job_id, db_engine, worker_settings)
    finally:
        other_builder.release()

    assert outcome == "deferred"
    state = job_state(db_engine, job_id)
    assert (state["status"], state["attempt"]) == ("queued", 0)  # 不消耗重試額度
    assert _index_rows(db_engine, snapshot_id) == []


def test_a_worker_whose_index_settings_differ_fails_with_profile_mismatch(
    mapped_client: TestClient, repository_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    job_id = _import_job(mapped_client, repository_id)
    other = worker_settings.model_copy(update={"index_max_symbols_per_file": 7})

    outcome = _run(job_id, db_engine, other)

    assert outcome == "failed"  # 永久性：不重試
    state = job_state(db_engine, job_id)
    assert state["error_code"] == "profile_mismatch"
    assert scalar(db_engine, "SELECT count(*) FROM snapshots") == 0  # 還沒碰任何資料


def test_each_file_is_recorded_as_the_current_unit_while_it_is_parsed(
    mapped_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _import_job(mapped_client, repository_id)
    seen: list[tuple[str, int | None, int | None]] = []
    real = snapshot_indexing._extract

    def spy(file_id: int, path: str, content: str, settings: Settings) -> Any:
        with db_engine.connect() as connection:
            work_state = connection.execute(
                text("SELECT work_state FROM jobs WHERE id = :i"), {"i": job_id}
            ).scalar_one()
        unit = work_state["current_unit"]
        assert unit["id"] == str(file_id)  # 解析之前，這個檔案已被記為目前單位
        seen.append((unit["kind"], unit["index"], unit["count"]))
        return real(file_id, path, content, settings)

    monkeypatch.setattr(snapshot_indexing, "_extract", spy)
    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    assert seen == [("file", 0, 2), ("file", 1, 2)]


# --- index-jobs 的提交規則 ---


def test_index_job_submission_rules(
    mapped_client: TestClient, repository_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    sync = mapped_client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
    snapshot_id = sync.json()["id"]

    first = _submit_index(mapped_client, snapshot_id)
    assert first.status_code == 202
    job = first.json()["job"]
    assert (job["kind"], job["status"], job["snapshot_id"]) == (
        "snapshot_index",
        "queued",
        snapshot_id,
    )
    assert job["params"] == {"snapshot_id": snapshot_id}
    assert first.headers["Location"] == f"/api/v1/jobs/{job['id']}"
    assert first.json()["repository_id"] == repository_id

    duplicate = _submit_index(mapped_client, snapshot_id)  # 進行中：回既有 job
    assert duplicate.status_code == 202 and duplicate.json()["job"]["id"] == job["id"]

    assert _run(job["id"], db_engine, worker_settings) == "succeeded"
    done = job_state(db_engine, job["id"])
    assert done["phase"] == "completed" and done["result"]["indexed_files"] == 2

    reused = _submit_index(mapped_client, snapshot_id)
    assert reused.status_code == 200
    assert reused.json()["reused"] is True
    assert reused.json()["job"]["id"] == job["id"]  # 最近一次成功的 job


def test_a_failed_or_cancelled_index_job_is_requeued_as_the_same_job(
    mapped_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    snapshot_id = mapped_client.post(
        f"/api/v1/repositories/{repository_id}/imports", json={}
    ).json()["id"]
    job_id = _submit_index(mapped_client, snapshot_id).json()["job"]["id"]

    assert mapped_client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    resubmitted = _submit_index(mapped_client, snapshot_id)

    assert resubmitted.status_code == 202
    assert resubmitted.json()["job"]["id"] == job_id  # 同一個 job，attempt 繼續累加
    assert resubmitted.json()["job"]["status"] == "queued"


def test_index_job_for_a_missing_or_unfinished_snapshot_is_rejected(
    mapped_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    assert _submit_index(mapped_client, 999).status_code == 404
    importing = add_snapshot(db_engine, repository_id, status="importing")
    response = _submit_index(mapped_client, importing)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_not_completed"
    assert scalar(db_engine, "SELECT count(*) FROM jobs") == 0


def test_an_index_job_needs_no_git_access_to_the_repository(
    mapped_client: TestClient,
    repository_id: int,
    repo: Path,
    db_engine: Engine,
    worker_settings: Settings,
) -> None:
    snapshot_id = mapped_client.post(
        f"/api/v1/repositories/{repository_id}/imports", json={}
    ).json()["id"]
    job_id = _submit_index(mapped_client, snapshot_id).json()["job"]["id"]
    remove_tree(repo)  # 索引只讀資料庫裡的快照內容，repo 目錄不在了也能建立

    assert _run(job_id, db_engine, worker_settings) == "succeeded"
    assert _index_rows(db_engine, snapshot_id)[0]["status"] == "completed"


def test_removing_a_repository_with_an_index_cascades_everything(
    mapped_client: TestClient,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
) -> None:
    _imported(mapped_client, db_engine, worker_settings, repository_id)
    assert scalar(db_engine, "SELECT count(*) FROM symbols") > 0

    response = mapped_client.delete(f"/api/v1/repositories/{repository_id}")

    assert response.status_code == 200
    for table in (
        "snapshot_indexes",
        "indexed_files",
        "symbols",
        "import_references",
        "symbol_relations",
        "jobs",
    ):
        assert scalar(db_engine, f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608
