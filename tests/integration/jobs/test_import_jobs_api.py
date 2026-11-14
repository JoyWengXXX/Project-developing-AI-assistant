"""`POST /api/v1/repositories/{id}/import-jobs`（規格 4.4）與同步 `/imports` 的 deprecated 標示。

真實 PostgreSQL＋真實暫存 Git repo；不需要 Redis／worker 容器（那是 `stack` 測試的範圍）。
需要 worker 實際執行的情境（已完成快照的重用）直接呼叫 `job_runner.run_job`。
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.main import create_app
from repopilot.services import job_runner
from repopilot.services import jobs as job_service
from repopilot.services.import_profile import ImportProfile
from tests.git_helpers import commit_files, git, init_repo
from tests.integration.jobs.conftest import (
    WorkerEnv,
    at,
    job_state,
    register_repo,
    remove_tree,
    run_together,
    scalar,
    submit_import_job,
)

FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n"}
JOB_SUBMIT_FIELDS = {"job", "snapshot_id", "repository_id", "reused"}


@pytest.fixture
def repo(worker_env: WorkerEnv) -> Path:
    return init_repo(worker_env.roots / "demo", files=FILES)


@pytest.fixture
def repository_id(mapped_client: TestClient, repo: Path) -> int:
    return register_repo(mapped_client, repo)


def _jobs(db_engine: Engine) -> list[tuple[int, str]]:
    with db_engine.connect() as connection:
        return [
            (row[0], row[1])
            for row in connection.execute(text("SELECT id, status FROM jobs ORDER BY id"))
        ]


def _fail_the_job(db_engine: Engine, worker_env: WorkerEnv, job_id: int) -> None:
    """取得租約後回報永久性失敗，讓 job 進入 failed。"""
    with Session(db_engine) as session:
        job = job_service.acquire_lease(session, job_id, worker_env.settings, now=at(0))
        assert job is not None
        assert job.lease_token is not None
        job_service.fail_job(
            session,
            job_id,
            job.lease_token,
            worker_env.settings,
            error_code="import_limit_exceeded",
            error_summary="too many files",
            now=at(1),
        )


def _insert_importing_snapshot(
    db_engine: Engine, worker_env: WorkerEnv, repository_id: int, repo: Path
) -> None:
    """模擬『有人（同步端點或已死的 worker）正在匯入』：新鮮的 importing 快照。"""
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO snapshots (repository_id, commit_sha, import_profile_hash, status, "
                "file_count, skipped_count, total_bytes, started_at) "
                "VALUES (:r, :s, :h, 'importing', 0, 0, 0, now())"
            ),
            {
                "r": repository_id,
                "s": git(repo, "rev-parse", "HEAD"),
                "h": ImportProfile.from_settings(worker_env.settings).hash(),
            },
        )


# --- 202：建立 ---


def test_submit_creates_a_queued_job_with_the_resolved_sha_and_an_outbox_event(
    mapped_client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    sha = git(repo, "rev-parse", "HEAD")

    response = submit_import_job(mapped_client, repository_id, revision="HEAD")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == JOB_SUBMIT_FIELDS
    assert body["reused"] is False
    assert body["snapshot_id"] is None
    assert body["repository_id"] == repository_id
    job = body["job"]
    assert response.headers["Location"] == f"/api/v1/jobs/{job['id']}"
    assert job["status_url"] == f"/api/v1/jobs/{job['id']}"
    assert job["kind"] == "snapshot_import"
    assert (job["status"], job["phase"], job["attempt"]) == ("queued", "pending", 0)
    assert job["max_attempts"] == 3
    # 提交當下已解析成完整 SHA；requested_revision 是使用者的原文
    assert job["params"] == {"requested_revision": "HEAD", "commit_sha": sha}
    assert job["progress"]["total_files"] is None  # 不偽造進度
    assert job["error"] is None
    assert job["request_id"] == response.headers["X-Request-ID"]
    # job 與第一筆待派送事件在同一交易（transactional outbox）
    dispatch = scalar(
        db_engine,
        "SELECT status || ':' || attempt_target FROM job_dispatches WHERE job_id = :i",
        i=job["id"],
    )
    assert dispatch == "pending:1"
    repo_key = scalar(db_engine, "SELECT repo_key FROM jobs WHERE id = :i", i=job["id"])
    assert repo_key == f"local:{repository_id}"
    hash_length = scalar(
        db_engine, "SELECT length(import_profile_hash) FROM jobs WHERE id = :i", i=job["id"]
    )
    assert hash_length == 64
    assert scalar(db_engine, "SELECT count(*) FROM snapshots") == 0  # API 只排入，不匯入


def test_body_is_optional_and_defaults_to_head(
    mapped_client: TestClient, repo: Path, repository_id: int
) -> None:
    response = mapped_client.post(f"/api/v1/repositories/{repository_id}/import-jobs")

    assert response.status_code == 202
    params = response.json()["job"]["params"]
    assert params["requested_revision"] == "HEAD"
    assert params["commit_sha"] == git(repo, "rev-parse", "HEAD")


def test_the_sha_is_fixed_at_submission_even_if_the_branch_moves_afterwards(
    mapped_client: TestClient, repo: Path, repository_id: int
) -> None:
    first_sha = git(repo, "rev-parse", "HEAD")
    job = submit_import_job(mapped_client, repository_id, revision="main").json()["job"]

    commit_files(repo, {"README.md": "changed after submit\n"})

    assert git(repo, "rev-parse", "HEAD") != first_sha
    fetched = mapped_client.get(f"/api/v1/jobs/{job['id']}").json()
    assert fetched["params"]["commit_sha"] == first_sha


# --- 去重 ---


def test_submitting_the_same_work_again_returns_the_existing_active_job(
    mapped_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    first = submit_import_job(mapped_client, repository_id, revision="HEAD")
    second = submit_import_job(mapped_client, repository_id, revision="HEAD")

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["job"]["id"] == second.json()["job"]["id"]
    assert len(_jobs(db_engine)) == 1
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches") == 1


def test_different_spellings_of_the_same_commit_share_one_job(
    mapped_client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    sha = git(repo, "rev-parse", "HEAD")

    ids = {
        submit_import_job(mapped_client, repository_id, revision=revision).json()["job"]["id"]
        for revision in ("HEAD", "main", sha, sha[:12])
    }

    assert len(ids) == 1
    assert len(_jobs(db_engine)) == 1


def test_a_different_commit_is_a_different_job(
    mapped_client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    first = submit_import_job(mapped_client, repository_id).json()["job"]["id"]
    commit_files(repo, {"README.md": "v2\n"})
    second = submit_import_job(mapped_client, repository_id).json()["job"]["id"]

    assert first != second
    assert len(_jobs(db_engine)) == 2


def test_concurrent_submissions_create_exactly_one_job(
    mapped_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    def submit(_: int) -> tuple[int, int]:
        response = submit_import_job(mapped_client, repository_id, revision="HEAD")
        return response.status_code, int(response.json()["job"]["id"])

    results = run_together(6, submit)

    assert {status for status, _ in results} == {202}
    assert len({job_id for _, job_id in results}) == 1
    assert len(_jobs(db_engine)) == 1
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches") == 1


# --- 200：快照已完成 ---


def test_a_completed_snapshot_without_an_index_gets_an_indexing_job_instead_of_a_200(
    mapped_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    """規格 4.4：快照 completed 但目前設定的索引缺少 → 建立 job（匯入階段重用快照，只做索引）。"""
    sync = mapped_client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
    assert sync.status_code == 201

    response = submit_import_job(mapped_client, repository_id, revision="HEAD")

    assert response.status_code == 202
    body = response.json()
    assert body["reused"] is False
    assert body["snapshot_id"] == sync.json()["id"]
    assert body["job"]["kind"] == "snapshot_import" and body["job"]["status"] == "queued"
    assert response.headers["Location"] == f"/api/v1/jobs/{body['job']['id']}"
    assert [status for _, status in _jobs(db_engine)] == ["queued"]


def test_a_completed_snapshot_from_a_job_returns_that_job(
    mapped_client: TestClient, worker_env: WorkerEnv, repository_id: int, db_engine: Engine
) -> None:
    job_id = submit_import_job(mapped_client, repository_id).json()["job"]["id"]
    worker_settings = job_runner.apply_worker_timeouts(worker_env.settings)
    assert job_runner.run_job(job_id, db_engine, worker_settings) == "succeeded"

    response = submit_import_job(mapped_client, repository_id)

    assert response.status_code == 200
    body = response.json()
    assert body["reused"] is True
    assert body["job"]["id"] == job_id
    assert body["job"]["status"] == "succeeded"
    assert body["snapshot_id"] == body["job"]["snapshot_id"]
    assert len(_jobs(db_engine)) == 1  # 沒有新 job


# --- 失敗或取消後重新提交：同一個 job ---


def test_resubmitting_after_failure_requeues_the_same_job_with_a_fresh_attempt_budget(
    mapped_client: TestClient, worker_env: WorkerEnv, repository_id: int, db_engine: Engine
) -> None:
    job_id = submit_import_job(mapped_client, repository_id).json()["job"]["id"]
    _fail_the_job(db_engine, worker_env, job_id)
    assert job_state(db_engine, job_id)["status"] == "failed"

    response = submit_import_job(mapped_client, repository_id, revision="HEAD")

    assert response.status_code == 202
    job = response.json()["job"]
    assert job["id"] == job_id  # 同一個 job，不是新的
    assert (job["status"], job["attempt"]) == ("queued", 1)  # attempt 繼續累加
    assert job["max_attempts"] == 1 + worker_env.settings.job_max_attempts  # 新額度
    assert response.headers["Location"] == f"/api/v1/jobs/{job_id}"
    assert len(_jobs(db_engine)) == 1
    assert scalar(db_engine, "SELECT count(*) FROM job_dispatches WHERE job_id = :i", i=job_id) == 1


def test_resubmitting_after_cancellation_requeues_the_same_job(
    mapped_client: TestClient, repository_id: int, db_engine: Engine
) -> None:
    job_id = submit_import_job(mapped_client, repository_id).json()["job"]["id"]
    assert mapped_client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    assert job_state(db_engine, job_id)["status"] == "cancelled"

    response = submit_import_job(mapped_client, repository_id)

    assert response.status_code == 202
    assert response.json()["job"]["id"] == job_id
    assert job_state(db_engine, job_id)["status"] == "queued"
    assert len(_jobs(db_engine)) == 1


def test_resubmission_updates_the_request_id_for_log_correlation(
    mapped_client: TestClient, repository_id: int
) -> None:
    first = submit_import_job(mapped_client, repository_id)
    job_id = first.json()["job"]["id"]
    mapped_client.post(f"/api/v1/jobs/{job_id}/cancel")

    second = submit_import_job(mapped_client, repository_id)

    assert second.json()["job"]["request_id"] == second.headers["X-Request-ID"]
    assert second.headers["X-Request-ID"] != first.headers["X-Request-ID"]


# --- 409：同步端點正在匯入 ---


def test_a_fresh_importing_snapshot_without_any_job_is_a_conflict(
    mapped_client: TestClient,
    worker_env: WorkerEnv,
    repo: Path,
    repository_id: int,
    db_engine: Engine,
) -> None:
    _insert_importing_snapshot(db_engine, worker_env, repository_id, repo)

    response = submit_import_job(mapped_client, repository_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_import_in_progress"
    assert _jobs(db_engine) == []


def test_a_fresh_importing_snapshot_left_by_a_failed_job_is_requeued_not_blocked(
    mapped_client: TestClient,
    worker_env: WorkerEnv,
    repo: Path,
    repository_id: int,
    db_engine: Engine,
) -> None:
    """worker 在最後一次嘗試中被殺：快照仍是新鮮的 importing，但 job 已 failed。

    重新提交必須能排入（之後由 worker 以快照鎖＋takeover 立即接手，不必等 30 分鐘）。
    """
    job_id = submit_import_job(mapped_client, repository_id).json()["job"]["id"]
    _fail_the_job(db_engine, worker_env, job_id)
    _insert_importing_snapshot(db_engine, worker_env, repository_id, repo)

    response = submit_import_job(mapped_client, repository_id)

    assert response.status_code == 202
    assert response.json()["job"]["id"] == job_id


# --- 錯誤 ---


def test_unknown_repository_is_404(mapped_client: TestClient) -> None:
    response = submit_import_job(mapped_client, 987654)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "repository_not_found"


@pytest.mark.parametrize("revision", ["no-such-branch", "-evil", "HEAD~999", "a" * 40])
def test_invalid_revision_is_422_and_creates_no_job(
    mapped_client: TestClient, repository_id: int, db_engine: Engine, revision: str
) -> None:
    response = submit_import_job(mapped_client, repository_id, revision=revision)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_revision"
    assert _jobs(db_engine) == []


@pytest.mark.parametrize(
    "body", [{"revision": ""}, {"revision": "x" * 257}, {"revision": 5}, {"unknown": 1}]
)
def test_malformed_body_is_a_validation_error(
    mapped_client: TestClient, repository_id: int, body: dict[str, Any]
) -> None:
    response = mapped_client.post(f"/api/v1/repositories/{repository_id}/import-jobs", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("bad_id", ["0", "-1", "2147483648", "abc"])
def test_repository_id_range_is_validated(mapped_client: TestClient, bad_id: str) -> None:
    response = mapped_client.post(f"/api/v1/repositories/{bad_id}/import-jobs")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.fixture
def unmapped_client(worker_env: WorkerEnv, clean_repositories: None, clean_jobs: None) -> Any:
    """與 `mapped_client` 相同，但 `WORKER_PATH_MAPPINGS` 是空的。"""
    unmapped = worker_env.settings.model_copy(update={"worker_path_mappings": []})
    with TestClient(create_app(unmapped)) as client:
        yield client


def test_a_repository_not_covered_by_the_path_mappings_is_503_and_creates_no_job(
    unmapped_client: TestClient, repo: Path, db_engine: Engine
) -> None:
    repository_id = register_repo(unmapped_client, repo)

    response = submit_import_job(unmapped_client, repository_id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "worker_path_not_mapped"
    assert str(repo) not in response.text  # 不洩漏路徑
    assert _jobs(db_engine) == []


def test_revision_is_validated_before_the_path_mapping_check(
    unmapped_client: TestClient, repo: Path
) -> None:
    repository_id = register_repo(unmapped_client, repo)

    response = submit_import_job(unmapped_client, repository_id, revision="no-such-branch")

    assert (
        response.status_code == 422
    )  # 規格 4.4 的順序：先 invalid_revision，再 worker_path_not_mapped
    assert response.json()["error"]["code"] == "invalid_revision"


def test_a_repository_whose_directory_disappeared_is_422_and_creates_no_job(
    mapped_client: TestClient, repo: Path, repository_id: int, db_engine: Engine
) -> None:
    remove_tree(repo)

    response = submit_import_job(mapped_client, repository_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_repository_path"
    assert _jobs(db_engine) == []


# --- OpenAPI 與同步端點 ---


def test_the_sync_imports_endpoint_is_marked_deprecated_and_import_jobs_is_not(
    mapped_client: TestClient,
) -> None:
    paths = mapped_client.get("/openapi.json").json()["paths"]

    sync = paths["/api/v1/repositories/{repository_id}/imports"]["post"]
    assert sync["deprecated"] is True
    assert "import-jobs" in sync["description"]
    jobs = paths["/api/v1/repositories/{repository_id}/import-jobs"]["post"]
    assert jobs.get("deprecated", False) is False
    assert set(jobs["responses"]) >= {"200", "202", "404", "409", "422", "503"}


def test_the_sync_endpoint_keeps_its_original_contract(
    mapped_client: TestClient, repository_id: int
) -> None:
    first = mapped_client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
    second = mapped_client.post(f"/api/v1/repositories/{repository_id}/imports", json={})

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["status"] == "completed"
    assert first.json()["id"] == second.json()["id"]
