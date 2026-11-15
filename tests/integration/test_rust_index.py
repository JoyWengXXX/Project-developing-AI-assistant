"""Rust 索引的 PostgreSQL 與 API 驗證。"""

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build


@pytest.fixture
def rust_snapshot(db_engine: Engine, clean_repositories: None, settings: Settings) -> int:
    with db_engine.begin() as connection:
        repository = int(
            connection.execute(
                text(
                    "INSERT INTO repositories (name, canonical_local_path) "
                    "VALUES ('r', '/r') RETURNING id"
                )
            ).scalar_one()
        )
    snapshot = add_snapshot(db_engine, repository)
    add_files(
        db_engine,
        snapshot,
        {
            "src/lib.rs": "use std::io::Read;\nmod api { pub struct Server {} }\n",
            "src/main.go": "package demo\nfunc main() {}\n",
        },
    )
    with Session(db_engine, expire_on_commit=False) as session:
        build(db_engine, session, snapshot, settings)
    return snapshot


def test_rust_index_search_and_use(client: TestClient, rust_snapshot: int) -> None:
    index = client.get(f"/api/v1/snapshots/{rust_snapshot}/index")
    assert index.status_code == 200
    assert index.json()["counts"]["files_by_language"] == {"rust": 1, "go": 1}
    results = client.get(
        f"/api/v1/snapshots/{rust_snapshot}/search/symbols",
        params={"q": "Server", "language": "rust", "kind": "struct"},
    )
    assert results.status_code == 200
    item = results.json()["items"][0]
    assert (item["qualified_name"], item["start_line"], item["path"]) == (
        "api.Server",
        2,
        "src/lib.rs",
    )
    detail = client.get(f"/api/v1/snapshots/{rust_snapshot}/symbols/{item['symbol_id']}")
    assert [(ref["kind"], ref["target"]) for ref in detail.json()["imports"]] == [
        ("rust_use", "std::io::Read")
    ]


def test_rust_index_prevents_downgrade_without_deleting_data(
    db_engine: Engine, rust_snapshot: int, test_database_url: str
) -> None:
    with pytest.raises(IntegrityError):
        command.downgrade(alembic_config(test_database_url), "0011")
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0012"
        assert (
            connection.scalar(text("SELECT count(*) FROM indexed_files WHERE language = 'rust'"))
            == 1
        )
