"""背景工作 API：`GET /jobs`、`GET /jobs/{id}`、`GET /jobs/status`、`POST /jobs/{id}/cancel|retry`。

本切片沒有建立 job 的公開端點；job 由服務層建立（等同切片 3 起 API 內部的作法）。
"""

import re
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.main import create_app
from repopilot.services import job_dispatch
from repopilot.services import jobs as job_service
from repopilot.services import service_heartbeats as heartbeats
from tests.integration.jobs.conftest import at, new_job, running_job, scalar

JOBS = "/api/v1/jobs"
JOB_FIELDS = {
    "id",
    "kind",
    "status",
    "phase",
    "repository_id",
    "snapshot_id",
    "params",
    "attempt",
    "max_attempts",
    "next_attempt_at",
    "cancel_requested",
    "cancel_requested_at",
    "progress",
    "diagnostics",
    "error",
    "result",
    "request_id",
    "created_at",
    "started_at",
    "phase_started_at",
    "last_heartbeat_at",
    "finished_at",
    "updated_at",
    "status_url",
}


@pytest.fixture
def client(
    job_settings: Settings, clean_repositories: None, clean_jobs: None
) -> Iterator[TestClient]:
    with TestClient(create_app(job_settings)) as test_client:
        yield test_client


def _repository(db_engine: Engine, name: str = "demo") -> int:
    with db_engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO repositories (name, canonical_local_path) "
                    "VALUES (:n, :p) RETURNING id"
                ),
                {"n": name, "p": f"/fake/{name}"},
            ).scalar_one()
        )


def _snapshot(db_engine: Engine, repository_id: int) -> int:
    with db_engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO snapshots "
                    "(repository_id, commit_sha, import_profile_hash, status) "
                    "VALUES (:r, :c, 'h', 'completed') RETURNING id"
                ),
                {"r": repository_id, "c": "a" * 40},
            ).scalar_one()
        )


def _make(session: Session, settings: Settings, key: str, **kwargs: Any) -> int:
    return new_job(session, settings, key=key, **kwargs).id


# --- GET /jobs ---


