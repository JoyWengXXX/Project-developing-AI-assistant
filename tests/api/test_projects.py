import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repopilot.config import Settings
from repopilot.main import create_app


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    directory = tmp_path / "projects"
    directory.mkdir()
    return directory


@pytest.fixture
def client(roots: Path) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(repository_roots=[roots]))) as test_client:
        yield test_client


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def link_dir(link: Path, target: Path) -> None:
    """建立目錄連結；Windows 沒有 symlink 權限時改用 junction。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlink 無法在這個環境建立")
        result = subprocess.run(  # noqa: S603
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("symlink 與 junction 都無法在這個環境建立")


def init(client: TestClient, path: Path | str) -> tuple[int, dict[str, Any]]:
    response = client.post("/api/v1/projects/init", json={"project_path": str(path)})
    return response.status_code, response.json()


def test_python_fastapi_project_is_detected(client: TestClient, roots: Path) -> None:
    project = roots / "app"
    write(
        project / "requirements.txt", "fastapi>=0.110\nSQLAlchemy[asyncio]==2.0\npytest\npsycopg\n"
    )
    write(project / "src" / "main.py", "print('x')")
    write(project / "tests" / "test_main.py", "")

    status, body = init(client, project)

    assert status == 200
    config = body["project_config"]
    assert config["language"] == "python"
    assert config["framework"] == "fastapi"
    assert config["orm"] == "sqlalchemy"
    assert config["test_framework"] == "pytest"
    assert config["database"] == "postgresql"
    assert config["dependencies"]["fastapi"] == ">=0.110"
    assert config["root_path"] == str(project.resolve())
    assert config["detected_at"]
    assert body["confidence"] == "high"
    assert body["manifests"] == ["requirements.txt"]
    assert body["language_files"] == {"python": 2}
    assert body["directories"] == ["src", "tests"]


@pytest.mark.parametrize(
    ("manifest", "content", "source", "language", "framework"),
    [
        (
            "package.json",
            '{"dependencies": {"@nestjs/core": "^10", "express": "^4"}, '
            '"devDependencies": {"typescript": "^5", "jest": "^29"}}',
            "src/main.ts",
            "typescript",
            "nestjs",
        ),
        (
            "package.json",
            '{"dependencies": {"express": "^4"}}',
            "index.js",
            "javascript",
            "express",
        ),
        (
            "pom.xml",
            "<project><parent><artifactId>spring-boot-starter-parent</artifactId></parent></project>",
            "src/main/java/App.java",
            "java",
            "spring-boot",
        ),
        (
            "go.mod",
            "module example.com/app\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9\n)\n",
            "main.go",
            "go",
            "gin",
        ),
        (
            "Cargo.toml",
            '[package]\nname = "app"\n\n[dependencies]\naxum = "0.7"\n',
            "src/main.rs",
            "rust",
            "axum",
        ),
        (
            "pyproject.toml",
            '[project]\nname = "app"\ndependencies = ["flask>=3"]\n',
            "app.py",
            "python",
            "flask",
        ),
    ],
)
def test_framework_detection_per_ecosystem(
    client: TestClient,
    roots: Path,
    manifest: str,
    content: str,
    source: str,
    language: str,
    framework: str,
) -> None:
    project = roots / "app"
    write(project / manifest, content)
    write(project / source)

    status, body = init(client, project)

    assert status == 200
    assert body["project_config"]["language"] == language
    assert body["project_config"]["framework"] == framework
    assert body["confidence"] == "high"


def test_unknown_framework_gives_medium_confidence(client: TestClient, roots: Path) -> None:
    project = roots / "app"
    write(project / "requirements.txt", "requests\n")
    write(project / "main.py")

    status, body = init(client, project)

    assert status == 200
    assert body["project_config"]["framework"] is None
    assert body["confidence"] == "medium"


def test_dependency_directories_are_not_scanned(client: TestClient, roots: Path) -> None:
    project = roots / "app"
    write(project / "package.json", '{"dependencies": {"express": "^4"}}')
    write(project / "index.js")
    for index in range(5):
        write(project / "node_modules" / "dep" / f"file{index}.ts")

    _, body = init(client, project)

    assert body["language_files"] == {"javascript": 1}
    assert body["project_config"]["language"] == "javascript"


def test_missing_dependency_file_reports_detected_language(client: TestClient, roots: Path) -> None:
    project = roots / "app"
    write(project / "main.py")
    write(project / "util.py")

    status, body = init(client, project)

    assert status == 422
    assert body["error"]["code"] == "project_dependencies_not_found"
    assert body["error"]["details"]["detected_language"] == "python"
    assert any("requirements.txt" in item for item in body["error"]["details"]["suggestions"])


def test_empty_directory_has_no_detected_language(client: TestClient, roots: Path) -> None:
    project = roots / "empty"
    project.mkdir()

    status, body = init(client, project)

    assert status == 422
    assert body["error"]["code"] == "project_dependencies_not_found"
    assert body["error"]["details"]["detected_language"] is None
    assert len(body["error"]["details"]["suggestions"]) > 1


def test_relative_and_missing_paths_are_rejected(client: TestClient, roots: Path) -> None:
    for path in ("relative/path", str(roots / "missing"), "  "):
        status, body = init(client, path)
        assert status == 400
        assert body["error"]["code"] == "project_path_invalid"


def test_file_path_is_rejected(client: TestClient, roots: Path) -> None:
    write(roots / "file.txt", "x")

    status, body = init(client, roots / "file.txt")

    assert (status, body["error"]["code"]) == (400, "project_path_invalid")


def test_path_outside_roots_is_rejected(client: TestClient, roots: Path) -> None:
    sibling = roots.parent / "projects-evil"
    write(sibling / "requirements.txt", "fastapi\n")

    status, body = init(client, sibling)

    assert (status, body["error"]["code"]) == (400, "project_path_invalid")


def test_parent_traversal_out_of_roots_is_rejected(client: TestClient, roots: Path) -> None:
    outside = roots.parent / "outside"
    write(outside / "requirements.txt", "fastapi\n")

    status, body = init(client, roots / ".." / "outside")

    assert (status, body["error"]["code"]) == (400, "project_path_invalid")


def test_symlinked_project_is_rejected(client: TestClient, roots: Path, tmp_path: Path) -> None:
    real = roots / "real"
    write(real / "requirements.txt", "fastapi\n")
    link = roots / "link"
    link_dir(link, real)

    status, body = init(client, link)

    assert (status, body["error"]["code"]) == (400, "project_path_invalid")


def test_path_through_symlinked_parent_is_rejected(client: TestClient, roots: Path) -> None:
    real_parent = roots / "real"
    write(real_parent / "app" / "requirements.txt", "fastapi\n")
    link = roots / "link"
    link_dir(link, real_parent)

    status, body = init(client, link / "app")

    assert (status, body["error"]["code"]) == (400, "project_path_invalid")


def test_symlink_pointing_outside_roots_is_not_followed(client: TestClient, roots: Path) -> None:
    outside = roots.parent / "outside"
    write(outside / "main.py")
    project = roots / "app"
    write(project / "requirements.txt", "fastapi\n")
    link_dir(project / "linked", outside)

    status, body = init(client, project)

    assert status == 200
    assert body["language_files"] == {}


@pytest.mark.skipif(os.name == "nt", reason="Windows 的目錄權限不由 os.access 反映")
def test_unreadable_directory_is_rejected(client: TestClient, roots: Path) -> None:
    project = roots / "locked"
    write(project / "requirements.txt", "fastapi\n")
    project.chmod(0)
    try:
        status, body = init(client, project)
    finally:
        project.chmod(0o755)

    assert (status, body["error"]["code"]) == (400, "project_path_invalid")


def test_unknown_request_fields_are_rejected(client: TestClient, roots: Path) -> None:
    response = client.post(
        "/api/v1/projects/init", json={"project_path": str(roots), "install": True}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_detection_does_not_modify_the_project(client: TestClient, roots: Path) -> None:
    project = roots / "app"
    write(project / "requirements.txt", "fastapi\n")
    write(project / "main.py")
    before = sorted(str(p.relative_to(project)) for p in project.rglob("*"))

    init(client, project)

    assert sorted(str(p.relative_to(project)) for p in project.rglob("*")) == before
