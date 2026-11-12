"""worker 的任務流程（規格 5.5）：直接呼叫 `job_runner.run_job`。

真實 PostgreSQL＋真實暫存 Git repo。
「容器端」就是本機檔案系統（`worker_env` 把映射的 host 與 container 指到同一個 pytest 暫存目錄），
所以路徑映射、容器端重驗、鎖、租約、進度、完成交易都是真的在執行；Celery、Redis 與真正的容器由
`stack` 測試驗證。故障以真實操作或最小的 monkeypatch 注入（Git 逾時、完成交易途中的資料庫錯誤）。
"""

import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.db.locks import acquire_advisory_lock, repo_lock_key, snapshot_lock_key
from repopilot.errors import GitTimeoutError, ImportCancelledError, JobLeaseLostError
from repopilot.main import create_app
from repopilot.request_context import request_id_var
from repopilot.services import job_dispatch, job_runner
from repopilot.services import jobs as job_service
from repopilot.services import snapshots as _snapshots_module
from repopilot.services.import_profile import ImportProfile
from tests.git_helpers import git, init_repo, link_directory
from tests.integration.jobs.conftest import (
    WorkerEnv,
    job_state,
    register_repo,
    remove_tree,
    scalar,
    snapshot_row,
    submit_import_job,
)

# 以 Any 取用：測試要替換 `list_tree`（snapshots 從 git.objects 匯入的名稱），
# mypy 不認為它是匯出屬性
snapshot_service: Any = _snapshots_module

FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n", "docs/guide.md": "guide\n"}


def _future(extra_seconds: float = 0) -> datetime:
    """比任何現在的租約都晚的時間：讓復原掃描認為租約已過期（`now` 注入，不靠 sleep）。"""
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
    return job_runner.apply_worker_timeouts(worker_env.settings)


def _run(job_id: int, engine: Engine, settings: Settings, **kwargs: Any) -> str:
    return str(job_runner.run_job(job_id, engine, settings, **kwargs))


def _advisory_locks(engine: Engine) -> int:
    return int(scalar(engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))


def _file_paths(engine: Engine, snapshot_id: int) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT path FROM snapshot_files WHERE snapshot_id = :i"), {"i": snapshot_id}
        )
        return {row[0] for row in rows}


def _count(engine: Engine, table: str) -> int:
    return int(scalar(engine, f"SELECT count(*) FROM {table}"))  # noqa: S608 - 固定表名


def _repo_key(repository_id: int) -> str:
    return f"local:{repository_id}"


# --- 成功 ---


def test_a_job_imports_the_pinned_commit_and_the_snapshot_and_job_complete_together(
    job_id: int, repository_id: int, repo: Path, db_engine: Engine, worker_settings: Settings
) -> None:
    outcome = _run(job_id, db_engine, worker_settings)

    assert outcome == "succeeded"
    state = job_state(db_engine, job_id)
    assert (state["status"], state["phase"], state["attempt"]) == ("succeeded", "completed", 1)
    snapshot_id = state["snapshot_id"]
    assert snapshot_id is not None
    assert snapshot_row(db_engine, snapshot_id) == ("completed", 3, None)  # .env 被排除（skipped）
    assert _file_paths(db_engine, snapshot_id) == {*FILES, ".env"}
    with db_engine.connect() as connection:
        row = connection.execute(
            text("SELECT commit_sha, repository_id FROM snapshots WHERE id = :i"),
            {"i": snapshot_id},
        ).one()
        env_row = connection.execute(
            text(
                "SELECT status, skip_reason, content FROM snapshot_files "
                "WHERE snapshot_id = :i AND path = '.env'"
            ),
            {"i": snapshot_id},
        ).one()
    assert row == (git(repo, "rev-parse", "HEAD"), repository_id)
    assert tuple(env_row) == ("skipped", "excluded", None)  # 秘密檔不讀內容
    assert state["result"] == {"snapshot_id": snapshot_id, "commit_sha": row[0]}
    assert state["lease_token"] is None
    # 進度：總數已知且全部處理完成
    assert (state["processed_files"], state["total_files"]) == (4, 4)
    # 沒有殘留：待派送事件、advisory lock 都已清除
    assert _count(db_engine, "job_dispatches") == 0
    assert _advisory_locks(db_engine) == 0


