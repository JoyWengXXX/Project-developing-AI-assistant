"""切片 5d-1：`GET /repositories/{id}/overview`（D35）。

PostgreSQL ＋ 真實本機 Git。GitHub 來源以本機 bare repo 模擬遠端（file://），GitHub API 以
`httpx.MockTransport` 模擬，不連真實 GitHub。重點不變量：唯讀（絕不匯入、不寫入）、查詢次數為
小常數（無 N+1）、不可信的 manifest／README 不會使 API 失敗，且不洩漏被排除檔案的內容。
"""

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, event, insert, text
from sqlalchemy.orm import Session

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.db.models import Snapshot, SnapshotFile
from repopilot.main import create_app
from repopilot.services.import_profile import ImportProfile
from tests.git_helpers import RemoteRepo, commit_files, commit_special_entries, git, init_repo
from tests.github_helpers import FakeGitHub

URL = "/api/v1/repositories"
GITHUB_URL = f"{URL}/github"


# --- fixtures（與 test_repository_history 相同的組合：本機 repo、本機 bare repo、假 GitHub）---


@pytest.fixture
def managed_dir(tmp_path: Path) -> Path:
    return tmp_path / "managed"


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    directory = tmp_path / "remotes"
    directory.mkdir()
    return directory


@pytest.fixture
def settings(test_database_url: str, roots: Path, managed_dir: Path, remotes: Path) -> Settings:
    return Settings(
        database_url=SecretStr(test_database_url),
        repository_roots=[roots],
        managed_repos_dir=managed_dir,
        github_clone_base_url=remotes.as_uri(),
        github_clone_timeout_seconds=60,
    )


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def app(settings: Settings, clean_repositories: None, fake_github: FakeGitHub) -> FastAPI:
    application = create_app(settings)
    application.dependency_overrides[get_github_client] = fake_github.client
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def remote(remotes: Path, fake_github: FakeGitHub) -> RemoteRepo:
    fake_github.add_repo("octocat/Hello-World")
    return RemoteRepo(remotes, "octocat", "Hello-World", files={"README.md": "# hi\n"})


# --- helpers ---


def _register(client: TestClient, path: Path) -> int:
    response = client.post(URL, json={"path": str(path)})
    assert response.status_code in (200, 201), response.text
    return int(response.json()["id"])


def _register_github(client: TestClient) -> int:
    response = client.post(GITHUB_URL, json={"repo": "octocat/Hello-World"})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _import(client: TestClient, repository_id: int, revision: str = "HEAD") -> dict[str, Any]:
    response = client.post(f"{URL}/{repository_id}/imports", json={"revision": revision})
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


def _overview(client: TestClient, repository_id: int) -> Any:
    return client.get(f"{URL}/{repository_id}/overview")