def test_list_is_empty_with_the_page_envelope(client: TestClient) -> None:
    response = client.get(JOBS)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_list_is_newest_first_and_paginates_without_gaps_or_duplicates(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    ids = [_make(session, job_settings, f"k{i}") for i in range(5)]

    first = client.get(JOBS, params={"limit": 2, "offset": 0}).json()
    second = client.get(JOBS, params={"limit": 2, "offset": 2}).json()
    third = client.get(JOBS, params={"limit": 2, "offset": 4}).json()

    seen = [i["id"] for page in (first, second, third) for i in page["items"]]
    assert seen == sorted(ids, reverse=True)
    assert (first["total"], second["total"], third["total"]) == (5, 5, 5)
    assert (first["limit"], second["offset"]) == (2, 2)
    assert client.get(JOBS, params={"offset": 99}).json()["items"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"limit": "x"},
        {"offset": -1},
        {"offset": 2**31},
        {"status": "bogus"},
        {"kind": "bogus"},
        {"repository_id": 0},
        {"repository_id": 2**31},
        {"snapshot_id": -1},
    ],
)
def test_list_rejects_invalid_parameters_with_422(
    client: TestClient, params: dict[str, Any]
) -> None:
    response = client.get(JOBS, params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_accepts_the_maximum_limit(client: TestClient) -> None:
    response = client.get(JOBS, params={"limit": 200})

    assert response.status_code == 200 and response.json()["limit"] == 200


def test_list_filters_by_status_kind_repository_and_snapshot(
    client: TestClient, session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    repo_a = _repository(db_engine, "a")
    repo_b = _repository(db_engine, "b")
    snapshot_a = _snapshot(db_engine, repo_a)
    queued_a = _make(session, job_settings, "qa", repository_id=repo_a, snapshot_id=snapshot_a)
    running_b, _ = running_job(session, job_settings, key="rb", repository_id=repo_b)
    fetch = _make(session, job_settings, "f", kind="github_fetch", repository_id=repo_b)
    cancelled = _make(session, job_settings, "c")
    job_service.cancel_job(session, cancelled)

    def ids(**params: Any) -> list[int]:
        body = client.get(JOBS, params=params).json()
        assert body["total"] == len(body["items"])
        return sorted(item["id"] for item in body["items"])

    assert ids(status="queued") == sorted([queued_a, fetch])
    assert ids(status="running") == [running_b.id]
    assert ids(status="cancelled") == [cancelled]
    assert ids(status="active") == sorted([queued_a, running_b.id, fetch])
    assert ids(kind="github_fetch") == [fetch]
    assert ids(repository_id=repo_b) == sorted([running_b.id, fetch])
    assert ids(snapshot_id=snapshot_a) == [queued_a]
    assert ids(repository_id=repo_b, status="queued") == [fetch]
    assert ids(repository_id=999_999) == []


# --- GET /jobs/{id} 與 /jobs/status 的路由 ---


def test_get_job_returns_the_documented_shape(
    client: TestClient, session: Session, job_settings: Settings, db_engine: Engine
) -> None:
    repo = _repository(db_engine)
    job_id = _make(
        session,
        job_settings,
        "k",
        repository_id=repo,
        params={"requested_revision": "main", "commit_sha": "b" * 40},
        request_id="req-abc",
        kind="snapshot_import",
    )

    response = client.get(f"{JOBS}/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == JOB_FIELDS
    assert body["id"] == job_id and body["kind"] == "snapshot_import"
    assert (body["status"], body["phase"], body["attempt"], body["max_attempts"]) == (
        "queued",
        "pending",
        0,
        3,
    )
    assert body["repository_id"] == repo and body["snapshot_id"] is None
    assert body["params"] == {"requested_revision": "main", "commit_sha": "b" * 40}
    assert body["progress"] == {
        "processed_files": 0,
        "total_files": None,
        "indexed_files": 0,
        "parse_error_count": 0,
        "bytes_received": None,
        "vectors_written": None,
        "estimated_remaining_seconds": None,
        "chunks_reused": None,
    }
    assert body["cancel_requested"] is False and body["error"] is None and body["result"] is None
    assert body["request_id"] == "req-abc"
    assert body["status_url"] == f"/api/v1/jobs/{job_id}"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", body["created_at"])
    assert body["started_at"] is None and body["finished_at"] is None


def test_get_job_never_exposes_the_lease_token_or_work_state(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    job_service.update_progress(
        session, job.id, token, work_state={"temp_dir": "internal-only", "current_file_id": 7}
    )

    text_body = client.get(f"{JOBS}/{job.id}").text
    list_text = client.get(JOBS).text

    for payload in (text_body, list_text):
        assert str(token) not in payload
        assert "lease_token" not in payload and "work_state" not in payload
        assert "internal-only" not in payload
    assert UUID(str(token))  # 令牌確實存在於資料庫，只是不外露


def test_running_job_shows_progress_and_heartbeat(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    job_service.update_progress(
        session, job.id, token, phase="importing", processed_files=4, total_files=10
    )

    body = client.get(f"{JOBS}/{job.id}").json()

    assert (body["status"], body["phase"], body["attempt"]) == ("running", "importing", 1)
    assert body["progress"]["processed_files"] == 4 and body["progress"]["total_files"] == 10
    assert body["last_heartbeat_at"] is not None and body["started_at"] is not None


def test_failed_job_shows_only_the_safe_error_code_and_summary(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    job_service.fail_job(
        session,
        job.id,
        token,
        job_settings,
        error_code="import_limit_exceeded",
        error_summary="Snapshot exceeds the file count limit.",
    )

    body = client.get(f"{JOBS}/{job.id}").json()

    assert body["status"] == "failed"
    assert body["error"] == {
        "code": "import_limit_exceeded",
        "message": "Snapshot exceeds the file count limit.",
    }
    assert body["finished_at"] is not None


def test_get_unknown_job_is_404_with_the_standard_error_format(client: TestClient) -> None:
    response = client.get(f"{JOBS}/424242", headers={"X-Request-ID": "trace-1"})

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "job_not_found" and error["request_id"] == "trace-1"


@pytest.mark.parametrize("job_id", ["0", "-1", "9223372036854775808", "abc", "1.5"])
def test_job_id_out_of_range_or_not_a_number_is_422(client: TestClient, job_id: str) -> None:
    response = client.get(f"{JOBS}/{job_id}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_the_largest_valid_job_id_is_404_not_500(client: TestClient) -> None:
    assert client.get(f"{JOBS}/{2**63 - 1}").status_code == 404


def test_status_route_is_not_swallowed_by_the_job_id_route(client: TestClient) -> None:
    response = client.get(f"{JOBS}/status")

    assert response.status_code == 200
    assert "worker" in response.json()


# --- GET /jobs/status ---


def test_status_reports_absent_services_and_counts(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    new_job(session, job_settings, key="q")
    running_job(session, job_settings, key="r")

    body = client.get(f"{JOBS}/status").json()

    assert body == {
        "dispatcher": {"status": "absent", "last_seen_at": None, "instances_online": 0},
        "worker": {"status": "absent", "last_seen_at": None, "instances_online": 0},
        "queued": 1,
        "running": 1,
        "heartbeat_timeout_seconds": 45,
    }


def test_status_distinguishes_online_and_stale_services(
    client: TestClient, session: Session
) -> None:
    heartbeats.record_heartbeat(session, "dispatcher", "d1")  # 資料庫現在時間
    heartbeats.record_heartbeat(session, "worker", "w1", now=at(0))  # 2026-01-01，早已過期

    body = client.get(f"{JOBS}/status").json()

    assert body["dispatcher"]["status"] == "online"
    assert body["dispatcher"]["instances_online"] == 1
    assert body["worker"]["status"] == "stale"
    assert body["worker"]["last_seen_at"] == "2026-01-01T12:00:00Z"


# --- POST /jobs/{id}/cancel ---


def test_cancel_queued_job_returns_200_and_cancelled(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job_id = _make(session, job_settings, "k")

    response = client.post(f"{JOBS}/{job_id}/cancel")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["finished_at"] is not None) == ("cancelled", True)
    assert scalar(session.get_bind(), "SELECT count(*) FROM job_dispatches") == 0


def test_cancel_running_job_returns_202_with_the_flag_and_location(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, _ = running_job(session, job_settings)

    response = client.post(f"{JOBS}/{job.id}/cancel")

    assert response.status_code == 202
    assert response.headers["Location"] == f"/api/v1/jobs/{job.id}"
    body = response.json()
    assert (body["status"], body["cancel_requested"]) == ("running", True)


def test_cancel_is_idempotent_for_cancelled_jobs(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job_id = _make(session, job_settings, "k")
    assert client.post(f"{JOBS}/{job_id}/cancel").status_code == 200

    again = client.post(f"{JOBS}/{job_id}/cancel")

    assert again.status_code == 200 and again.json()["status"] == "cancelled"


@pytest.mark.parametrize("final", ["succeeded", "failed"])
def test_cancel_finished_job_is_409(
    client: TestClient, session: Session, job_settings: Settings, final: str
) -> None:
    job, token = running_job(session, job_settings)
    if final == "succeeded":
        job_service.complete_job(session, job.id, token)
    else:
        job_service.fail_job(session, job.id, token, job_settings, error_code="invalid_revision")

    response = client.post(f"{JOBS}/{job.id}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_cancellable"
    assert client.get(f"{JOBS}/{job.id}").json()["status"] == final


# --- POST /jobs/{id}/retry ---


def test_retry_failed_job_returns_202_and_queues_the_same_job(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    job_service.fail_job(session, job.id, token, job_settings, error_code="invalid_revision")

    response = client.post(f"{JOBS}/{job.id}/retry")

    assert response.status_code == 202
    assert response.headers["Location"] == f"/api/v1/jobs/{job.id}"
    body = response.json()
    assert body["id"] == job.id and body["status"] == "queued"
    assert (body["attempt"], body["max_attempts"]) == (1, 4)
    assert body["finished_at"] is None
    assert scalar(session.get_bind(), "SELECT count(*) FROM job_dispatches") == 1


@pytest.mark.parametrize("state", ["queued", "running", "succeeded", "cancelled"])
def test_retry_non_failed_job_is_409(
    client: TestClient, session: Session, job_settings: Settings, state: str
) -> None:
    job = new_job(session, job_settings)
    if state == "cancelled":
        job_service.cancel_job(session, job.id)
    elif state in ("running", "succeeded"):
        lease = job_service.acquire_lease(session, job.id, job_settings)
        assert lease is not None and lease.lease_token is not None
        if state == "succeeded":
            job_service.complete_job(session, job.id, lease.lease_token)

    response = client.post(f"{JOBS}/{job.id}/retry")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_retryable"
    assert client.get(f"{JOBS}/{job.id}").json()["status"] == state


def test_cancel_and_retry_of_unknown_jobs_are_404(client: TestClient) -> None:
    for action in ("cancel", "retry"):
        response = client.post(f"{JOBS}/424242/{action}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"
    assert client.post(f"{JOBS}/0/cancel").status_code == 422
    assert client.post(f"{JOBS}/0/retry").status_code == 422


def test_retried_job_can_be_dispatched_and_leased_again(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    job_settings: Settings,
) -> None:
    """API 狀態轉換與服務層銜接：retry 後事件可被派送、可再次取得租約。"""
    job, token = running_job(session, job_settings)
    job_service.fail_job(session, job.id, token, job_settings, error_code="invalid_revision")
    assert client.post(f"{JOBS}/{job.id}/retry").status_code == 202

    class Recorder:
        def __init__(self) -> None:
            self.ids: list[int] = []

        def publish(self, job_id: int) -> None:
            self.ids.append(job_id)

    recorder = Recorder()
    assert job_dispatch.dispatch_pending(session, recorder).sent == 1
    assert recorder.ids == [job.id]
    lease = job_service.acquire_lease(session, job.id, job_settings)
    assert lease is not None and lease.attempt == 2


def test_openapi_lists_the_job_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert {
        f"{JOBS}",
        f"{JOBS}/status",
        f"{JOBS}/{{job_id}}",
        f"{JOBS}/{{job_id}}/cancel",
        f"{JOBS}/{{job_id}}/retry",
    } <= set(paths)
    # 本切片不新增建立 job 的公開端點
    assert "post" not in paths[JOBS]


# --- 失敗追查欄位（第二階段切片 4）：cancel_requested_at 與 work_state 的安全摘要 ---

ZERO_DIAGNOSTICS = {
    "current_unit": None,
    "poison": {"suspect_crashes": 0, "skipped_units": 0, "total_crashes": 0},
}


def test_a_fresh_job_has_zero_diagnostics_and_no_cancel_timestamp(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job_id = _make(session, job_settings, "k")

    body = client.get(f"{JOBS}/{job_id}").json()

    assert body["diagnostics"] == ZERO_DIAGNOSTICS
    assert body["cancel_requested"] is False and body["cancel_requested_at"] is None


def test_cancel_requested_at_is_reported_once_and_does_not_move_on_repeated_requests(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, _ = running_job(session, job_settings)

    first = client.post(f"{JOBS}/{job.id}/cancel").json()
    second = client.post(f"{JOBS}/{job.id}/cancel").json()

    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", first["cancel_requested_at"])
    assert first["cancel_requested"] is True
    assert second["cancel_requested_at"] == first["cancel_requested_at"]  # 冪等：時間不變
    assert (
        client.get(f"{JOBS}/{job.id}").json()["cancel_requested_at"]
        == (first["cancel_requested_at"])
    )


def test_diagnostics_summarize_the_work_state_without_leaking_identifiers(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    job_service.update_progress(
        session,
        job.id,
        token,
        work_state={
            "current_unit": {
                "id": "blobs-SECRET-UNIT-ID",
                "kind": "blob_batch",
                "crash_kind": "worker_crash",
                "index": 3,
                "count": 9,
            },
            "poison": {
                "suspect": {
                    "unit": "blobs-SECRET-UNIT-ID",
                    "kind": "blob_batch",
                    "crash_kind": "worker_crash",
                    "crashes": 2,
                },
                "skipped": [
                    {"unit": "file-SECRET-PATH", "crash_kind": "parser_crash", "crashes": 2}
                ],
                "total_crashes": 5,
            },
            "temp_dir": "internal-only",
        },
    )

    response = client.get(f"{JOBS}/{job.id}")

    assert response.json()["diagnostics"] == {
        "current_unit": {"kind": "blob_batch", "index": 3, "count": 9},
        "poison": {"suspect_crashes": 2, "skipped_units": 1, "total_crashes": 5},
    }
    for leaked in ("SECRET-UNIT-ID", "SECRET-PATH", "internal-only", "work_state", str(token)):
        assert leaked not in response.text
        assert leaked not in client.get(JOBS).text


def test_the_current_unit_is_only_shown_while_the_job_is_running(
    client: TestClient, session: Session, job_settings: Settings
) -> None:
    job, token = running_job(session, job_settings)
    job_service.update_progress(
        session,
        job.id,
        token,
        work_state={
            "current_unit": {"id": "u", "kind": "blob_batch", "crash_kind": "worker_crash"},
        },
    )
    assert client.get(f"{JOBS}/{job.id}").json()["diagnostics"]["current_unit"] is not None

    job_service.fail_job(
        session, job.id, token, job_settings, error_code="git_timeout", now=at(1), rand=lambda: 0.5
    )

    body = client.get(f"{JOBS}/{job.id}").json()
    assert body["status"] == "queued"  # 重新排隊：留在 work_state 的指標已過時，不顯示
    assert body["diagnostics"]["current_unit"] is None


@pytest.mark.parametrize(
    "garbage",
    ['"text"', "[]", '{"poison": 5, "current_unit": []}', '{"poison": {"suspect": "x"}}'],
)
def test_a_work_state_with_an_unexpected_shape_never_breaks_the_endpoint(
    client: TestClient, session: Session, job_settings: Settings, db_engine: Engine, garbage: str
) -> None:
    job_id = _make(session, job_settings, "k")
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET work_state = CAST(:w AS jsonb) WHERE id = :i"),
            {"w": garbage, "i": job_id},
        )

    for url in (f"{JOBS}/{job_id}", JOBS):
        response = client.get(url)
        assert response.status_code == 200
    assert client.get(f"{JOBS}/{job_id}").json()["diagnostics"] == ZERO_DIAGNOSTICS
