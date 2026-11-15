"""Go 索引的 PostgreSQL 與 API 驗證。"""

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from repopilot.config import Settings
from tests.integration.conftest import alembic_config
from tests.integration.snapshot_helpers import add_snapshot
from tests.integration.test_snapshot_indexing import add_files, build


@pytest.fixture
def go_snapshot(db_engine: Engine, clean_repositories: None, settings: Settings) -> int:
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
            "src/main.go": 'package demo\nimport alias "net/http"\n'
            "type Server struct {}\nfunc Serve() {}\n",
            "src/main.py": "def main(): pass\n",
        },
    )
    with Session(db_engine, expire_on_commit=False) as session:
        build(db_engine, session, snapshot, settings)
    return snapshot


def test_go_index_search_and_import(client: TestClient, go_snapshot: int) -> None:
    index = client.get(f"/api/v1/snapshots/{go_snapshot}/index")
    assert index.status_code == 200
    assert index.json()["counts"]["files_by_language"] == {"go": 1, "python": 1}
    results = client.get(
        f"/api/v1/snapshots/{go_snapshot}/search/symbols",
        params={"q": "Server", "language": "go", "kind": "struct"},
    )
    assert results.status_code == 200
    item = results.json()["items"][0]
    assert (item["name"], item["start_line"], item["path"]) == ("Server", 3, "src/main.go")
    detail = client.get(f"/api/v1/snapshots/{go_snapshot}/symbols/{item['symbol_id']}")
    assert [(ref["kind"], ref["target"], ref["alias"]) for ref in detail.json()["imports"]] == [
        ("go_import", "net/http", "alias")
    ]


def test_go_index_prevents_downgrade_without_deleting_data(
    db_engine: Engine, go_snapshot: int, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    with pytest.raises(IntegrityError):
        command.downgrade(config, "0010")
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert (
            connection.scalar(text("SELECT count(*) FROM indexed_files WHERE language = 'go'")) == 1
        )