def _ok(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _error_code(response: Any) -> str:
    code = response.json()["error"]["code"]
    assert isinstance(code, str)
    return code


def _analyzed_overview(client: TestClient, repo: Path) -> dict[str, Any]:
    """註冊並匯入 HEAD，回傳 overview（analyzed 必為 true）。"""
    repository_id = _register(client, repo)
    _import(client, repository_id)
    body = _ok(_overview(client, repository_id))
    assert body["analyzed"] is True
    return body


def _project_info(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """project_info 依 path 索引（單一檔案的測試比較好讀）。"""
    return {item["path"]: item for item in body["project_info"]}


def _make_repo(roots: Path, files: dict[str, str | bytes], name: str = "demo") -> Path:
    repo = init_repo(roots / name, files={"seed.txt": "seed\n"})
    commit_files(repo, files, message="add files")
    return repo


def _update_row(db_engine: Engine, repository_id: int, **values: str) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    with db_engine.begin() as connection:
        connection.execute(
            text(f"UPDATE repositories SET {assignments} WHERE id = :id"),  # noqa: S608
            {**values, "id": repository_id},
        )


def _table_counts(db_engine: Engine) -> dict[str, int]:
    with db_engine.connect() as connection:
        return {
            table: int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
            for table in ("repositories", "snapshots", "snapshot_files")
        }


def _record_git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command and command[0] == "git" and command[1] == "-C":
            calls.append(list(command[3:]))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("repopilot.git.runner.subprocess.run", spy)
    return calls


def _subcommands(calls: list[list[str]]) -> set[str]:
    names: set[str] = set()
    for args in calls:
        index = 0
        while args[index] == "-c":
            index += 2
        names.add(args[index])
    return names


class _Statements:
    """記錄某個 engine 執行的 SQL 與參數（用來斷言查詢次數與唯讀）。"""

    def __init__(self, app: FastAPI) -> None:
        self.engine: Engine = app.state.session_factory.kw["bind"]
        self.sql: list[str] = []
        self.parameters: list[Any] = []

    def _record(self, *args: Any) -> None:
        self.sql.append(str(args[2]))
        self.parameters.append(args[3])

    def __enter__(self) -> "_Statements":
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)

    def count(self, fragment: str) -> int:
        return sum(1 for statement in self.sql if fragment in statement)

    def writes(self) -> list[str]:
        return [
            s for s in self.sql if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]


# --- 未分析 → 已分析 ---


def test_unanalyzed_repository_returns_only_the_head(client: TestClient, roots: Path) -> None:
    repo = init_repo(roots / "demo", files={"README.md": "hello\n"})
    sha = git(repo, "rev-parse", "HEAD")
    repository_id = _register(client, repo)

    body = _ok(_overview(client, repository_id))

    assert body == {
        "repository_id": repository_id,
        "default_ref": "refs/heads/main",
        "head": {
            "sha": sha,
            "subject": "initial",
            "author_name": "Test",
            "authored_at": body["head"]["authored_at"],
        },
        "snapshot": None,
        "stats": None,
        "languages": [],
        "readme": None,
        "project_info": [],
        "analyzed": False,
    }
    assert body["head"]["authored_at"].endswith("Z")
    assert "test@example.invalid" not in str(body)  # 不暴露 email


CSHARP_FILES: dict[str, str | bytes] = {
    "README.md": "# Demo\n\nA .NET sample.\n",
    "Demo.sln": "Microsoft Visual Studio Solution File\n",
    "src/App/App.csproj": (
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
        '  <ItemGroup><PackageReference Include="Serilog" Version="3.1.1" /></ItemGroup>\n'
        "</Project>\n"
    ),
    "src/App/Program.cs": "Console.WriteLine(1);\n",
    "src/App/appsettings.json": '{"a": 1}\n',
    "docs/guide.md": "guide\n",
    ".env": "SECRET=FAKE_KEY_FOR_TEST\n",  # 預設被排除：沒有內容
    "assets/logo.png": b"\x89PNG\x00\x01binary",  # 二進位：skipped
}


def test_analyzed_csharp_project_has_stats_languages_readme_and_project_info(
    client: TestClient, roots: Path
) -> None:
    repo = _make_repo(roots, CSHARP_FILES)
    repository_id = _register(client, repo)
    snapshot = _import(client, repository_id)

    response = _overview(client, repository_id)
    body = _ok(response)

    imported = {p: c for p, c in CSHARP_FILES.items() if p not in (".env", "assets/logo.png")}
    imported["seed.txt"] = "seed\n"
    assert body["analyzed"] is True
    assert body["snapshot"] == {
        "id": snapshot["id"],
        "commit_sha": snapshot["commit_sha"],
        "status": "completed",
    }
    assert body["stats"] == {
        "imported_files": len(imported),
        "skipped_files": 2,  # .env（excluded）與 logo.png（binary）
        "total_bytes": sum(len(str(c).encode()) for c in imported.values()),
    }
    by_language = {entry["language"]: entry["files"] for entry in body["languages"]}
    assert by_language == {"MSBuild": 2, "Markdown": 2, "C#": 1, "JSON": 1, "其他": 1}
    assert body["languages"][0]["language"] == "MSBuild"  # 同為 2 個檔案時依名稱
    assert body["languages"][-1]["language"] == "其他"  # seed.txt
    assert body["readme"] == {
        "path": "README.md",
        "text": "# Demo\n\nA .NET sample.\n",
        "truncated": False,
    }
    assert body["project_info"] == [
        {
            "kind": "csproj",
            "path": "src/App/App.csproj",
            "name": "App",
            "version": None,
            "dependencies": ["Serilog@3.1.1"],
            "truncated": False,
            "target_frameworks": ["net8.0"],
            "skipped_reason": None,
            "parse_error": None,
        }
    ]
    # 被排除檔案本來就沒有內容；回應不含其內容
    assert "FAKE_KEY_FOR_TEST" not in response.text


def test_overview_reports_symlinks_and_submodules_as_skipped(
    client: TestClient, roots: Path
) -> None:
    repo = init_repo(roots / "demo", files={"a.py": "x = 1\n"})
    commit_special_entries(repo, symlinks={"link": "a.py"}, gitlinks={"vendor/lib": "1" * 40})

    body = _analyzed_overview(client, repo)

    assert body["stats"]["imported_files"] == 1
    assert body["stats"]["skipped_files"] == 2
    assert [entry["language"] for entry in body["languages"]] == ["Python"]


def test_analyzed_response_has_the_documented_shape(client: TestClient, roots: Path) -> None:
    body = _analyzed_overview(client, _make_repo(roots, {"app.py": "x = 1\n"}))

    assert set(body) == {
        "repository_id",
        "default_ref",
        "head",
        "snapshot",
        "stats",
        "languages",
        "readme",
        "project_info",
        "analyzed",
    }
    assert set(body["head"]) == {"sha", "subject", "author_name", "authored_at"}
    assert set(body["stats"]) == {"imported_files", "skipped_files", "total_bytes"}
    assert set(body["languages"][0]) == {"language", "files", "bytes"}
    assert body["head"]["subject"] == "add files"
    assert body["snapshot"]["commit_sha"] == body["head"]["sha"]


def test_overview_follows_the_head_commit_and_old_snapshots_do_not_count(
    client: TestClient, roots: Path
) -> None:
    repo = _make_repo(roots, {"app.py": "x = 1\n"})
    repository_id = _register(client, repo)
    _import(client, repository_id)
    assert _ok(_overview(client, repository_id))["analyzed"] is True

    new_sha = commit_files(repo, {"more.cs": "class A {}\n"}, message="second")
    body = _ok(_overview(client, repository_id))

    assert body["analyzed"] is False
    assert body["snapshot"] is None
    assert body["head"]["sha"] == new_sha
    assert body["head"]["subject"] == "second"


def test_overview_is_unanalyzed_when_the_import_profile_changed(
    client: TestClient, roots: Path, settings: Settings
) -> None:
    repository_id = _register(client, _make_repo(roots, {"app.py": "x = 1\n"}))
    _import(client, repository_id)
    assert _ok(_overview(client, repository_id))["analyzed"] is True

    changed = settings.model_copy(update={"import_max_files": settings.import_max_files + 1})
    assert (
        ImportProfile.from_settings(changed).hash() != ImportProfile.from_settings(settings).hash()
    )
    with TestClient(create_app(changed)) as other_client:
        body = _ok(_overview(other_client, repository_id))

    assert (body["analyzed"], body["snapshot"], body["stats"]) == (False, None, None)
    assert body["head"] is not None


def test_failed_or_importing_snapshots_do_not_count_as_analyzed(
    client: TestClient, roots: Path, db_engine: Engine, settings: Settings
) -> None:
    repo = _make_repo(roots, {"app.py": "x = 1\n"})
    repository_id = _register(client, repo)
    sha = git(repo, "rev-parse", "HEAD")

    with Session(db_engine) as session:
        session.add(
            Snapshot(
                repository_id=repository_id,
                commit_sha=sha,
                import_profile_hash=ImportProfile.from_settings(settings).hash(),
                status="failed",
            )
        )
        session.commit()

    assert _ok(_overview(client, repository_id))["analyzed"] is False


# --- 預設分支 ---


def test_empty_repository_has_no_head_and_is_not_an_error(client: TestClient, roots: Path) -> None:
    empty = roots / "empty"
    empty.mkdir()
    git(empty, "init", "--quiet", "--initial-branch=main")
    repository_id = _register(client, empty)

    body = _ok(_overview(client, repository_id))

    assert body == {
        "repository_id": repository_id,
        "default_ref": None,
        "head": None,
        "snapshot": None,
        "stats": None,
        "languages": [],
        "readme": None,
        "project_info": [],
        "analyzed": False,
    }


def test_detached_head_uses_head_but_has_no_default_ref(client: TestClient, roots: Path) -> None:
    repo = _make_repo(roots, {"app.py": "x = 1\n"})
    git(repo, "checkout", "--quiet", "--detach")
    detached = commit_files(repo, {"extra.go": "package x\n"}, message="detached work")
    repository_id = _register(client, repo)

    before = _ok(_overview(client, repository_id))
    _import(client, repository_id)
    after = _ok(_overview(client, repository_id))

    assert (before["default_ref"], before["head"]["sha"], before["analyzed"]) == (
        None,
        detached,
        False,
    )
    assert (after["analyzed"], after["snapshot"]["commit_sha"]) == (True, detached)
    assert {entry["language"] for entry in after["languages"]} == {"Python", "Go", "其他"}


def test_local_source_follows_the_checked_out_branch(client: TestClient, roots: Path) -> None:
    repo = _make_repo(roots, {"app.py": "x = 1\n"})
    git(repo, "checkout", "--quiet", "-b", "dev")
    dev_sha = commit_files(repo, {"dev.ts": "export {}\n"}, message="on dev")
    repository_id = _register(client, repo)

    body = _ok(_overview(client, repository_id))

    assert body["default_ref"] == "refs/heads/dev"
    assert body["head"]["sha"] == dev_sha


def test_snapshots_of_other_branches_do_not_affect_the_default_branch_overview(
    client: TestClient, roots: Path
) -> None:
    repo = _make_repo(roots, {"app.py": "x = 1\n"})
    main_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--quiet", "-b", "feature")
    feature_sha = commit_files(repo, {"big.cs": "class A {}\n"}, message="feature")
    git(repo, "checkout", "--quiet", "main")
    repository_id = _register(client, repo)

    _import(client, repository_id, feature_sha)
    unanalyzed = _ok(_overview(client, repository_id))
    _import(client, repository_id, main_sha)
    analyzed = _ok(_overview(client, repository_id))

    assert (unanalyzed["analyzed"], unanalyzed["head"]["sha"]) == (False, main_sha)
    assert analyzed["analyzed"] is True
    assert analyzed["snapshot"]["commit_sha"] == main_sha
    assert "C#" not in {entry["language"] for entry in analyzed["languages"]}


# --- GitHub 來源 ---


def test_github_source_uses_origin_default_branch_not_the_local_clone_head(
    client: TestClient, remote: RemoteRepo
) -> None:
    repository_id = _register_github(client)
    first = remote.head()
    new_sha = remote.commit({"app.py": "x = 1\n"}, message="upstream")
    assert client.post(f"{URL}/{repository_id}/fetch").status_code == 200

    before = _ok(_overview(client, repository_id))
    _import(client, repository_id, "origin/main")
    after = _ok(_overview(client, repository_id))

    assert before["default_ref"] == "refs/remotes/origin/main"
    assert before["head"]["sha"] == new_sha != first  # clone 時的本機 HEAD 是 first
    assert before["analyzed"] is False
    assert after["analyzed"] is True
    assert after["snapshot"]["commit_sha"] == new_sha
    assert after["readme"]["text"] == "# hi\n"


def test_github_source_ignores_other_remote_branches_and_local_only_commits(
    client: TestClient, remote: RemoteRepo, managed_dir: Path
) -> None:
    repository_id = _register_github(client)
    main_sha = remote.head()
    clone = managed_dir / "github" / "octocat" / "Hello-World"
    commit_files(clone, {"local_only.rs": "fn main() {}\n"}, message="local only")

    body = _ok(_overview(client, repository_id))

    assert body["head"]["sha"] == main_sha
    assert body["head"]["subject"] == "initial"


def test_github_default_branch_missing_in_the_clone_is_treated_like_no_head(
    client: TestClient, remote: RemoteRepo, fake_github: FakeGitHub
) -> None:
    fake_github.add_repo("octocat/Hello-World", default_branch="renamed-away")
    repository_id = _register_github(client)

    body = _ok(_overview(client, repository_id))

    assert (body["default_ref"], body["head"], body["analyzed"]) == (None, None, False)


# --- README ---


def _readme_of(client: TestClient, roots: Path, files: dict[str, str | bytes]) -> Any:
    return _analyzed_overview(client, _make_repo(roots, files))["readme"]


@pytest.mark.parametrize(
    "name", ["README", "README.md", "README.rst", "README.txt", "README.markdown", "readme.MD"]
)
def test_readme_names_are_matched_case_insensitively(
    client: TestClient, roots: Path, name: str
) -> None:
    readme = _readme_of(client, roots, {name: "content\n"})

    assert readme == {"path": name, "text": "content\n", "truncated": False}


def test_readme_priority_is_fixed(client: TestClient, roots: Path) -> None:
    files: dict[str, str | bytes] = {
        "README.markdown": "markdown",
        "README.txt": "txt",
        "README.rst": "rst",
        "README.md": "md",
    }

    assert _readme_of(client, roots, files)["path"] == "README.md"


def test_nested_readme_is_not_used(client: TestClient, roots: Path) -> None:
    readme = _readme_of(client, roots, {"docs/README.md": "nested", "src/app/readme": "x"})

    assert readme is None


def test_nested_readme_does_not_hide_a_lower_priority_root_readme(
    client: TestClient, roots: Path
) -> None:
    readme = _readme_of(client, roots, {"docs/README.md": "nested", "README.txt": "root"})

    assert readme["path"] == "README.txt"


def test_repository_without_readme_has_null_readme(client: TestClient, roots: Path) -> None:
    assert _readme_of(client, roots, {"app.py": "x = 1\n"}) is None


def test_readme_longer_than_4000_characters_is_cut_and_flagged(
    client: TestClient, roots: Path
) -> None:
    readme = _readme_of(client, roots, {"README.md": "a" * 4001})

    assert (len(readme["text"]), readme["truncated"]) == (4000, True)


def test_readme_of_exactly_4000_characters_is_not_flagged(client: TestClient, roots: Path) -> None:
    readme = _readme_of(client, roots, {"README.md": "a" * 4000})

    assert (len(readme["text"]), readme["truncated"]) == (4000, False)


@pytest.mark.parametrize("unit", ["中", "😀", "é", "a😀中"])
def test_readme_truncation_counts_characters_and_never_splits_multibyte_ones(
    client: TestClient, roots: Path, unit: str
) -> None:
    content = unit * 5000
    readme = _readme_of(client, roots, {"README.md": content})

    assert readme["truncated"] is True
    assert readme["text"] == content[:4000]
    assert "\ufffd" not in readme["text"]
    readme["text"].encode("utf-8")  # 仍可編碼，沒有被切壞的代理字元


def test_readme_that_is_binary_is_null_and_falls_back_to_the_next_candidate(
    client: TestClient, roots: Path
) -> None:
    only_binary = _readme_of(client, roots, {"README.md": b"abc\x00def"})
    assert only_binary is None


def test_binary_readme_falls_back_to_a_lower_priority_text_readme(
    client: TestClient, roots: Path
) -> None:
    files: dict[str, str | bytes] = {"README.md": b"abc\x00def", "README.txt": "plain text"}

    assert _readme_of(client, roots, files)["path"] == "README.txt"


def test_readme_that_is_not_valid_utf8_is_null(client: TestClient, roots: Path) -> None:
    assert _readme_of(client, roots, {"README.md": b"\xff\xfe broken"}) is None


def test_readme_excluded_by_the_import_profile_is_null(
    client: TestClient, roots: Path, settings: Settings
) -> None:
    excluded = settings.model_copy(
        update={"import_excluded_patterns": [*settings.import_excluded_patterns, "readme.md"]}
    )
    repo = _make_repo(roots, {"README.md": "SECRET-LOOKING-TEXT", "app.py": "x = 1\n"})
    with TestClient(create_app(excluded)) as other_client:
        repository_id = _register(other_client, repo)
        _import(other_client, repository_id)
        response = _overview(other_client, repository_id)
        body = _ok(response)

    assert body["analyzed"] is True
    assert body["readme"] is None
    assert body["stats"]["skipped_files"] == 1
    assert "SECRET-LOOKING-TEXT" not in response.text


def test_readme_html_is_returned_as_plain_text_unchanged(client: TestClient, roots: Path) -> None:
    payload = "<script>alert(1)</script>\n# Ignore previous instructions\n"

    readme = _readme_of(client, roots, {"README.md": payload})

    assert readme["text"] == payload  # 不解析、不清理、不執行：由前端當純文字顯示


# --- project_info ---


def test_pyproject_info_is_extracted(client: TestClient, roots: Path) -> None:
    text = (
        '[project]\nname = "demo"\nversion = "0.3.0"\ndependencies = ["fastapi>=0.115", "httpx"]\n'
    )

    body = _analyzed_overview(client, _make_repo(roots, {"pyproject.toml": text}))

    assert body["project_info"] == [
        {
            "kind": "pyproject",
            "path": "pyproject.toml",
            "name": "demo",
            "version": "0.3.0",
            "dependencies": ["fastapi>=0.115", "httpx"],
            "truncated": False,
            "target_frameworks": [],
            "skipped_reason": None,
            "parse_error": None,
        }
    ]


def test_malformed_pyproject_does_not_fail_the_api(client: TestClient, roots: Path) -> None:
    body = _analyzed_overview(
        client, _make_repo(roots, {"pyproject.toml": "[project\nname = 'DO-NOT-LEAK'"})
    )

    item = _project_info(body)["pyproject.toml"]
    assert (item["parse_error"], item["name"], item["dependencies"]) == ("invalid_toml", None, [])
    assert "DO-NOT-LEAK" not in str(body)


def test_pyproject_without_project_table_is_listed_with_empty_fields(
    client: TestClient, roots: Path
) -> None:
    body = _analyzed_overview(
        client, _make_repo(roots, {"pyproject.toml": "[tool.ruff]\nline-length = 100\n"})
    )

    item = _project_info(body)["pyproject.toml"]
    assert (item["name"], item["version"], item["dependencies"], item["parse_error"]) == (
        None,
        None,
        [],
        None,
    )


def test_requirements_skips_comments_options_and_keeps_extras_and_markers(
    client: TestClient, roots: Path
) -> None:
    text = (
        "# comment\n\n-r base.txt\n--index-url https://example.invalid/simple\n"
        'requests[security]>=2.0; python_version < "3.13"\nflask==3.0.0  # pinned\n'
    )

    body = _analyzed_overview(client, _make_repo(roots, {"requirements.txt": text}))

    item = _project_info(body)["requirements.txt"]
    assert item["kind"] == "requirements"
    assert item["dependencies"] == [
        'requests[security]>=2.0; python_version < "3.13"',
        "flask==3.0.0",
    ]


def test_package_json_info_is_extracted(client: TestClient, roots: Path) -> None:
    text = (
        '{"name": "web", "version": "1.0.0", "dependencies": {"react": "^19"},'
        ' "devDependencies": {"vite": "^7"}}'
    )

    body = _analyzed_overview(client, _make_repo(roots, {"package.json": text}))

    item = _project_info(body)["package.json"]
    assert (item["kind"], item["name"], item["version"]) == ("package_json", "web", "1.0.0")
    assert item["dependencies"] == ["react@^19", "vite@^7"]


@pytest.mark.parametrize(
    ("text", "error"),
    [("{broken", "invalid_json"), ("[1, 2]", "not_an_object"), ("", "invalid_json")],
)
def test_malformed_package_json_does_not_fail_the_api(
    client: TestClient, roots: Path, text: str, error: str
) -> None:
    body = _analyzed_overview(client, _make_repo(roots, {"package.json": text, "app.py": "x=1\n"}))

    assert _project_info(body)["package.json"]["parse_error"] == error
    assert body["stats"]["imported_files"] >= 2


def test_manifests_outside_the_root_are_ignored_except_csproj(
    client: TestClient, roots: Path
) -> None:
    files: dict[str, str | bytes] = {
        "sub/pyproject.toml": '[project]\nname = "x"\n',
        "sub/package.json": "{}",
        "sub/requirements.txt": "a",
        "sub/X.csproj": "<Project />",
    }

    body = _analyzed_overview(client, _make_repo(roots, files))

    assert [item["path"] for item in body["project_info"]] == ["sub/X.csproj"]


def test_project_info_is_ordered_pyproject_requirements_package_json_then_csproj(
    client: TestClient, roots: Path
) -> None:
    files: dict[str, str | bytes] = {
        "z/Z.csproj": "<Project />",
        "package.json": "{}",
        "a/A.csproj": "<Project />",
        "requirements.txt": "a",
        "pyproject.toml": "",
    }

    body = _analyzed_overview(client, _make_repo(roots, files))

    assert [item["path"] for item in body["project_info"]] == [
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "a/A.csproj",
        "z/Z.csproj",
    ]


def test_csproj_with_multiple_references_version_element_and_missing_version(
    client: TestClient, roots: Path
) -> None:
    text = (
        "<Project><PropertyGroup>"
        "<TargetFrameworks>net8.0;net48</TargetFrameworks></PropertyGroup><ItemGroup>"
        '<PackageReference Include="A" Version="1.0" />'
        '<PackageReference Include="B"><Version>2.0</Version></PackageReference>'
        '<PackageReference Include="C" />'
        "</ItemGroup></Project>"
    )

    body = _analyzed_overview(client, _make_repo(roots, {"src/My.Lib/My.Lib.csproj": text}))

    item = _project_info(body)["src/My.Lib/My.Lib.csproj"]
    assert item["name"] == "My.Lib"
    assert item["dependencies"] == ["A@1.0", "B@2.0", "C"]
    assert item["target_frameworks"] == ["net8.0", "net48"]


def test_csproj_with_malicious_doctype_and_entities_is_not_expanded_and_does_not_hurt(
    client: TestClient, roots: Path
) -> None:
    text = (
        '<?xml version="1.0"?>\n<!DOCTYPE lolz [\n'
        '  <!ENTITY lol "lol">\n'
        '  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        '  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
        '  <!ENTITY xxe SYSTEM "file:///etc/passwd">\n]>\n'
        "<Project><ItemGroup>"
        '<PackageReference Include="Evil&xxe;" Version="&lol3;" />'
        "</ItemGroup></Project>\n"
    )

    response = _overview(
        client, _register_and_import(client, _make_repo(roots, {"E.csproj": text}))
    )
    body = _ok(response)

    item = _project_info(body)["E.csproj"]
    assert item["dependencies"] == ["Evil&xxe;@&lol3;"]  # 原樣文字，沒有展開
    assert item["parse_error"] is None
    assert "root:" not in response.text  # 沒有載入外部實體（/etc/passwd 的典型內容）


def _register_and_import(client: TestClient, repo: Path) -> int:
    repository_id = _register(client, repo)
    _import(client, repository_id)
    return repository_id


def test_csproj_larger_than_256_kib_is_skipped_and_its_content_is_never_read(
    app: FastAPI, client: TestClient, roots: Path
) -> None:
    padding = "<!-- " + "x" * (257 * 1024) + " -->"
    big = f'<Project>{padding}<PackageReference Include="Big" Version="1" /></Project>'
    files: dict[str, str | bytes] = {"Big/Big.csproj": big, "Small/Small.csproj": "<Project />"}
    repository_id = _register_and_import(client, _make_repo(roots, files))

    with _Statements(app) as statements:
        body = _ok(_overview(client, repository_id))

    items = _project_info(body)
    assert items["Big/Big.csproj"]["skipped_reason"] == "too_large"
    assert items["Big/Big.csproj"]["dependencies"] == []
    assert items["Small/Small.csproj"]["skipped_reason"] is None
    # 讀取內容的查詢只帶有可讀檔案的 path，過大的檔案沒有被要求
    flattened = repr(statements.parameters)
    assert "Small/Small.csproj" in flattened
    assert "Big/Big.csproj" not in flattened


def test_exactly_256_kib_is_still_parsed(client: TestClient, roots: Path) -> None:
    head = '<Project><PackageReference Include="Edge" Version="1" />'
    tail = "</Project>"
    padding = "x" * (256 * 1024 - len(head) - len(tail))
    body = _analyzed_overview(client, _make_repo(roots, {"E.csproj": head + padding + tail}))

    item = _project_info(body)["E.csproj"]
    assert (item["skipped_reason"], item["dependencies"]) == (None, ["Edge@1"])


def test_only_the_first_20_csproj_files_by_path_are_read(client: TestClient, roots: Path) -> None:
    files: dict[str, str | bytes] = {f"p{n:02d}/P{n:02d}.csproj": "<Project />" for n in range(25)}

    body = _analyzed_overview(client, _make_repo(roots, files))

    assert [item["path"] for item in body["project_info"]] == [
        f"p{n:02d}/P{n:02d}.csproj" for n in range(20)
    ]
    assert len(body["project_info"]) <= 23  # 3 個根目錄檔案＋20 個 csproj 的結構上限


def test_dependencies_over_50_are_truncated(client: TestClient, roots: Path) -> None:
    text = "\n".join(f"pkg{n}==1.0" for n in range(60))

    body = _analyzed_overview(client, _make_repo(roots, {"requirements.txt": text}))

    item = _project_info(body)["requirements.txt"]
    assert (len(item["dependencies"]), item["truncated"]) == (50, True)


def test_repository_without_any_manifest_has_empty_project_info(
    client: TestClient, roots: Path
) -> None:
    assert (
        _analyzed_overview(client, _make_repo(roots, {"app.py": "x = 1\n"}))["project_info"] == []
    )


# --- 語言（整合：mixed 的上限在單元測試完整涵蓋，這裡確認資料流）---


def test_languages_fold_beyond_ten_into_other(client: TestClient, roots: Path) -> None:
    extensions = [".py", ".cs", ".ts", ".js", ".md", ".json", ".yml", ".sql", ".html", ".css"]
    extensions += [".sh", ".go"]
    files: dict[str, str | bytes] = {f"f{i}{ext}": "x\n" for i, ext in enumerate(extensions)}

    body = _analyzed_overview(client, _make_repo(roots, files))

    languages = body["languages"]
    assert len(languages) == 10
    assert languages[-1]["language"] == "其他"
    assert sum(entry["files"] for entry in languages) == body["stats"]["imported_files"]


# --- 效能與查詢次數 ---


def _insert_large_snapshot(
    db_engine: Engine, repository_id: int, commit_sha: str, settings: Settings, files: int
) -> None:
    """直接寫入 completed 快照與大量檔案列（路徑即可，內容極小），不經 Git 匯入。"""
    extensions = [".py", ".cs", ".ts", ".md", ".json", ".csproj", ".weird", ""]
    with Session(db_engine) as session:
        snapshot = Snapshot(
            repository_id=repository_id,
            commit_sha=commit_sha,
            import_profile_hash=ImportProfile.from_settings(settings).hash(),
            status="completed",
            file_count=files,
        )
        session.add(snapshot)
        session.flush()
        rows = [
            {
                "snapshot_id": snapshot.id,
                "path": f"src/pkg{n // 200:03d}/mod{n}{extensions[n % len(extensions)]}",
                "byte_size": 2,
                "content": "x\n",
                "status": "imported",
            }
            for n in range(files - 2)
        ]
        rows.append(
            {
                "snapshot_id": snapshot.id,
                "path": "README.md",
                "byte_size": 2,
                "content": "x\n",
                "status": "imported",
            }
        )
        rows.append(
            {
                "snapshot_id": snapshot.id,
                "path": "package.json",
                "byte_size": 2,
                "content": '{"name": "big"}',
                "status": "imported",
            }
        )
        for start in range(0, len(rows), 2000):
            session.execute(insert(SnapshotFile), rows[start : start + 2000])
        session.commit()


def test_overview_of_a_20000_file_snapshot_uses_a_constant_number_of_queries(
    app: FastAPI,
    client: TestClient,
    roots: Path,
    db_engine: Engine,
    settings: Settings,
    record_property: Any,
) -> None:
    repo = _make_repo(roots, {"app.py": "x = 1\n"})
    repository_id = _register(client, repo)
    # 基準：未分析時只有 Git 與 repo 查詢（沒有快照內容），用來估計檔案相關的成本
    baseline_started = time.perf_counter()
    assert _ok(_overview(client, repository_id))["analyzed"] is False
    record_property(
        "overview_unanalyzed_ms", round((time.perf_counter() - baseline_started) * 1000)
    )
    _insert_large_snapshot(
        db_engine, repository_id, git(repo, "rev-parse", "HEAD"), settings, files=20_000
    )

    with _Statements(app) as statements:
        started = time.perf_counter()
        body = _ok(_overview(client, repository_id))
        elapsed_ms = (time.perf_counter() - started) * 1000
    record_property("overview_20000_files_ms", round(elapsed_ms))

    assert body["analyzed"] is True
    assert body["stats"]["imported_files"] == 20_000
    # 取得 repo、找快照、檔案清單、README、manifest 內容：5 次，與檔案數無關
    assert len(statements.sql) == 5
    assert statements.count("FROM snapshot_files") == 3
    assert statements.writes() == []
    # 只讀 path／byte_size／status 的那次查詢不含 content 欄位
    listing = next(s for s in statements.sql if "snapshot_files.byte_size" in s)
    assert "snapshot_files.content" not in listing
    assert {item["path"] for item in body["project_info"]} >= {"package.json"}
    # 寬鬆的防呆（不是效能目標）：病態的退化（例如逐檔查詢）會遠超過此值
    assert elapsed_ms < 10_000


def test_query_count_does_not_depend_on_the_number_of_files(
    app: FastAPI, client: TestClient, roots: Path
) -> None:
    small = _make_repo(roots, {"a.py": "x\n", "README.md": "hi\n", "package.json": "{}"}, "small")
    files: dict[str, str | bytes] = {f"d{n}/f{n}.py": "x\n" for n in range(300)}
    files |= {"README.md": "hi\n", "package.json": "{}"}
    many = _make_repo(roots, files, "many")
    counts = []
    for repo in (small, many):
        repository_id = _register_and_import(client, repo)
        with _Statements(app) as statements:
            _ok(_overview(client, repository_id))
        counts.append(len(statements.sql))

    assert counts[0] == counts[1] == 5


def test_unanalyzed_overview_runs_only_two_queries(
    app: FastAPI, client: TestClient, roots: Path
) -> None:
    repository_id = _register(client, _make_repo(roots, {"a.py": "x\n"}))

    with _Statements(app) as statements:
        _ok(_overview(client, repository_id))

    assert len(statements.sql) == 2  # 取得 repo＋找快照
    assert statements.count("snapshot_files") == 0


# --- 唯讀 ---


def test_overview_never_imports_or_writes_anything(
    app: FastAPI,
    client: TestClient,
    roots: Path,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(roots, {"app.py": "x = 1\n", "README.md": "hi\n"})
    repository_id = _register(client, repo)
    before = _table_counts(db_engine)
    calls = _record_git_calls(monkeypatch)

    with _Statements(app) as statements:
        for _ in range(3):
            assert _ok(_overview(client, repository_id))["analyzed"] is False

    assert _table_counts(db_engine) == before
    assert before["snapshots"] == 0
    assert statements.writes() == []
    assert _subcommands(calls) <= {"rev-parse", "for-each-ref", "log"}


def test_overview_of_an_analyzed_repository_is_also_read_only(
    app: FastAPI, client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repository_id = _register_and_import(client, _make_repo(roots, CSHARP_FILES))
    before = _table_counts(db_engine)

    with _Statements(app) as statements:
        first = _ok(_overview(client, repository_id))
        second = _ok(_overview(client, repository_id))

    assert first == second  # 重複呼叫結果相同
    assert _table_counts(db_engine) == before
    assert statements.writes() == []


# --- 錯誤處理與路徑重驗 ---


def test_unknown_repository_returns_404(client: TestClient) -> None:
    response = _overview(client, 999)

    assert (response.status_code, _error_code(response)) == (404, "repository_not_found")


@pytest.mark.parametrize("bad_id", ["0", "-1", "2147483648", "99999999999999999999", "abc"])
def test_out_of_range_or_non_integer_id_returns_422(client: TestClient, bad_id: str) -> None:
    response = client.get(f"{URL}/{bad_id}/overview")

    assert response.status_code == 422
    assert _error_code(response) == "validation_error"


def test_removed_repository_returns_404(client: TestClient, roots: Path) -> None:
    repo = _make_repo(roots, {"a.py": "x\n"})
    repository_id = _register_and_import(client, repo)
    assert client.delete(f"{URL}/{repository_id}").status_code == 200

    response = _overview(client, repository_id)

    assert (response.status_code, _error_code(response)) == (404, "repository_not_found")


def test_tampered_local_path_outside_roots_returns_422(
    client: TestClient, roots: Path, tmp_path: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, _make_repo(roots, {"a.py": "x\n"}))
    _update_row(db_engine, repository_id, canonical_local_path=str(init_repo(tmp_path / "evil")))

    response = _overview(client, repository_id)

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


def test_missing_local_directory_returns_422(
    client: TestClient, roots: Path, db_engine: Engine
) -> None:
    repository_id = _register(client, _make_repo(roots, {"a.py": "x\n"}))
    _update_row(db_engine, repository_id, canonical_local_path=str(roots / "gone"))

    response = _overview(client, repository_id)

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


def test_github_tampered_path_returns_422(
    client: TestClient, remote: RemoteRepo, tmp_path: Path, db_engine: Engine
) -> None:
    repository_id = _register_github(client)
    _update_row(db_engine, repository_id, canonical_local_path=str(init_repo(tmp_path / "evil")))

    response = _overview(client, repository_id)

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


def test_git_timeout_returns_504(
    client: TestClient, roots: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id = _register(client, _make_repo(roots, {"a.py": "x\n"}))

    def fake_run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("repopilot.git.runner.subprocess.run", fake_run)

    response = _overview(client, repository_id)

    assert (response.status_code, _error_code(response)) == (504, "git_timeout")
