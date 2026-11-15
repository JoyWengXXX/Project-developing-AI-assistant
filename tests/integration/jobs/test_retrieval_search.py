"""跨快照檢索透過 API 與實際 PostgreSQL 驗證。"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.embedding.gateway_client import embed_query
from repopilot.embedding.local_runtime import EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.main import create_app
from repopilot.services import job_runner, retrieval_job_runner, retrieval_jobs, retrieval_search
from tests.integration.jobs.conftest import WorkerEnv
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files


@dataclass(frozen=True)
class SearchFixtures:
    client: TestClient
    snapshot_ids: list[int]
    not_indexed: int


@pytest.fixture
def ready_search(
    db_engine: Engine,
    clean_repositories: None,
    clean_jobs: None,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> SearchFixtures:
    class FakeEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            pass

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            if "lookup" in value:
                return [0.0, 1.0] + [0.0] * 766
            return [1.0] + [0.0] * 767

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", FakeEmbedding)
    settings = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": "a" * 64,
            "embedding_runtime_sha256": "b" * 64,
            "embedding_socket_path": Path("/fake/socket"),
        }
    )
    snapshots = []
    for number, content in enumerate(
        ("Widget sort handler\n", "widget lookup\n", "widget sort unselected\n"), start=1
    ):
        with db_engine.begin() as connection:
            repo = connection.scalar(
                text(
                    "INSERT INTO repositories (name, canonical_local_path) "
                    "VALUES (:name, :path) RETURNING id"
                ),
                {"name": f"repo-{number}", "path": f"/fake-{number}"},
            )
        assert isinstance(repo, int)
        snapshot_id = add_snapshot(db_engine, repo)
        add_files(db_engine, snapshot_id, {f"file-{number}.txt": content})
        with Session(db_engine, expire_on_commit=False) as session:
            submission = retrieval_jobs.submit_retrieval_job(session, snapshot_id, settings)
            assert submission.job is not None
            job_id = submission.job.id
        assert job_runner.run_job(job_id, db_engine, settings) == "succeeded"
        snapshots.append(snapshot_id)
    another_version = add_snapshot(db_engine, repo, index=2)
    add_files(db_engine, another_version, {"file-3.txt": "widget sort newer\n"})
    with Session(db_engine, expire_on_commit=False) as session:
        submission = retrieval_jobs.submit_retrieval_job(session, another_version, settings)
        assert submission.job is not None
        job_id = submission.job.id
    assert job_runner.run_job(job_id, db_engine, settings) == "succeeded"
    snapshots.append(another_version)
    not_indexed = add_snapshot(db_engine, repo, index=3)
    return SearchFixtures(TestClient(create_app(settings)), snapshots, not_indexed)


def test_keyword_search_ranks_only_selected_snapshots(ready_search: SearchFixtures) -> None:
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": ready_search.snapshot_ids[:2],
            "query": "widget SORT",
            "mode": "keyword",
            "limit": 5,
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "keyword"
    assert [item["snapshot_id"] for item in result["items"]] == ready_search.snapshot_ids[:2]
    first, second = result["items"]
    assert first["keyword_score"] == 2 and second["keyword_score"] == 1
    assert first["path"] == "file-1.txt" and first["content"] == "Widget sort handler\n"
    assert first["start_line"] == first["end_line"] == 1
    assert len(first["commit_sha"]) == 40
    assert first["support_level"] == "text"
    assert all(item["snapshot_id"] != ready_search.snapshot_ids[2] for item in result["items"])


def test_keyword_search_rejects_an_unready_snapshot_without_partial_results(
    ready_search: SearchFixtures,
) -> None:
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": [ready_search.snapshot_ids[0], ready_search.not_indexed],
            "query": "widget",
            "mode": "keyword",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "retrieval_index_not_ready"
    assert response.json()["error"]["details"]["snapshot_id"] == ready_search.not_indexed


def test_keyword_search_keeps_versions_separate_with_stable_ties(
    ready_search: SearchFixtures,
) -> None:
    older, newer = ready_search.snapshot_ids[2:]
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={"snapshot_ids": [newer, older], "query": "widget sort", "mode": "keyword"},
    )
    assert response.status_code == 200
    first, second = response.json()["items"]
    assert [first["snapshot_id"], second["snapshot_id"]] == [older, newer]
    assert first["repository_id"] == second["repository_id"]
    assert first["commit_sha"] != second["commit_sha"]


def test_keyword_search_identifies_missing_snapshot(ready_search: SearchFixtures) -> None:
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": [ready_search.snapshot_ids[0], 999999999],
            "query": "widget",
            "mode": "keyword",
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["details"]["snapshot_id"] == 999999999


def test_keyword_search_identifies_incomplete_snapshot(
    ready_search: SearchFixtures, db_engine: Engine
) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            text("UPDATE snapshots SET status='failed' WHERE id=:id"),
            {"id": ready_search.not_indexed},
        )
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": [ready_search.snapshot_ids[0], ready_search.not_indexed],
            "query": "widget",
            "mode": "keyword",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_not_completed"
    assert response.json()["error"]["details"]["snapshot_id"] == ready_search.not_indexed


def test_keyword_search_empty_matches_are_valid_and_limit_is_respected(
    ready_search: SearchFixtures,
) -> None:
    payload = {
        "snapshot_ids": ready_search.snapshot_ids[:2],
        "query": "notpresent",
        "mode": "keyword",
        "limit": 1,
    }
    response = ready_search.client.post("/api/v1/retrieval/search", json=payload)
    assert response.status_code == 200 and response.json()["items"] == []
    payload["query"] = "widget"
    response = ready_search.client.post("/api/v1/retrieval/search", json=payload)
    assert response.status_code == 200 and len(response.json()["items"]) == 1


@pytest.mark.parametrize("query", ["", "++", "\u0000"])
def test_keyword_search_rejects_invalid_query(ready_search: SearchFixtures, query: str) -> None:
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={"snapshot_ids": ready_search.snapshot_ids[:1], "query": query, "mode": "keyword"},
    )
    assert response.status_code == 422


def test_semantic_search_ranks_cross_snapshot_vectors(
    ready_search: SearchFixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retrieval_search, "embed_query", lambda *args: [0.0, 1.0] + [0.0] * 766, raising=False
    )
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": ready_search.snapshot_ids[:2],
            "query": "find by identifier",
            "mode": "semantic",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "semantic"
    assert [item["snapshot_id"] for item in result["items"]] == ready_search.snapshot_ids[1::-1]
    assert result["items"][0]["semantic_score"] == pytest.approx(1.0)
    assert result["items"][0]["content"] == "widget lookup\n"


def test_semantic_search_uses_best_segment_once_per_chunk(
    ready_search: SearchFixtures, monkeypatch: pytest.MonkeyPatch, db_engine: Engine
) -> None:
    vector = "[" + ",".join(["0", "1", *(["0"] * 766)]) + "]"
    with db_engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT v.id, v.index_id, v.chunk_set_id, v.chunk_id, v.end_offset "
                "FROM retrieval_vectors v JOIN retrieval_indexes i ON i.id=v.index_id "
                "WHERE i.snapshot_id=:snapshot_id"
            ),
            {"snapshot_id": ready_search.snapshot_ids[0]},
        ).one()
        connection.execute(
            text("UPDATE retrieval_vectors SET end_offset=5 WHERE id=:id"), {"id": row.id}
        )
        connection.execute(
            text(
                "INSERT INTO retrieval_vectors "
                "(index_id, chunk_set_id, chunk_id, ordinal, start_offset, end_offset, "
                "token_count, embedding) "
                "VALUES (:index_id, :chunk_set_id, :chunk_id, 1, 5, :end_offset, 2, "
                "CAST(:vector AS vector))"
            ),
            {
                "index_id": row.index_id,
                "chunk_set_id": row.chunk_set_id,
                "chunk_id": row.chunk_id,
                "end_offset": row.end_offset,
                "vector": vector,
            },
        )
    monkeypatch.setattr(retrieval_search, "embed_query", lambda *args: [0.0, 1.0] + [0.0] * 766)
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={"snapshot_ids": ready_search.snapshot_ids[:2], "query": "++", "mode": "semantic"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["snapshot_id"] for item in items] == ready_search.snapshot_ids[:2]
    assert len({item["chunk_id"] for item in items}) == 2
    assert items[0]["semantic_score"] == pytest.approx(1.0)


def test_hybrid_search_combines_keyword_and_semantic_ranks(
    ready_search: SearchFixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retrieval_search, "embed_query", lambda *args: [0.0, 1.0] + [0.0] * 766, raising=False
    )
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": ready_search.snapshot_ids[:2],
            "query": "widget sort",
            "mode": "hybrid",
        },
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["snapshot_id"] for item in items] == ready_search.snapshot_ids[:2]
    assert items[0]["hybrid_score"] == pytest.approx(1 / 61 + 1 / 62)
    assert items[1]["hybrid_score"] == pytest.approx(1 / 61 + 1 / 62)


def test_semantic_search_rejects_unready_snapshot_without_querying_model(
    ready_search: SearchFixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object) -> list[float]:
        pytest.fail("The model must not be queried when an index is unavailable.")

    monkeypatch.setattr(retrieval_search, "embed_query", unexpected, raising=False)
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": [ready_search.snapshot_ids[0], ready_search.not_indexed],
            "query": "find by identifier",
            "mode": "semantic",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "retrieval_index_not_ready"


def test_semantic_search_reports_model_unavailability(
    ready_search: SearchFixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object) -> list[float]:
        raise EmbeddingRuntimeError("The test model is unavailable.")

    monkeypatch.setattr(retrieval_search, "embed_query", unavailable, raising=False)
    response = ready_search.client.post(
        "/api/v1/retrieval/search",
        json={
            "snapshot_ids": ready_search.snapshot_ids[:1],
            "query": "find by identifier",
            "mode": "semantic",
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embedding_runtime_error"


@pytest.mark.skipif(
    os.environ.get("REPOPILOT_RUN_REAL_GATEWAY_TEST") != "1",
    reason="requires an explicitly configured isolated gateway and model",
)
def test_semantic_api_with_real_model_and_postgresql(
    db_engine: Engine,
    clean_repositories: None,
    clean_jobs: None,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = worker_env.settings.model_copy(
        update={
            "embedding_model_sha256": os.environ["EMBEDDING_MODEL_SHA256"],
            "embedding_runtime_sha256": os.environ["EMBEDDING_RUNTIME_SHA256"],
            "embedding_gateway_port": int(os.environ["EMBEDDING_GATEWAY_PORT"]),
            "embedding_gateway_token": SecretStr(os.environ["EMBEDDING_GATEWAY_TOKEN"]),
            "embedding_socket_path": Path("/fake/socket"),
        }
    )

    class GatewayEmbedding:
        def __init__(self, *, socket_path: Path, profile: EmbeddingProfile) -> None:
            self.profile = profile

        def verify_identity(self) -> None:
            pass

        def count_tokens(self, value: str) -> int:
            return len(value) + 1

        def embed(self, value: str) -> list[float]:
            return embed_query(settings, self.profile, value)

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", GatewayEmbedding)
    with db_engine.begin() as connection:
        repository_id = connection.scalar(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('fake-real-model', '/fake-real-model') RETURNING id"
            )
        )
    assert isinstance(repository_id, int)
    snapshot_id = add_snapshot(db_engine, repository_id)
    add_files(
        db_engine, snapshot_id, {"fake.py": "def sort_values(values):\n    return sorted(values)\n"}
    )
    with Session(db_engine, expire_on_commit=False) as session:
        submission = retrieval_jobs.submit_retrieval_job(session, snapshot_id, settings)
        assert submission.job is not None
        job_id = submission.job.id
    assert job_runner.run_job(job_id, db_engine, settings) == "succeeded"
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/v1/retrieval/search",
        json={"snapshot_ids": [snapshot_id], "query": "sort a list", "mode": "semantic"},
    )
    assert response.status_code == 200
    result = response.json()["items"]
    assert len(result) == 1
    assert result[0]["path"] == "fake.py"
    assert result[0]["snapshot_id"] == snapshot_id
    assert -1 <= result[0]["semantic_score"] <= 1