def test_imported_content_matches_git_byte_for_byte(
    job_id: int, repo: Path, db_engine: Engine, worker_settings: Settings
) -> None:
    _run(job_id, db_engine, worker_settings)

    snapshot_id = job_state(db_engine, job_id)["snapshot_id"]
    with db_engine.connect() as connection:
        rows: dict[str, str] = dict(
            connection.execute(
                text(
                    "SELECT path, content FROM snapshot_files WHERE snapshot_id = :i "
                    "AND status = 'imported'"
                ),
                {"i": snapshot_id},
            ).all()
        )
    assert rows == {name: git(repo, "show", f"HEAD:{name}") + "\n" for name in FILES}


def test_a_duplicate_message_after_success_is_discarded_without_side_effects(
    job_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    assert _run(job_id, db_engine, worker_settings) == "succeeded"
    before = job_state(db_engine, job_id)

    assert _run(job_id, db_engine, worker_settings) == "discarded"

    assert job_state(db_engine, job_id) == before  # attempt 沒有增加
    assert _count(db_engine, "snapshots") == 1


def test_a_message_for_a_job_that_does_not_exist_is_discarded(
    db_engine: Engine, worker_settings: Settings, clean_jobs: None
) -> None:
    assert _run(424242, db_engine, worker_settings) == "discarded"


def test_a_message_for_a_cancelled_job_is_discarded(
    mapped_client: TestClient, job_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    mapped_client.post(f"/api/v1/jobs/{job_id}/cancel")

    assert _run(job_id, db_engine, worker_settings) == "discarded"

    assert job_state(db_engine, job_id)["status"] == "cancelled"
    assert _count(db_engine, "snapshots") == 0


def test_a_message_for_a_job_someone_else_is_running_is_discarded(
    job_id: int, db_engine: Engine, worker_env: WorkerEnv, worker_settings: Settings
) -> None:
    with Session(db_engine) as session:
        owner = job_service.acquire_lease(session, job_id, worker_env.settings)
    assert owner is not None

    assert _run(job_id, db_engine, worker_settings) == "discarded"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["attempt"]) == ("running", 1)
    assert state["lease_token"] == owner.lease_token  # 原擁有者的租約沒被碰


def test_concurrent_duplicate_messages_have_exactly_one_winner(
    job_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    barrier = threading.Barrier(4)
    outcomes: list[str] = []

    def deliver() -> None:
        barrier.wait(timeout=30)
        outcomes.append(_run(job_id, db_engine, worker_settings))

    threads = [threading.Thread(target=deliver) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert sorted(outcomes) == ["discarded", "discarded", "discarded", "succeeded"]
    assert _count(db_engine, "snapshots") == 1
    assert job_state(db_engine, job_id)["attempt"] == 1


def test_the_request_id_of_the_submission_is_bound_while_running_and_restored_after(
    job_id: int, db_engine: Engine, worker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []
    real = snapshot_service.import_snapshot

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(request_id_var.get())
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "import_snapshot", spy)

    _run(job_id, db_engine, worker_settings)

    assert scalar(db_engine, "SELECT request_id FROM jobs WHERE id = :i", i=job_id) == seen[0]
    assert seen[0] is not None
    assert request_id_var.get() is None


def test_progress_is_reported_monotonically_with_the_total_known_early(
    job_id: int, db_engine: Engine, worker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports: list[tuple[int | None, int | None]] = []
    real = job_service.update_progress

    def spy(session: Session, jid: int, token: Any, **kwargs: Any) -> Any:
        if "processed_files" in kwargs:
            reports.append((kwargs["processed_files"], kwargs["total_files"]))
        return real(session, jid, token, **kwargs)

    monkeypatch.setattr(job_service, "update_progress", spy)

    _run(job_id, db_engine, worker_settings)

    assert reports[0] == (0, 4)  # ls-tree 完成後立刻知道總數，尚未處理任何檔案
    processed = [p for p, _ in reports if p is not None]
    assert processed == sorted(processed)
    assert reports[-1] == (4, 4)


# --- 路徑與設定的重驗（容器端） ---


def test_an_unmapped_path_fails_permanently_with_path_not_mapped(
    job_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    no_mappings = worker_settings.model_copy(update={"worker_path_mappings": []})

    assert _run(job_id, db_engine, no_mappings) == "failed"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"], state["attempt"]) == (
        "failed",
        "path_not_mapped",
        1,
    )
    assert _count(db_engine, "snapshots") == 0
    assert _advisory_locks(db_engine) == 0


def test_a_path_that_no_longer_exists_in_the_container_fails_permanently(
    job_id: int, repo: Path, db_engine: Engine, worker_settings: Settings
) -> None:
    remove_tree(repo)

    assert _run(job_id, db_engine, worker_settings) == "failed"

    state = job_state(db_engine, job_id)
    assert state["error_code"] == "invalid_repository_path"
    assert str(repo) not in (state["error_summary"] or "")  # 摘要不含路徑


def test_a_path_that_leaves_the_container_roots_is_rejected(
    job_id: int, worker_env: WorkerEnv, db_engine: Engine, worker_settings: Settings, tmp_path: Path
) -> None:
    """映射存在，但容器端允許的根（REPOSITORY_ROOTS）不包含它：容器端重驗拒絕。"""
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    narrowed = worker_settings.model_copy(update={"repository_roots": [other_root.resolve()]})

    assert _run(job_id, db_engine, narrowed) == "failed"

    assert job_state(db_engine, job_id)["error_code"] == "invalid_repository_path"


def test_a_junction_or_symlink_in_the_path_is_never_followed(
    job_id: int, repo: Path, worker_env: WorkerEnv, db_engine: Engine, worker_settings: Settings
) -> None:
    """repo 目錄在註冊後被換成連結：容器端一律拒絕（不跟隨）。

    連結的目標仍在允許的根之內（所以不是被「根檢查」擋下的），被拒絕的原因就是 `resolve()` 的結果
    與映射後的路徑不同——也就是路徑中有一段是連結。
    """
    import shutil

    real_target = worker_env.roots / "real-target"
    shutil.move(str(repo), str(real_target))
    link_directory(repo, real_target)

    assert _run(job_id, db_engine, worker_settings) == "failed"

    state = job_state(db_engine, job_id)
    assert state["error_code"] == "invalid_repository_path"
    assert _count(db_engine, "snapshots") == 0


def test_a_profile_mismatch_between_api_and_worker_fails_permanently(
    job_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    drifted = worker_settings.model_copy(update={"import_max_files": 5})

    assert _run(job_id, db_engine, drifted) == "failed"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"]) == ("failed", "profile_mismatch")
    assert state["attempt"] == 1  # 永久性：不重試
    assert _count(db_engine, "snapshots") == 0


def test_a_job_without_a_valid_commit_sha_fails_permanently(
    job_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            text('UPDATE jobs SET params = \'{"commit_sha": "HEAD"}\' WHERE id = :i'), {"i": job_id}
        )

    assert _run(job_id, db_engine, worker_settings) == "failed"

    assert job_state(db_engine, job_id)["error_code"] == "invalid_revision"


def test_only_import_jobs_are_supported(
    db_engine: Engine, worker_settings: Settings, clean_jobs: None
) -> None:
    with Session(db_engine) as session:
        created = job_service.create_job(
            session, kind="github_fetch", dedupe_key="fetch:x", settings=worker_settings
        )

    assert _run(created.job.id, db_engine, worker_settings) == "failed"

    assert job_state(db_engine, created.job.id)["error_code"] == "unsupported_job_kind"


# --- 鎖忙碌：延後，不消耗額度 ---


def test_a_busy_repo_lock_defers_the_job_without_consuming_an_attempt(
    job_id: int, repository_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    exclusive = acquire_advisory_lock(db_engine, repo_lock_key(_repo_key(repository_id)))
    assert exclusive is not None  # 例如 repo 移除或（切片 5）clone／fetch 持有獨占鎖
    try:
        assert _run(job_id, db_engine, worker_settings) == "deferred"
    finally:
        exclusive.release()

    state = job_state(db_engine, job_id)
    assert (state["status"], state["attempt"], state["phase"]) == ("queued", 0, "pending")
    assert state["next_attempt_at"] is not None  # 延後 5～15 秒
    assert scalar(db_engine, "SELECT next_attempt_at > now() FROM jobs WHERE id = :i", i=job_id)
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches WHERE job_id = :i", i=job_id) == 1
    assert _count(db_engine, "snapshots") == 0


def test_a_busy_snapshot_lock_defers_and_releases_the_repo_lock(
    job_id: int,
    repository_id: int,
    repo: Path,
    db_engine: Engine,
    worker_env: WorkerEnv,
    worker_settings: Settings,
) -> None:
    key = snapshot_lock_key(
        repository_id,
        git(repo, "rev-parse", "HEAD"),
        ImportProfile.from_settings(worker_env.settings).hash(),
    )
    held = acquire_advisory_lock(db_engine, key)  # 例如同步端點正在匯入同一快照
    assert held is not None
    try:
        assert _run(job_id, db_engine, worker_settings) == "deferred"
        # worker 已放掉它取得的 repo 共享鎖：可以取得獨占鎖
        exclusive = acquire_advisory_lock(db_engine, repo_lock_key(_repo_key(repository_id)))
        assert exclusive is not None
        exclusive.release()
    finally:
        held.release()

    state = job_state(db_engine, job_id)
    assert (state["status"], state["attempt"]) == ("queued", 0)
    assert _count(db_engine, "snapshots") == 0


def test_a_deferred_job_runs_to_completion_once_the_lock_is_free(
    job_id: int, repository_id: int, db_engine: Engine, worker_settings: Settings
) -> None:
    held = acquire_advisory_lock(db_engine, repo_lock_key(_repo_key(repository_id)))
    assert held is not None
    assert _run(job_id, db_engine, worker_settings) == "deferred"
    held.release()
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = :i"),
            {"i": job_id},
        )

    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    assert job_state(db_engine, job_id)["attempt"] == 1  # 延後那次沒有消耗額度


def test_the_sync_endpoint_is_refused_while_a_worker_holds_the_snapshot_lock(
    mapped_client: TestClient,
    job_id: int,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 匯入進行中（持有快照鎖），同步端點得到 409，而不是第二份匯入。"""
    statuses: list[int] = []
    real = snapshot_service.list_tree

    def during_import(*args: Any, **kwargs: Any) -> Any:
        statuses.append(
            mapped_client.post(f"/api/v1/repositories/{repository_id}/imports", json={}).status_code
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "list_tree", during_import)

    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    assert statuses == [409]
    assert _count(db_engine, "snapshots") == 1


@pytest.mark.parametrize(
    "hook",
    [
        # 快照列還不存在（worker 已持有 repo 共享鎖、正在解析 commit）：
        # **只有 advisory lock** 擋得住移除
        "resolve_commit",
        # 快照列已存在且 importing：第一階段 D33 的 importing 檢查與 advisory lock 都會擋
        "list_tree",
    ],
)
def test_removing_the_repository_is_refused_while_a_worker_imports_it(
    mapped_client: TestClient,
    job_id: int,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    hook: str,
) -> None:
    removals: list[tuple[int, str]] = []
    real = getattr(snapshot_service, hook)

    def during_import(*args: Any, **kwargs: Any) -> Any:
        response = mapped_client.delete(f"/api/v1/repositories/{repository_id}")
        removals.append((response.status_code, response.json()["error"]["code"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, hook, during_import)

    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    assert removals == [(409, "repository_busy")]
    assert (
        mapped_client.delete(f"/api/v1/repositories/{repository_id}").status_code == 200
    )  # 結束後可移除


# --- takeover：持鎖時立即接手殘留的 importing ---


def _leftover_snapshot(
    engine: Engine, repository_id: int, repo: Path, settings: Settings, *, status: str
) -> int:
    with engine.begin() as connection:
        snapshot_id = connection.execute(
            text(
                "INSERT INTO snapshots (repository_id, commit_sha, import_profile_hash, status, "
                "file_count, skipped_count, total_bytes, started_at) "
                "VALUES (:r, :s, :h, :st, 0, 0, 0, now()) RETURNING id"
            ),
            {
                "r": repository_id,
                "s": git(repo, "rev-parse", "HEAD"),
                "h": ImportProfile.from_settings(settings).hash(),
                "st": status,
            },
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO snapshot_files (snapshot_id, path, status, content) "
                "VALUES (:i, 'leftover.txt', 'imported', 'stale')"
            ),
            {"i": snapshot_id},
        )
    return int(snapshot_id)


@pytest.mark.parametrize("status", ["importing", "failed"])
def test_a_leftover_snapshot_is_taken_over_immediately_and_cleaned(
    job_id: int,
    repository_id: int,
    repo: Path,
    db_engine: Engine,
    worker_env: WorkerEnv,
    worker_settings: Settings,
    status: str,
) -> None:
    """worker 崩潰後的快照：即使 started_at 很新，持有快照鎖的 job 也立即接手。

    「很新」＝遠小於 IMPORT_STALE_SECONDS；第一階段的規則會讓重試被擋最多 30 分鐘。
    """
    leftover = _leftover_snapshot(
        db_engine, repository_id, repo, worker_env.settings, status=status
    )

    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    state = job_state(db_engine, job_id)
    assert state["snapshot_id"] == leftover  # 接手同一個快照列，不是另建
    assert snapshot_row(db_engine, leftover)[0] == "completed"
    assert "leftover.txt" not in _file_paths(db_engine, leftover)  # 殘留檔案列被清掉
    assert _count(db_engine, "snapshots") == 1


def test_an_already_completed_snapshot_is_reused_and_the_job_still_completes(
    mapped_client: TestClient,
    job_id: int,
    repository_id: int,
    db_engine: Engine,
    worker_settings: Settings,
) -> None:
    """完成快照後、設 succeeded 前崩潰的情境：新 attempt 看到 completed 快照，補上 job 的完成。"""
    sync = mapped_client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
    assert sync.status_code == 201

    assert _run(job_id, db_engine, worker_settings) == "succeeded"

    state = job_state(db_engine, job_id)
    assert state["snapshot_id"] == sync.json()["id"]
    assert state["result"]["reused"] is True
    assert _count(db_engine, "snapshots") == 1


# --- 完成交易的一致性 ---


def test_a_failure_inside_the_completion_transaction_rolls_back_snapshot_and_job_together(
    job_id: int, db_engine: Engine, worker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = snapshot_service._write_completion

    def write_then_lose_the_connection(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)  # 快照 completed 與檔案列都已寫入（尚未提交）
        raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

    monkeypatch.setattr(snapshot_service, "_write_completion", write_then_lose_the_connection)

    assert _run(job_id, db_engine, worker_settings) == "retry_scheduled"

    state = job_state(db_engine, job_id)
    # job 沒有 succeeded；快照沒有 completed、沒有任何檔案列：兩者一起回復
    assert (state["status"], state["error_code"]) == ("queued", "database_unavailable")
    assert _count(db_engine, "snapshot_files") == 0
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"
    assert scalar(db_engine, "SELECT count(*) FROM snapshots WHERE status = 'completed'") == 0


def test_the_snapshot_is_never_completed_without_its_job_being_published(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """發布邊界的取消：complete_job 先看到旗標，快照不得被完成，也不會發布成功。"""
    real = job_service.complete_job

    def cancel_just_before_publish(session: Session, jid: int, token: Any, **kwargs: Any) -> Any:
        mapped_client.post(f"/api/v1/jobs/{jid}/cancel")  # 取消請求在完成交易開始前到達
        return real(session, jid, token, **kwargs)

    monkeypatch.setattr(job_service, "complete_job", cancel_just_before_publish)

    assert _run(job_id, db_engine, worker_settings) == "cancelled"

    state = job_state(db_engine, job_id)
    assert state["status"] == "cancelled"
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"  # 可被後續匯入接手
    assert scalar(db_engine, "SELECT error_summary FROM snapshots") == "The import was cancelled."
    assert _count(db_engine, "snapshot_files") == 0
    assert _advisory_locks(db_engine) == 0


# --- 取消與租約遺失的檢查點 ---


class _CheckingHeartbeat(job_runner.LeaseHeartbeat):
    """測試用：檢查點先同步做一次真正的心跳（與執行緒做的事相同），讓測試不依賴計時。"""

    def checkpoint(self) -> None:
        try:
            with self._session_factory() as session:
                if job_service.heartbeat(session, self._job_id, self._token, self._settings):
                    self._cancel_requested.set()
        except Exception:
            self._lease_lost.set()
        super().checkpoint()


def test_a_cancel_request_stops_the_import_at_a_checkpoint_and_the_snapshot_stays_invisible(
    mapped_client: TestClient,
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_runner, "LeaseHeartbeat", _CheckingHeartbeat)
    real = snapshot_service.list_tree

    def cancel_during_ls_tree(*args: Any, **kwargs: Any) -> Any:
        entries = real(*args, **kwargs)
        assert mapped_client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 202
        return entries

    monkeypatch.setattr(snapshot_service, "list_tree", cancel_during_ls_tree)

    assert _run(job_id, db_engine, worker_settings) == "cancelled"

    state = job_state(db_engine, job_id)
    assert state["status"] == "cancelled"
    assert state["snapshot_id"] is None
    # 快照標記 failed（不是 completed），所以任何查詢都不會採用它
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"
    assert _count(db_engine, "snapshot_files") == 0
    snapshot_id = scalar(db_engine, "SELECT id FROM snapshots")
    assert mapped_client.get(f"/api/v1/snapshots/{snapshot_id}/files").status_code == 409
    assert _advisory_locks(db_engine) == 0


def test_losing_the_lease_mid_run_stops_the_worker_without_writing_any_result(
    job_id: int,
    db_engine: Engine,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_runner, "LeaseHeartbeat", _CheckingHeartbeat)
    real = snapshot_service.list_tree
    new_owner: dict[str, Any] = {}

    def lease_is_taken_by_the_recovery_scan(*args: Any, **kwargs: Any) -> Any:
        entries = real(*args, **kwargs)
        with Session(db_engine) as session:
            # 租約過期 → 復原掃描重新排隊 → 另一個 worker 取得新令牌（時間以注入的 now 推進）
            job_dispatch.recover_expired_leases(session, worker_settings, now=_future())
            taken = job_service.acquire_lease(session, job_id, worker_settings, now=_future(10_000))
        assert taken is not None
        new_owner["token"] = taken.lease_token
        return entries

    monkeypatch.setattr(snapshot_service, "list_tree", lease_is_taken_by_the_recovery_scan)
    blob_reads: list[int] = []
    real_read_blobs = snapshot_service.read_blobs

    def spy_read_blobs(*args: Any, **kwargs: Any) -> Any:
        blob_reads.append(1)
        return real_read_blobs(*args, **kwargs)

    monkeypatch.setattr(snapshot_service, "read_blobs", spy_read_blobs)

    assert _run(job_id, db_engine, worker_settings) == "lease_lost"

    # 在下一個檢查點就中止：失去租約之後不再讀取任何 blob（不做白工，也不寫任何結果）
    assert blob_reads == []
    state = job_state(db_engine, job_id)
    # 現在的擁有者是新 worker；舊 worker 沒有寫入完成、失敗或進度
    assert (state["status"], state["attempt"]) == ("running", 2)
    assert state["lease_token"] == new_owner["token"]
    assert state["snapshot_id"] is None
    assert state["error_code"] == "worker_lost"  # 來自復原掃描，不是舊 worker
    assert scalar(db_engine, "SELECT count(*) FROM snapshots WHERE status = 'completed'") == 0
    assert _count(db_engine, "snapshot_files") == 0
    assert _advisory_locks(db_engine) == 0


# --- 錯誤分類與重試 ---


def test_a_git_timeout_is_retried_with_a_backoff_and_hints_at_git_gc(
    job_id: int, db_engine: Engine, worker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(*_: Any, **__: Any) -> Any:
        raise GitTimeoutError("Git command timed out.")

    real = snapshot_service.list_tree
    monkeypatch.setattr(snapshot_service, "list_tree", timed_out)

    assert _run(job_id, db_engine, worker_settings) == "retry_scheduled"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"], state["attempt"]) == ("queued", "git_timeout", 1)
    assert "git gc" in state["error_summary"]
    assert scalar(db_engine, "SELECT next_attempt_at > now() FROM jobs WHERE id = :i", i=job_id)
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"  # 未完成的快照不可見
    assert _advisory_locks(db_engine) == 0

    # 退避時間到、Git 恢復正常：同一個 job 第二次嘗試成功，並接手失敗的快照
    monkeypatch.setattr(snapshot_service, "list_tree", real)
    failed_snapshot = scalar(db_engine, "SELECT id FROM snapshots")
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET next_attempt_at = now() - interval '1 second' WHERE id = :i"),
            {"i": job_id},
        )
    assert _run(job_id, db_engine, worker_settings) == "succeeded"
    final = job_state(db_engine, job_id)
    assert (final["attempt"], final["snapshot_id"], final["error_code"]) == (
        2,
        failed_snapshot,
        None,
    )


def test_a_transient_error_stops_retrying_when_the_attempts_are_exhausted(
    job_id: int, db_engine: Engine, worker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(*_: Any, **__: Any) -> Any:
        raise GitTimeoutError("Git command timed out.")

    monkeypatch.setattr(snapshot_service, "list_tree", timed_out)
    with db_engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET max_attempts = 1 WHERE id = :i"), {"i": job_id})

    assert _run(job_id, db_engine, worker_settings) == "failed"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"]) == ("failed", "git_timeout")


def test_an_import_limit_is_a_permanent_failure_that_is_not_retried(
    worker_env: WorkerEnv, repository_id: int, db_engine: Engine, clean_jobs: None
) -> None:
    small = worker_env.settings.model_copy(update={"import_max_files": 1})
    with TestClient(create_app(small)) as client:
        job = int(submit_import_job(client, repository_id).json()["job"]["id"])

    assert _run(job, db_engine, job_runner.apply_worker_timeouts(small)) == "failed"

    state = job_state(db_engine, job)
    assert (state["status"], state["error_code"], state["attempt"]) == (
        "failed",
        "import_limit_exceeded",
        1,
    )
    assert scalar(db_engine, "SELECT status FROM snapshots") == "failed"


def test_an_unexpected_exception_is_permanent_and_leaks_nothing(
    job_id: int, db_engine: Engine, worker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("password=FAKE_SECRET_FOR_TEST at /mnt/repos/demo")

    monkeypatch.setattr(snapshot_service, "list_tree", broken)

    assert _run(job_id, db_engine, worker_settings) == "failed"

    state = job_state(db_engine, job_id)
    assert (state["status"], state["error_code"]) == ("failed", "internal_error")
    assert state["error_summary"] == "Unexpected RuntimeError."
    assert "FAKE_SECRET_FOR_TEST" not in str(state)
    assert "FAKE_SECRET_FOR_TEST" not in (
        scalar(db_engine, "SELECT error_summary FROM snapshots") or ""
    )


# --- 租約心跳執行緒 ---


@pytest.fixture
def leased_job(
    db_engine: Engine, worker_settings: Settings, mapped_client: TestClient, job_id: int
) -> Iterator[tuple[int, Any, sessionmaker[Session]]]:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        job = job_service.acquire_lease(session, job_id, worker_settings)
    assert job is not None
    yield job_id, job.lease_token, factory


def _wait_for(predicate: Callable[[], bool], what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_the_heartbeat_thread_extends_the_lease_and_reports_cancellation(
    leased_job: tuple[int, Any, sessionmaker[Session]],
    db_engine: Engine,
    worker_settings: Settings,
    mapped_client: TestClient,
) -> None:
    job_id, token, factory = leased_job
    first_expiry = scalar(db_engine, "SELECT lease_expires_at FROM jobs WHERE id = :i", i=job_id)
    heartbeat = job_runner.LeaseHeartbeat(
        factory, job_id, token, worker_settings, interval_seconds=0.05
    )
    heartbeat.start()
    try:
        _wait_for(
            lambda: (
                scalar(db_engine, "SELECT lease_expires_at FROM jobs WHERE id = :i", i=job_id)
                > first_expiry
            ),
            "the lease to be extended",
        )
        heartbeat.checkpoint()  # 正常：不拋例外
        assert not heartbeat.cancel_requested

        mapped_client.post(f"/api/v1/jobs/{job_id}/cancel")
        _wait_for(lambda: heartbeat.cancel_requested, "the cancel flag")
        with pytest.raises(ImportCancelledError):
            heartbeat.checkpoint()
    finally:
        heartbeat.stop()


def test_the_heartbeat_detects_a_lost_lease_and_checkpoint_raises(
    leased_job: tuple[int, Any, sessionmaker[Session]], db_engine: Engine, worker_settings: Settings
) -> None:
    job_id, token, factory = leased_job
    # 租約被復原掃描接手、重新排隊，並由另一個 worker 取得新令牌
    with factory() as session:
        job_dispatch.recover_expired_leases(session, worker_settings, now=_future())
        taken = job_service.acquire_lease(session, job_id, worker_settings, now=_future(10_000))
        assert taken is not None
    heartbeat = job_runner.LeaseHeartbeat(
        factory, job_id, token, worker_settings, interval_seconds=0.05
    )
    heartbeat.start()
    try:
        _wait_for(lambda: heartbeat.lease_lost, "the lost lease to be detected")
        with pytest.raises(JobLeaseLostError):
            heartbeat.checkpoint()
    finally:
        heartbeat.stop()


def test_the_heartbeat_survives_transient_database_errors(
    leased_job: tuple[int, Any, sessionmaker[Session]], db_engine: Engine, worker_settings: Settings
) -> None:
    job_id, token, real_factory = leased_job
    failures = {"left": 3}

    def flaky() -> Session:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))
        return real_factory()

    first_heartbeat = scalar(
        db_engine, "SELECT last_heartbeat_at FROM jobs WHERE id = :i", i=job_id
    )
    heartbeat = job_runner.LeaseHeartbeat(
        flaky,  # type: ignore[arg-type]
        job_id,
        token,
        worker_settings,
        interval_seconds=0.03,
    )
    heartbeat.start()
    try:
        _wait_for(
            lambda: (
                scalar(db_engine, "SELECT last_heartbeat_at FROM jobs WHERE id = :i", i=job_id)
                > first_heartbeat
            ),
            "a heartbeat after the transient failures",
        )
        assert failures["left"] == 0
        assert not heartbeat.lease_lost  # 暫時性錯誤不算失去租約
        heartbeat.checkpoint()
    finally:
        heartbeat.stop()
