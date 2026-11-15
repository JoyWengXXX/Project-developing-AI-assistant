"""固定假資料上的檢索品質量測；需明確啟用隔離模型與測試資料庫。"""

import hashlib
import json
import os
import platform
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from repopilot.embedding.gateway_client import embed_query
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.main import create_app
from repopilot.services import job_runner, retrieval_job_runner, retrieval_jobs
from tests.integration.jobs.conftest import WorkerEnv
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files

DATASET = Path(__file__).with_name("retrieval_evaluation_v1.json")


def recall_at_five(relevant: set[str], results: list[str]) -> float:
    if not relevant:
        raise ValueError("A query must have at least one relevant item.")
    return len(relevant.intersection(results[:5])) / len(relevant)


def test_recall_counts_unique_relevant_items_only_in_first_five() -> None:
    assert recall_at_five({"a", "b"}, ["a", "a", "x", "y", "z", "b"]) == 0.5
    with pytest.raises(ValueError):
        recall_at_five(set(), [])


@pytest.mark.skipif(
    os.environ.get("REPOPILOT_RUN_RETRIEVAL_EVALUATION") != "1",
    reason="requires an explicitly configured isolated gateway and model",
)
def test_fixed_dataset_real_model_evaluation(
    db_engine: Engine,
    clean_repositories: None,
    clean_jobs: None,
    worker_env: WorkerEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = DATASET.read_bytes()
    dataset = json.loads(raw)
    documents = {item["path"]: item["content"] for item in dataset["documents"]}
    assert len(documents) == len(dataset["documents"])
    assert {q["split"] for q in dataset["queries"]} == {"development", "holdout"}
    assert all(set(q["relevant"]) <= documents.keys() and q["relevant"] for q in dataset["queries"])
    assert all(len(value.encode("utf-8")) < 256 for value in documents.values())

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
            return len(value.encode("utf-8")) + 1

        def embed(self, value: str) -> list[float]:
            return embed_query(settings, self.profile, value)

    monkeypatch.setattr(retrieval_job_runner, "SocketEmbeddingClient", GatewayEmbedding)
    with db_engine.begin() as connection:
        repository_id = connection.execute(
            text(
                "INSERT INTO repositories (name, canonical_local_path) "
                "VALUES ('evaluation-v1', '/evaluation-v1') RETURNING id"
            )
        ).scalar_one()
    snapshot_id = add_snapshot(db_engine, repository_id)
    add_files(db_engine, snapshot_id, documents)
    with Session(db_engine, expire_on_commit=False) as session:
        submission = retrieval_jobs.submit_retrieval_job(session, snapshot_id, settings)
        assert submission.job is not None
        job_id = submission.job.id
    assert job_runner.run_job(job_id, db_engine, settings) == "succeeded"

    rows = []
    with TestClient(create_app(settings)) as client:
        candidate_response = client.post(
            "/api/v1/retrieval/search",
            json={
                "snapshot_ids": [snapshot_id],
                "query": "evaluation candidates",
                "mode": "semantic",
                "limit": 50,
            },
        )
        assert candidate_response.status_code == 200
        candidates = [item["path"] for item in candidate_response.json()["items"]]
        assert len(candidates) == len(documents) and set(candidates) == documents.keys()
        for query in dataset["queries"]:
            for mode in ("keyword", "semantic", "hybrid"):
                started = time.perf_counter()
                response = client.post(
                    "/api/v1/retrieval/search",
                    json={
                        "snapshot_ids": [snapshot_id],
                        "query": query["query"],
                        "mode": mode,
                        "limit": 5,
                    },
                )
                seconds = time.perf_counter() - started
                assert response.status_code == 200, response.text
                paths = [item["path"] for item in response.json()["items"]]
                rows.append(
                    {
                        "split": query["split"],
                        "query": query["query"],
                        "mode": mode,
                        "results": paths,
                        "recall_at_5": recall_at_five(set(query["relevant"]), paths),
                        "seconds": seconds,
                    }
                )
    report = {
        "dataset_version": dataset["version"],
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "label_method": "synthetic examples and AI-proposed labels reviewed by the user",
        "model_sha256": settings.embedding_model_sha256,
        "runtime_sha256": settings.embedding_runtime_sha256,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "index_tokenizer": "conservative UTF-8 byte count, not the model tokenizer",
        "rows": rows,
    }
    output = Path(os.environ["REPOPILOT_EVALUATION_OUTPUT"])
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
