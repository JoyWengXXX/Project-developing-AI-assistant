"""TypeScript／JavaScript 混合快照的 PostgreSQL 與 API 驗證。"""

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
def mixed_snapshot(db_engine: Engine, clean_repositories: None, settings: Settings) -> int:
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
            "auth.ts": 'import { hash as h } from "crypto";\nexport type UserId = string;\n'
            "export function verify() {}\n",
            "Card.tsx": "export const Card = () => <div>Hello</div>;\n",
            "module.mts": "export function moduleTs() {}\n",
            "common.cts": "export function commonTs() {}\n",
            "view.jsx": "export const View = () => <div>OK</div>;\n",
            "app.MJS": "export class Service { run() {} }\n",
            "common.cjs": "function commonJs() {}\n",
            "broken.js": "function okay() {}\nfunction broken( {\n",
            "script.py": "def helper(): pass\n",
            "other.cs": "class Other { }\n",
            "unrelated.txt": "not code",
        },
    )
    with Session(db_engine, expire_on_commit=False) as session:
        build(db_engine, session, snapshot, settings)
    return snapshot


def test_mixed_languages_and_errors_are_visible_through_api(
    client: TestClient, mixed_snapshot: int
) -> None:
    status = client.get(f"/api/v1/snapshots/{mixed_snapshot}/index")
    assert status.status_code == 200
    counts = status.json()["counts"]
    assert counts["files_by_language"] == {
        "typescript": 4,
        "javascript": 4,
        "python": 1,
        "csharp": 1,
    }
    response = client.get(
        f"/api/v1/snapshots/{mixed_snapshot}/index/files", params={"has_problems": True}
    )
    assert response.status_code == 200
    assert [(item["path"], item["parse_status"]) for item in response.json()["items"]] == [
        ("broken.js", "parsed_with_errors")
    ]
    symbols = client.get(f"/api/v1/snapshots/{mixed_snapshot}/symbols")
    assert symbols.status_code == 200
    items = symbols.json()["items"]
    alias = next(item for item in items if item["name"] == "UserId")
    assert (alias["language"], alias["kind"], alias["start_line"]) == (
        "typescript",
        "type_alias",
        2,
    )
    detail = client.get(f"/api/v1/snapshots/{mixed_snapshot}/symbols/{alias['symbol_id']}")
    assert detail.status_code == 200
    assert [(item["kind"], item["target"], item["alias"]) for item in detail.json()["imports"]] == [
        ("script_import", "crypto", "h")
    ]


def test_symbol_search_accepts_typescript_and_type_alias(
    client: TestClient, mixed_snapshot: int
) -> None:
    response = client.get(
        f"/api/v1/snapshots/{mixed_snapshot}/search/symbols",
        params={"q": "User", "language": "typescript", "kind": "type_alias"},
    )
    assert response.status_code == 200
    assert [(item["name"], item["language"]) for item in response.json()["items"]] == [
        ("UserId", "typescript")
    ]


def test_downgrade_refuses_to_delete_new_index_data(
    db_engine: Engine, mixed_snapshot: int, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    with pytest.raises(IntegrityError):
        command.downgrade(config, "0008")
    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
        assert (
            connection.scalar(
                text("SELECT count(*) FROM indexed_files WHERE language = 'typescript'")
            )
            == 4
        )


def test_empty_index_schema_can_downgrade_and_upgrade(
    clean_repositories: None, test_database_url: str
) -> None:
    config = alembic_config(test_database_url)
    command.downgrade(config, "0008")
    command.upgrade(config, "head")
