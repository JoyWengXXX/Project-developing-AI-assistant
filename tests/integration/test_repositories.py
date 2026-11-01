import os
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from repopilot.config import Settings
from repopilot.db.models import Repository
from repopilot.main import create_app
from repopilot.services.repositories import register_repository
from tests.git_helpers import init_repo

URL = "/api/v1/repositories"


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    directory = tmp_path / "repos"
    directory.mkdir()
    return directory


@pytest.fixture
def clean_repositories(db_engine: Engine) -> None:
    # 只清除名稱含 test 的測試資料庫（由 db_engine fixture 保證）
    with db_engine.begin() as connection:
        connection.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))


@pytest.fixture
def settings(test_database_url: str, roots: Path) -> Settings:
    return Settings(database_url=SecretStr(test_database_url), repository_roots=[roots])


@pytest.fixture
def client(settings: Settings, clean_repositories: None) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _count(db_engine: Engine) -> int:
    with Session(db_engine) as session:
        return session.scalar(select(func.count()).select_from(Repository)) or 0


def test_register_new_repository_returns_201(client: TestClient, roots: Path) -> None:
    repo = init_repo(roots / "demo")

    response = client.post(URL, json={"path": str(repo)})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "demo"
    assert body["canonical_local_path"] == str(repo.resolve())
    assert body["id"] >= 1
    assert body["created_at"]


def test_register_uses_custom_name(client: TestClient, roots: Path) -> None:
    repo = init_repo(roots / "demo")

    response = client.post(URL, json={"path": str(repo), "name": "  My Demo  "})

    assert response.json()["name"] == "My Demo"


def test_duplicate_registration_returns_200_and_same_record(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "demo")
    first = client.post(URL, json={"path": str(repo), "name": "first"})

    second = client.post(URL, json={"path": str(repo), "name": "other"})

    assert second.status_code == 200
    assert second.json() == first.json()
    assert _count(db_engine) == 1


def test_different_path_spellings_are_deduplicated(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repo = init_repo(roots / "demo")
    spellings = [
        str(repo),
        str(repo) + os.sep,
        str(roots / "demo" / ".." / "demo"),
    ]
    if os.name == "nt":
        spellings.append(str(repo).upper())

    statuses = [client.post(URL, json={"path": p}).status_code for p in spellings]

    assert statuses[0] == 201
    assert set(statuses[1:]) == {200}
    assert _count(db_engine) == 1


@pytest.mark.parametrize("kind", ["outside", "plain", "subdir", "missing", "relative", "empty"])
def test_invalid_paths_return_422_and_store_nothing(
    client: TestClient, roots: Path, tmp_path: Path, db_engine: Engine, kind: str
) -> None:
    repo = init_repo(roots / "demo", files={"src/a.py": "x\n"})
    plain = roots / "plain"
    plain.mkdir()
    outside = init_repo(tmp_path / "outside")
    paths = {
        "outside": str(outside),
        "plain": str(plain),
        "subdir": str(repo / "src"),
        "missing": str(roots / "missing"),
        "relative": "demo",
        "empty": "",
    }

    response = client.post(URL, json={"path": paths[kind]})

    assert response.status_code == 422
    assert _count(db_engine) == 0


def test_invalid_path_error_uses_unified_format(client: TestClient, tmp_path: Path) -> None:
    response = client.post(URL, json={"path": str(tmp_path)})

    error = response.json()["error"]
    assert error["code"] == "invalid_repository_path"
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_outside_root_returns_invalid_repository_path_code(
    client: TestClient, tmp_path: Path
) -> None:
    outside = init_repo(tmp_path / "outside")

    response = client.post(URL, json={"path": str(outside)})

    assert response.json()["error"]["code"] == "invalid_repository_path"


def test_missing_path_field_returns_validation_error(client: TestClient) -> None:
    response = client.post(URL, json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unknown_field_is_rejected(client: TestClient, roots: Path) -> None:
    repo = init_repo(roots / "demo")

    response = client.post(URL, json={"path": str(repo), "id": 99})

    assert response.status_code == 422


def test_list_and_get(client: TestClient, roots: Path) -> None:
    a = client.post(URL, json={"path": str(init_repo(roots / "a"))}).json()
    b = client.post(URL, json={"path": str(init_repo(roots / "b"))}).json()

    listing = client.get(URL)
    single = client.get(f"{URL}/{b['id']}")

    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [a["id"], b["id"]]
    assert single.status_code == 200
    assert single.json() == b


def test_list_is_empty_initially(client: TestClient) -> None:
    assert client.get(URL).json() == {"items": []}


def test_get_unknown_repository_returns_404(client: TestClient) -> None:
    response = client.get(f"{URL}/999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "repository_not_found"


def test_get_non_integer_id_returns_422(client: TestClient) -> None:
    assert client.get(f"{URL}/abc").status_code == 422


def test_git_timeout_returns_504(
    client: TestClient, roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(roots / "demo")

    def fake_run(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("repopilot.git.runner.subprocess.run", fake_run)

    response = client.post(URL, json={"path": str(repo)})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "git_timeout"


def test_untracked_changes_in_working_tree_do_not_block_registration(
    client: TestClient, roots: Path
) -> None:
    repo = init_repo(roots / "demo")
    (repo / "untracked.txt").write_text("x", encoding="utf-8")

    assert client.post(URL, json={"path": str(repo)}).status_code == 201


def test_database_rejects_duplicate_path_with_named_constraint(
    settings: Settings, clean_repositories: None, db_engine: Engine
) -> None:
    with Session(db_engine) as session:
        session.add(Repository(name="a", canonical_local_path="/x"))
        session.commit()
        session.add(Repository(name="b", canonical_local_path="/x"))
        with pytest.raises(IntegrityError) as exc_info:
            session.commit()

    assert "uq_repositories_canonical_local_path" in str(exc_info.value.orig)


def test_concurrent_registration_creates_single_row(
    settings: Settings, clean_repositories: None, db_engine: Engine, roots: Path
) -> None:
    repo = init_repo(roots / "demo")
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    workers = 8
    barrier = threading.Barrier(workers)

    def attempt() -> tuple[int, bool]:
        barrier.wait()
        with factory() as session:
            repository, created = register_repository(
                session,
                raw_path=str(repo),
                name=None,
                roots=settings.repository_roots,
                git_timeout=30,
            )
            return repository.id, created

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: attempt(), range(workers)))

    assert len({repo_id for repo_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert _count(db_engine) == 1
