"""Java 結構索引的 PostgreSQL 與 API 驗證。"""

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
def java_snapshot(db_engine: Engine, clean_repositories: None, settings: Settings) -> int:
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
            "src/Service.java": "package demo;\n"
            "import static java.util.Collections.emptyList;\n"
            "class Service { void run() {} }\n",
            "src/Broken.JAVA": "class Good {}\nclass Broken {\n",
            "src/script.ts": "export const test = () => true;\n",
            "src/main.py": "def main(): pass\n",
        },
    )
    with Session(db_engine, expire_on_commit=False) as session:
        build(db_engine, session, snapshot, settings)
    return snapshot


def test_java_index_symbols_imports_errors_and_search(
    client: TestClient, java_snapshot: int
) -> None:
    index = client.get(f"/api/v1/snapshots/{java_snapshot}/index")
    assert index.status_code == 200
    assert index.json()["counts"]["files_by_language"] == {"java": 2, "typescript": 1, "python": 1}
    problems = client.get(
        f"/api/v1/snapshots/{java_snapshot}/index/files", params={"has_problems": True}
    )
    assert [(item["path"], item["parse_status"]) for item in problems.json()["items"]] == [
        ("src/Broken.JAVA", "parsed_with_errors")
    ]
    results = client.get(
        f"/api/v1/snapshots/{java_snapshot}/search/symbols",
        params={"q": "Service", "language": "java", "kind": "class"},
    )
    assert results.status_code == 200
    item = results.json()["items"][0]
    assert (item["name"], item["start_line"], item["path"]) == ("Service", 3, "src/Service.java")
    detail = client.get(f"/api/v1/snapshots/{java_snapshot}/symbols/{item['symbol_id']}")
    assert [(ref["kind"], ref["target"]) for ref in detail.json()["imports"]] == [
        ("java_static_import", "java.util.Collections.emptyList")
    ]


def test_java_index_prevents_downgrade_without_deleting_data(
    db_engine: Engine, java_snapshot: int, test_database_url: str
) -> None:
    with pytest.raises(IntegrityError):
        command.downgrade(alembic_config(test_database_url), "0009")
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0010"
        assert (
            connection.scalar(text("SELECT count(*) FROM indexed_files WHERE language = 'java'"))
            == 2
        )
