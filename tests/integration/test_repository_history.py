"""切片 5e-1：`GET /repositories/{id}/refs` 與 `GET /repositories/{id}/commits`（D34）。

PostgreSQL ＋ 真實本機 Git。GitHub 來源以本機 bare repo 模擬遠端（file://），GitHub API 以
`httpx.MockTransport` 模擬，不連真實 GitHub。重點不變量：唯讀（絕不寫入 repo）、惡意 `ref` 一律
422 且 `git log` 根本不會被呼叫、輸出解析不被特殊字元破壞。
"""

import hashlib
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

from repopilot.api.routes.github import get_github_client
from repopilot.config import Settings
from repopilot.db.models import Snapshot
from repopilot.git.runner import GitResult, run_git
from repopilot.main import create_app
from repopilot.services.import_profile import ImportProfile
from tests.git_helpers import RemoteRepo, git, init_repo
from tests.github_helpers import FakeGitHub

URL = "/api/v1/repositories"
GITHUB_URL = f"{URL}/github"
REPO_FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n"}


# --- fixtures ---


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
def local_repo(roots: Path) -> Path:
    return init_repo(roots / "demo", files=dict(REPO_FILES))


@pytest.fixture
def remote(remotes: Path, fake_github: FakeGitHub) -> RemoteRepo:
    fake_github.add_repo("octocat/Hello-World")
    return RemoteRepo(remotes, "octocat", "Hello-World", files=dict(REPO_FILES))


# --- helpers ---


def _register(client: TestClient, path: Path) -> int:
    response = client.post(URL, json={"path": str(path)})
    assert response.status_code in (200, 201), response.text
    return int(response.json()["id"])


def _register_github(client: TestClient) -> int:
    response = client.post(GITHUB_URL, json={"repo": "octocat/Hello-World"})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _refs(client: TestClient, repository_id: int) -> Any:
    return client.get(f"{URL}/{repository_id}/refs")


def _commits(client: TestClient, repository_id: int, **params: Any) -> Any:
    return client.get(f"{URL}/{repository_id}/commits", params=params)


def _ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def _error_code(response: Any) -> str:
    code = response.json()["error"]["code"]
    assert isinstance(code, str)
    return code


def _empty_commit(
    repo: Path, message: str = "c", *, author: str | None = None, date: str | None = None
) -> str:
    args = ["-c", "commit.gpgsign=false"]
    if author is not None:
        args = ["-c", f"user.name={author}", *args]
    args += ["commit", "--quiet", "--allow-empty", "--allow-empty-message", "-m", message]
    if date is not None:
        args.append(f"--date={date}")
    git(repo, *args)
    return git(repo, "rev-parse", "HEAD")


def _merge(repo: Path, message: str, *branches: str) -> str:
    git(repo, "-c", "commit.gpgsign=false", "merge", "--quiet", "--no-ff", "-m", message, *branches)
    return git(repo, "rev-parse", "HEAD")


def _bulk_refs(repo: Path, prefix: str, count: int, sha: str) -> None:
    commands = "".join(f"create {prefix}b{n:04d} {sha}\n" for n in range(count))
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "update-ref", "--stdin"],  # noqa: S607
        input=commands.encode(),
        capture_output=True,
        check=True,
        timeout=60,
    )


def _fingerprint(root: Path) -> dict[str, str]:
    """目錄樹（含 `.git`）的內容指紋：路徑 → 檔案 SHA-256 或 `dir`。"""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _update_row(db_engine: Engine, repository_id: int, **values: str) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    with db_engine.begin() as connection:
        connection.execute(
            text(f"UPDATE repositories SET {assignments} WHERE id = :id"),  # noqa: S608
            {**values, "id": repository_id},
        )


def _record_git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """記錄應用程式執行的所有 Git 指令（`-C <路徑>` 之後的參數）；行為不變。

    在最底層（`subprocess.run`）攔截，所以 `rev-parse`、`for-each-ref`、`log` 與路徑驗證的
    指令都會被看到；只在測試把它掛上之後的請求期間有意義。
    """
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


def _inject_timeout(monkeypatch: pytest.MonkeyPatch, *, only: str | None) -> None:
    """讓 Git 逾時（`only` 指定時，只有該子指令逾時）。"""
    real_run = subprocess.run

    def fake_run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if only is None or only in command:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("repopilot.git.runner.subprocess.run", fake_run)


# --- refs：本機來源 ---


def test_refs_lists_branches_and_tags_with_documented_shape(
    client: TestClient, local_repo: Path
) -> None:
    sha = git(local_repo, "rev-parse", "HEAD")
    git(local_repo, "branch", "feature/login")
    git(local_repo, "tag", "v1")
    git(local_repo, "tag", "-a", "v2", "-m", "release")
    git(local_repo, "tag", "tree-only", git(local_repo, "rev-parse", "HEAD^{tree}"))
    repository_id = _register(client, local_repo)

    body = _ok(_refs(client, repository_id))

    assert body == {
        "default_ref": "refs/heads/main",
        "head_sha": sha,
        "branches": [
            {
                "name": "feature/login",
                "ref": "refs/heads/feature/login",
                "sha": sha,
                "is_default": False,
            },
            {"name": "main", "ref": "refs/heads/main", "sha": sha, "is_default": True},
        ],
        "tags": [
            {"name": "v1", "ref": "refs/tags/v1", "sha": sha},
            {"name": "v2", "ref": "refs/tags/v2", "sha": sha},  # 附註標籤已還原為 commit
        ],
        "truncated": False,
    }


def test_refs_head_follows_the_checked_out_branch(client: TestClient, local_repo: Path) -> None:
    git(local_repo, "checkout", "--quiet", "-b", "dev")
    dev_sha = _empty_commit(local_repo, "on dev")
    repository_id = _register(client, local_repo)

    body = _ok(_refs(client, repository_id))

    assert body["default_ref"] == "refs/heads/dev"
    assert body["head_sha"] == dev_sha
    assert [b["name"] for b in body["branches"] if b["is_default"]] == ["dev"]


def test_refs_detached_head_has_no_default_ref_but_keeps_head_sha(
    client: TestClient, local_repo: Path
) -> None:
    git(local_repo, "checkout", "--quiet", "--detach")
    detached = _empty_commit(local_repo, "detached work")
    repository_id = _register(client, local_repo)

    body = _ok(_refs(client, repository_id))

    assert body["default_ref"] is None
    assert body["head_sha"] == detached
    assert [b["name"] for b in body["branches"]] == ["main"]
    assert not any(b["is_default"] for b in body["branches"])


def test_refs_in_an_empty_repository_is_empty_not_500(client: TestClient, roots: Path) -> None:
    empty = roots / "empty"
    empty.mkdir()
    git(empty, "init", "--quiet", "--initial-branch=main")
    repository_id = _register(client, empty)

    body = _ok(_refs(client, repository_id))

    assert body == {
        "default_ref": None,
        "head_sha": None,
        "branches": [],
        "tags": [],
        "truncated": False,
    }


def test_refs_ignores_remote_tracking_stash_and_symbolic_refs(
    client: TestClient, local_repo: Path
) -> None:
    sha = git(local_repo, "rev-parse", "HEAD")
    git(local_repo, "update-ref", "refs/remotes/origin/main", sha)
    git(local_repo, "update-ref", "refs/stash", sha)
    git(local_repo, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    repository_id = _register(client, local_repo)

    body = _ok(_refs(client, repository_id))

    assert [b["ref"] for b in body["branches"]] == ["refs/heads/main"]


@pytest.mark.parametrize(
    "name",
    [
        "feature/功能-中文",
        "release/v1.0",
        "a.b.c",
        "with'quote",
        "semi;colon",
        "dollar$x",
        "percent%x",
        "amp&x",
        "back`tick",
        "plus+x",
        "hash#1",
        "at@x",
        "emoji-😀",
    ],
)
def test_special_character_branch_names_work_in_refs_and_commits(
    client: TestClient, local_repo: Path, name: str
) -> None:
    git(local_repo, "checkout", "--quiet", "-b", name)
    tip = _empty_commit(local_repo, f"work on {name}")
    git(local_repo, "checkout", "--quiet", "main")
    repository_id = _register(client, local_repo)

    refs = _ok(_refs(client, repository_id))
    commits = _ok(_commits(client, repository_id, ref=f"refs/heads/{name}"))

    assert {"name": name, "ref": f"refs/heads/{name}", "sha": tip, "is_default": False} in refs[
        "branches"
    ]
    assert commits["items"][0]["sha"] == tip
    assert {"name": name, "kind": "branch"} in commits["items"][0]["refs"]


def test_refs_caps_branches_and_tags_at_500_and_reports_truncated(
    client: TestClient, local_repo: Path
) -> None:
    sha = git(local_repo, "rev-parse", "HEAD")
    _bulk_refs(local_repo, "refs/heads/", 520, sha)
    repository_id = _register(client, local_repo)

    body = _ok(_refs(client, repository_id))

    assert len(body["branches"]) == 500
    assert body["truncated"] is True


def test_refs_truncated_when_only_tags_exceed_the_cap(client: TestClient, local_repo: Path) -> None:
    sha = git(local_repo, "rev-parse", "HEAD")
    _bulk_refs(local_repo, "refs/tags/", 501, sha)
    repository_id = _register(client, local_repo)

    body = _ok(_refs(client, repository_id))

    assert len(body["tags"]) == 500
    assert len(body["branches"]) == 1
    assert body["truncated"] is True


# --- commits：本機來源 ---


def test_commits_linear_history_shape_newest_first_without_email(
    client: TestClient, local_repo: Path
) -> None:
    root = git(local_repo, "rev-parse", "HEAD")
    second = _empty_commit(local_repo, "second", author="Alice", date="2021-03-04T05:06:07+02:00")
    repository_id = _register(client, local_repo)

    response = _commits(client, repository_id)

    body = _ok(response)
    assert set(body) == {"items", "total_returned", "has_more"}
    assert body["total_returned"] == 2
    assert body["has_more"] is False
    newest, oldest = body["items"]
    assert newest == {
        "sha": second,
        "parents": [root],
        "subject": "second",
        "author_name": "Alice",
        "authored_at": "2021-03-04T03:06:07Z",
        "refs": [{"name": "main", "kind": "branch"}],
        "imported": False,
    }
    assert oldest["sha"] == root and oldest["parents"] == []
    # 不暴露 email（測試 repo 的作者 email 是 test@example.invalid）
    assert "example.invalid" not in response.text and "email" not in response.text
    datetime.fromisoformat(newest["authored_at"].replace("Z", "+00:00"))  # 合法的 ISO 8601


def test_commits_merge_octopus_and_multiple_roots_report_all_parents(
    client: TestClient, local_repo: Path
) -> None:
    root_a = git(local_repo, "rev-parse", "HEAD")
    git(local_repo, "checkout", "--quiet", "-b", "topic")
    topic = _empty_commit(local_repo, "topic")
    git(local_repo, "checkout", "--quiet", "main")
    main = _empty_commit(local_repo, "main work")
    merge = _merge(local_repo, "merge topic", "topic")
    for name in ("o1", "o2"):
        git(local_repo, "checkout", "--quiet", "-b", name, "main")
        _empty_commit(local_repo, name)
    git(local_repo, "checkout", "--quiet", "--orphan", "other-root")
    root_b = _empty_commit(local_repo, "second root")
    git(local_repo, "checkout", "--quiet", "main")
    _empty_commit(local_repo, "main moves on")
    octopus = _merge(local_repo, "octopus", "o1", "o2")
    repository_id = _register(client, local_repo)

    items = {c["sha"]: c for c in _ok(_commits(client, repository_id))["items"]}

    assert set(items[merge]["parents"]) == {main, topic}
    assert len(items[octopus]["parents"]) == 3
    assert items[root_a]["parents"] == [] and items[root_b]["parents"] == []
    # topo-order：每個 commit 都在它的所有親代之前
    order = [c["sha"] for c in _ok(_commits(client, repository_id))["items"]]
    for sha, item in items.items():
        for parent in item["parents"]:
            assert order.index(sha) < order.index(parent)


def test_commits_ref_filter_versus_all_branches(client: TestClient, local_repo: Path) -> None:
    base = git(local_repo, "rev-parse", "HEAD")
    git(local_repo, "checkout", "--quiet", "-b", "topic")
    topic = _empty_commit(local_repo, "topic only")
    git(local_repo, "checkout", "--quiet", "main")
    main = _empty_commit(local_repo, "main only")
    repository_id = _register(client, local_repo)

    everything = {c["sha"] for c in _ok(_commits(client, repository_id))["items"]}
    only_topic = [
        c["sha"] for c in _ok(_commits(client, repository_id, ref="refs/heads/topic"))["items"]
    ]
    only_main = [
        c["sha"] for c in _ok(_commits(client, repository_id, ref="refs/heads/main"))["items"]
    ]

    assert everything == {base, topic, main}
    assert only_topic == [topic, base]
    assert only_main == [main, base]


def test_commits_accepts_annotated_and_lightweight_tags_as_ref(
    client: TestClient, local_repo: Path
) -> None:
    old = git(local_repo, "rev-parse", "HEAD")
    git(local_repo, "tag", "-a", "v1", "-m", "release")
    git(local_repo, "tag", "light")
    _empty_commit(local_repo, "after the tag")
    repository_id = _register(client, local_repo)

    annotated = _ok(_commits(client, repository_id, ref="refs/tags/v1"))
    lightweight = _ok(_commits(client, repository_id, ref="refs/tags/light"))

    assert [c["sha"] for c in annotated["items"]] == [old]
    assert [c["sha"] for c in lightweight["items"]] == [old]
    assert {"name": "v1", "kind": "tag"} in annotated["items"][0]["refs"]


def test_commits_all_branches_includes_tagged_history_but_not_stash(
    client: TestClient, local_repo: Path
) -> None:
    git(local_repo, "checkout", "--quiet", "--detach")
    tagged = _empty_commit(local_repo, "only a tag reaches me")
    git(local_repo, "tag", "release", tagged)
    stashed = _empty_commit(local_repo, "only the stash reaches me")
    git(local_repo, "update-ref", "refs/stash", stashed)
    git(local_repo, "checkout", "--quiet", "main")
    repository_id = _register(client, local_repo)

    shas = {c["sha"] for c in _ok(_commits(client, repository_id))["items"]}

    assert tagged in shas
    assert stashed not in shas


def test_commits_pagination(client: TestClient, local_repo: Path) -> None:
    for n in range(24):
        _empty_commit(local_repo, f"c{n}")  # 加上初始 commit 共 25 個
    repository_id = _register(client, local_repo)

    first = _ok(_commits(client, repository_id, limit=10))
    second = _ok(_commits(client, repository_id, limit=10, offset=10))
    third = _ok(_commits(client, repository_id, limit=10, offset=20))
    beyond = _ok(_commits(client, repository_id, limit=10, offset=25))
    exact = _ok(_commits(client, repository_id, limit=25))
    default = _ok(_commits(client, repository_id))

    assert (first["total_returned"], first["has_more"]) == (10, True)
    assert (second["total_returned"], second["has_more"]) == (10, True)
    assert (third["total_returned"], third["has_more"]) == (5, False)
    assert (beyond["items"], beyond["total_returned"], beyond["has_more"]) == ([], 0, False)
    assert (exact["total_returned"], exact["has_more"]) == (25, False)
    assert default["total_returned"] == 25  # 預設 limit 100
    pages = first["items"] + second["items"] + third["items"]
    assert [c["sha"] for c in pages] == [c["sha"] for c in default["items"]]


def test_commits_limit_500_and_offset_100000_are_the_inclusive_bounds(
    client: TestClient, local_repo: Path
) -> None:
    repository_id = _register(client, local_repo)

    assert _commits(client, repository_id, limit=500).status_code == 200
    assert _commits(client, repository_id, offset=100_000).status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 501},
        {"limit": "abc"},
        {"limit": ""},
        {"limit": "1.5"},
        {"offset": -1},
        {"offset": 100_001},
        {"offset": 2**63},
        {"offset": "abc"},
    ],
)
def test_commits_invalid_paging_parameters_return_422_validation_error(
    client: TestClient, local_repo: Path, params: dict[str, Any]
) -> None:
    repository_id = _register(client, local_repo)

    response = _commits(client, repository_id, **params)

    assert response.status_code == 422
    assert _error_code(response) == "validation_error"


def test_commits_subject_author_and_message_edge_cases(
    client: TestClient, local_repo: Path
) -> None:
    multi = _empty_commit(
        local_repo,
        'line one\nline two\n\nbody "quoted" with 100% $HOME `x` \\ and 中文',
        author='Ünï "q" 名字; $(whoami) %s',
    )
    long = _empty_commit(local_repo, "長" * 450)
    empty = _empty_commit(local_repo, "")  # git 允許空訊息（--allow-empty-message 由 -m "" 達成）
    repository_id = _register(client, local_repo)

    items = {c["sha"]: c for c in _ok(_commits(client, repository_id))["items"]}

    assert items[multi]["subject"] == "line one line two"
    assert items[multi]["author_name"] == 'Ünï "q" 名字; $(whoami) %s'
    assert items[long]["subject"] == "長" * 200
    assert items[empty]["subject"] == ""


def test_commits_decorations_name_branches_and_tags_precisely(
    client: TestClient, local_repo: Path
) -> None:
    git(local_repo, "branch", "zeta")
    git(local_repo, "branch", "alpha/one")
    git(local_repo, "tag", "v1")
    git(local_repo, "tag", "-a", "v2", "-m", "annotated")
    # 看起來像 remote-tracking 的「本機」分支名稱：不會與遠端分支混淆
    git(local_repo, "branch", "origin/main")
    tip = _empty_commit(local_repo, "tip")
    repository_id = _register(client, local_repo)

    items = _ok(_commits(client, repository_id))["items"]

    assert items[0]["refs"] == [{"name": "main", "kind": "branch"}] and items[0]["sha"] == tip
    assert items[1]["refs"] == [
        {"name": "alpha/one", "kind": "branch"},
        {"name": "origin/main", "kind": "branch"},
        {"name": "zeta", "kind": "branch"},
        {"name": "v1", "kind": "tag"},
        {"name": "v2", "kind": "tag"},
    ]


def test_commits_decorations_exclude_stash_and_remote_tracking_refs(
    client: TestClient, local_repo: Path
) -> None:
    sha = git(local_repo, "rev-parse", "HEAD")
    git(local_repo, "update-ref", "refs/stash", sha)
    git(local_repo, "update-ref", "refs/remotes/origin/main", sha)
    repository_id = _register(client, local_repo)

    item = _ok(_commits(client, repository_id))["items"][0]

    assert item["refs"] == [{"name": "main", "kind": "branch"}]


def test_commits_in_an_empty_repository_is_empty_not_500(client: TestClient, roots: Path) -> None:
    empty = roots / "empty"
    empty.mkdir()
    git(empty, "init", "--quiet", "--initial-branch=main")
    repository_id = _register(client, empty)

    assert _ok(_commits(client, repository_id)) == {
        "items": [],
        "total_returned": 0,
        "has_more": False,
    }
    # 空 repo 沒有任何 ref，指定 ref 就是不存在
    response = _commits(client, repository_id, ref="refs/heads/main")
    assert (response.status_code, _error_code(response)) == (422, "invalid_revision")


def test_commits_tag_pointing_to_a_tree_is_not_selectable_and_does_not_break_the_log(
    client: TestClient, local_repo: Path
) -> None:
    git(local_repo, "tag", "tree-only", git(local_repo, "rev-parse", "HEAD^{tree}"))
    repository_id = _register(client, local_repo)

    assert _ok(_commits(client, repository_id))["total_returned"] == 1
    response = _commits(client, repository_id, ref="refs/tags/tree-only")
    assert (response.status_code, _error_code(response)) == (422, "invalid_revision")


def test_local_source_does_not_accept_remote_tracking_refs(
    client: TestClient, local_repo: Path
) -> None:
    git(local_repo, "update-ref", "refs/remotes/origin/main", git(local_repo, "rev-parse", "HEAD"))
    repository_id = _register(client, local_repo)

    response = _commits(client, repository_id, ref="refs/remotes/origin/main")

    assert (response.status_code, _error_code(response)) == (422, "invalid_revision")


def test_dirty_working_tree_does_not_change_the_results(
    client: TestClient, local_repo: Path
) -> None:
    repository_id = _register(client, local_repo)
    before = (_ok(_refs(client, repository_id)), _ok(_commits(client, repository_id)))

    (local_repo / "README.md").write_text("changed but not committed\n", encoding="utf-8")
    (local_repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    (local_repo / "src").mkdir(exist_ok=True)
    (local_repo / "src" / "staged.py").write_text("y = 2\n", encoding="utf-8")
    git(local_repo, "add", "src/staged.py")

    after = (_ok(_refs(client, repository_id)), _ok(_commits(client, repository_id)))
    assert after == before


# --- 惡意 ref ---

MALICIOUS_REFS = [
    "--output=x",
    "--output=../x",
    "--all",
    "-h",
    "--exec=touch x",
    "HEAD",
    "HEAD~1",
    "HEAD^",
    "main",
    "refs/heads/main..refs/heads/main",
    "refs/heads/main...HEAD",
    "refs/heads/../x",
    "refs/heads/main/../../HEAD",
    "refs/heads/does-not-exist",
    "refs/heads/",
    "refs/heads",
    "refs/heads/*",
    "refs/heads/ma*",
    "refs/heads/main extra",
    " refs/heads/main",
    "refs/heads/main ",
    "refs/heads/main\tx",
    "refs/heads/main\nrefs/heads/main",
    "refs/heads/main\r",
    "refs/heads/main\x00",
    "refs/heads/main\x1b[31m",
    "refs/heads/main^{tree}",
    "refs/heads/main~1",
    "refs/heads/main@{1}",
    "refs/stash",
    "refs/notes/commits",
    "refs/remotes/origin/main",
    "refs/tags/--output=x",
    "",
    "x" * 5000,
    "refs/heads/" + "x" * 5000,
]


@pytest.mark.parametrize("ref", MALICIOUS_REFS)
def test_malicious_or_unknown_refs_are_rejected_with_422_and_write_nothing(
    client: TestClient,
    local_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    ref: str,
) -> None:
    repository_id = _register(client, local_repo)
    before = _fingerprint(local_repo)
    calls = _record_git_calls(monkeypatch)

    response = _commits(client, repository_id, ref=ref)

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_revision"
    assert "log" not in _subcommands(calls)  # 沒有任何 revision 被交給 git log
    assert _fingerprint(local_repo) == before
    assert not (Path.cwd() / "x").exists()
    assert not (local_repo.parent / "x").exists()


def test_other_repository_sha_is_not_a_valid_ref(client: TestClient, roots: Path) -> None:
    mine = init_repo(roots / "mine")
    other = init_repo(roots / "other", files={"other.txt": "other\n"})
    other_sha = git(other, "rev-parse", "HEAD")
    repository_id = _register(client, mine)

    for ref in (other_sha, other_sha[:12], "refs/heads/" + other_sha):
        response = _commits(client, repository_id, ref=ref)
        assert (response.status_code, _error_code(response)) == (422, "invalid_revision")


def test_a_ref_of_another_repository_is_not_visible(client: TestClient, roots: Path) -> None:
    mine = init_repo(roots / "mine")
    other = init_repo(roots / "other", files={"other.txt": "other\n"})
    git(other, "branch", "secret-feature")
    repository_id = _register(client, mine)

    response = _commits(client, repository_id, ref="refs/heads/secret-feature")

    assert (response.status_code, _error_code(response)) == (422, "invalid_revision")


def test_refs_and_commits_only_run_read_only_git_commands(
    client: TestClient, local_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(local_repo, "tag", "v1")
    repository_id = _register(client, local_repo)
    before = _fingerprint(local_repo)
    calls = _record_git_calls(monkeypatch)

    _ok(_refs(client, repository_id))
    _ok(_commits(client, repository_id))
    _ok(_commits(client, repository_id, ref="refs/tags/v1"))

    assert _subcommands(calls) <= {"for-each-ref", "log", "rev-parse"}
    assert _fingerprint(local_repo) == before


def test_commits_uses_a_single_log_process_per_request(
    client: TestClient, local_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(5):
        _empty_commit(local_repo, f"c{n}")
    repository_id = _register(client, local_repo)
    calls = _record_git_calls(monkeypatch)

    _ok(_commits(client, repository_id, limit=500))

    assert [_subcommands([c]) for c in calls].count({"log"}) == 1


# --- imported ---


def _import(client: TestClient, repository_id: int, revision: str) -> Any:
    response = client.post(f"{URL}/{repository_id}/imports", json={"revision": revision})
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_imported_flag_changes_after_import_and_matches_import_reuse(
    client: TestClient, local_repo: Path
) -> None:
    old = git(local_repo, "rev-parse", "HEAD")
    new = _empty_commit(local_repo, "new")
    repository_id = _register(client, local_repo)
    assert [c["imported"] for c in _ok(_commits(client, repository_id))["items"]] == [False, False]

    created = client.post(f"{URL}/{repository_id}/imports", json={"revision": old})
    assert created.status_code == 201

    imported = {c["sha"]: c["imported"] for c in _ok(_commits(client, repository_id))["items"]}
    assert imported == {old: True, new: False}
    # `imported=true` 恰好表示「再匯入一次會回 200」；false 表示會新建（201）
    assert client.post(f"{URL}/{repository_id}/imports", json={"revision": old}).status_code == 200
    assert client.post(f"{URL}/{repository_id}/imports", json={"revision": new}).status_code == 201


def test_imported_is_false_when_the_import_profile_changed(
    client: TestClient, local_repo: Path, settings: Settings, clean_repositories: None
) -> None:
    repository_id = _register(client, local_repo)
    _import(client, repository_id, "HEAD")
    assert _ok(_commits(client, repository_id))["items"][0]["imported"] is True

    changed = settings.model_copy(update={"import_max_files": settings.import_max_files + 1})
    assert (
        ImportProfile.from_settings(changed).hash() != ImportProfile.from_settings(settings).hash()
    )
    with TestClient(create_app(changed)) as other_client:
        assert _ok(_commits(other_client, repository_id))["items"][0]["imported"] is False
        # 與匯入的語意一致：換了設定，再匯入會建立新快照（201）
        reimport = other_client.post(f"{URL}/{repository_id}/imports", json={"revision": "HEAD"})
        assert reimport.status_code == 201


def test_imported_ignores_failed_importing_and_other_repository_snapshots(
    client: TestClient, roots: Path, db_engine: Engine, settings: Settings
) -> None:
    first = init_repo(roots / "first")
    second = init_repo(roots / "second")
    first_id, second_id = _register(client, first), _register(client, second)
    sha = git(first, "rev-parse", "HEAD")
    profile_hash = ImportProfile.from_settings(settings).hash()

    def add(repository_id: int, status: str) -> None:
        with Session(db_engine) as session:
            session.add(
                Snapshot(
                    repository_id=repository_id,
                    commit_sha=sha,
                    import_profile_hash=profile_hash,
                    status=status,
                )
            )
            session.commit()

    add(second_id, "completed")  # 別的 repo 有同一個 SHA 的 completed 快照
    add(first_id, "failed")
    assert _ok(_commits(client, first_id))["items"][0]["imported"] is False

    with db_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM snapshots WHERE repository_id = :id"), {"id": first_id}
        )
    add(first_id, "importing")
    assert _ok(_commits(client, first_id))["items"][0]["imported"] is False


def test_imported_is_decided_with_one_snapshot_query_for_all_commits(
    app: FastAPI, client: TestClient, local_repo: Path
) -> None:
    for n in range(40):
        _empty_commit(local_repo, f"c{n}")
    repository_id = _register(client, local_repo)
    _import(client, repository_id, "HEAD")
    statements: list[str] = []
    engine: Engine = app.state.session_factory.kw["bind"]

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        body = _ok(_commits(client, repository_id, limit=100))
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert body["total_returned"] == 41
    assert sum(1 for s in statements if "FROM snapshots" in s) == 1
    assert len(statements) <= 3  # 取得 repo＋一次快照查詢（與 commit 數量無關）
    assert sum(1 for c in body["items"] if c["imported"]) == 1


# --- 錯誤處理與路徑重驗 ---


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_unknown_repository_returns_404(client: TestClient, endpoint: str) -> None:
    response = client.get(f"{URL}/999/{endpoint}")

    assert (response.status_code, _error_code(response)) == (404, "repository_not_found")


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
@pytest.mark.parametrize("bad_id", ["0", "-1", "2147483648", "99999999999999999999", "abc"])
def test_out_of_range_or_non_integer_id_returns_422(
    client: TestClient, endpoint: str, bad_id: str
) -> None:
    response = client.get(f"{URL}/{bad_id}/{endpoint}")

    assert response.status_code == 422
    assert _error_code(response) == "validation_error"


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_removed_repository_returns_404(
    client: TestClient, local_repo: Path, endpoint: str
) -> None:
    repository_id = _register(client, local_repo)
    assert client.delete(f"{URL}/{repository_id}").status_code == 200

    response = client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (404, "repository_not_found")
    assert local_repo.exists()  # 移除 local 來源不碰磁碟


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_tampered_local_path_outside_roots_returns_422(
    client: TestClient, local_repo: Path, tmp_path: Path, db_engine: Engine, endpoint: str
) -> None:
    repository_id = _register(client, local_repo)
    outside = init_repo(tmp_path / "outside-roots")
    _update_row(db_engine, repository_id, canonical_local_path=str(outside))

    response = client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_missing_local_directory_returns_422(
    client: TestClient, local_repo: Path, db_engine: Engine, endpoint: str
) -> None:
    repository_id = _register(client, local_repo)
    _update_row(db_engine, repository_id, canonical_local_path=str(local_repo.parent / "gone"))

    response = client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_path_that_is_a_subdirectory_of_a_repo_returns_422(
    client: TestClient, local_repo: Path, db_engine: Engine, endpoint: str
) -> None:
    repository_id = _register(client, local_repo)
    _update_row(db_engine, repository_id, canonical_local_path=str(local_repo / "src"))

    response = client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_git_timeout_returns_504(
    client: TestClient, local_repo: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    repository_id = _register(client, local_repo)
    _inject_timeout(monkeypatch, only=None)

    response = client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (504, "git_timeout")


def test_timeout_in_git_log_itself_returns_504(
    client: TestClient, local_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id = _register(client, local_repo)
    _inject_timeout(monkeypatch, only="log")

    response = _commits(client, repository_id)

    assert (response.status_code, _error_code(response)) == (504, "git_timeout")


def test_timeout_in_for_each_ref_returns_504(
    client: TestClient, local_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id = _register(client, local_repo)
    _inject_timeout(monkeypatch, only="for-each-ref")

    response = _refs(client, repository_id)

    assert (response.status_code, _error_code(response)) == (504, "git_timeout")


def test_git_failure_returns_500_with_stable_code(
    client: TestClient, local_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id = _register(client, local_repo)
    real = run_git

    def broken(repo: Path, args: list[str], **kwargs: Any) -> Any:
        if "log" in args:
            return GitResult(128, b"", "fatal: boom")
        return real(repo, args, **kwargs)

    monkeypatch.setattr("repopilot.git.history.run_git", broken)

    response = _commits(client, repository_id)

    assert (response.status_code, _error_code(response)) == (500, "git_command_failed")


# --- GitHub 來源 ---


def _push_branch(remote: RemoteRepo, name: str, message: str) -> str:
    git(remote.work, "checkout", "--quiet", "-b", name)
    sha = _empty_commit(remote.work, message)
    remote.push(f"HEAD:refs/heads/{name}")
    git(remote.work, "checkout", "--quiet", "main")
    return sha


def test_github_refs_use_origin_branches_without_prefix_and_exclude_origin_head(
    client: TestClient, remote: RemoteRepo, managed_dir: Path
) -> None:
    main_sha = remote.head()
    dev_sha = _push_branch(remote, "dev", "dev work")
    release_sha = _push_branch(remote, "release/1.0", "release work")
    git(remote.work, "tag", "v1")
    git(remote.work, "tag", "-a", "v2", "-m", "annotated", "dev")
    remote.push("refs/tags/v1", "refs/tags/v2")
    repository_id = _register_github(client)
    clone = managed_dir / "github" / "octocat" / "Hello-World"
    git(clone, "branch", "local-only")  # 本機 clone 的分支不屬於 GitHub 的分支
    before = _fingerprint(clone)

    body = _ok(_refs(client, repository_id))

    assert body["default_ref"] == "refs/remotes/origin/main"
    assert body["head_sha"] == main_sha
    assert body["branches"] == [
        {"name": "dev", "ref": "refs/remotes/origin/dev", "sha": dev_sha, "is_default": False},
        {"name": "main", "ref": "refs/remotes/origin/main", "sha": main_sha, "is_default": True},
        {
            "name": "release/1.0",
            "ref": "refs/remotes/origin/release/1.0",
            "sha": release_sha,
            "is_default": False,
        },
    ]
    assert body["tags"] == [
        {"name": "v1", "ref": "refs/tags/v1", "sha": main_sha},
        {"name": "v2", "ref": "refs/tags/v2", "sha": dev_sha},  # 附註標籤還原為 commit
    ]
    assert "HEAD" not in [b["name"] for b in body["branches"]]
    assert _fingerprint(clone) == before


def test_github_default_branch_comes_from_the_registered_value(
    client: TestClient, remote: RemoteRepo, fake_github: FakeGitHub
) -> None:
    dev_sha = _push_branch(remote, "dev", "dev work")
    fake_github.add_repo("octocat/Hello-World", default_branch="dev")
    repository_id = _register_github(client)

    body = _ok(_refs(client, repository_id))

    assert body["default_ref"] == "refs/remotes/origin/dev"
    assert body["head_sha"] == dev_sha
    assert [b["name"] for b in body["branches"] if b["is_default"]] == ["dev"]


def test_github_default_branch_missing_from_the_clone_gives_null_default(
    client: TestClient, remote: RemoteRepo, fake_github: FakeGitHub
) -> None:
    fake_github.add_repo("octocat/Hello-World", default_branch="renamed-away")
    repository_id = _register_github(client)

    body = _ok(_refs(client, repository_id))

    assert (body["default_ref"], body["head_sha"]) == (None, None)
    assert [b["name"] for b in body["branches"]] == ["main"]
    assert not any(b["is_default"] for b in body["branches"])


def test_github_commits_all_branches_and_ref_filter(client: TestClient, remote: RemoteRepo) -> None:
    base = remote.head()
    dev = _push_branch(remote, "dev", "dev only")
    repository_id = _register_github(client)

    everything = _ok(_commits(client, repository_id))
    only_dev = _ok(_commits(client, repository_id, ref="refs/remotes/origin/dev"))
    only_main = _ok(_commits(client, repository_id, ref="refs/remotes/origin/main"))

    assert {c["sha"] for c in everything["items"]} == {base, dev}
    assert [c["sha"] for c in only_dev["items"]] == [dev, base]
    assert [c["sha"] for c in only_main["items"]] == [base]
    # decoration 的名稱已去掉 origin/，且不含 HEAD 與本機 clone 的 main
    labels = [r for c in everything["items"] if c["sha"] == base for r in c["refs"]]
    assert labels == [{"name": "main", "kind": "branch"}]
    assert {"name": "dev", "kind": "branch"} in everything["items"][0]["refs"]


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/main",  # clone 在本機建立的分支：不屬於 GitHub 來源的清單
        "refs/remotes/origin/HEAD",
        "refs/remotes/upstream/main",
        "origin/main",
        "main",
        "refs/remotes/origin/missing",
    ],
)
def test_github_commits_reject_refs_outside_the_origin_namespace(
    client: TestClient, remote: RemoteRepo, ref: str
) -> None:
    repository_id = _register_github(client)

    response = _commits(client, repository_id, ref=ref)

    assert (response.status_code, _error_code(response)) == (422, "invalid_revision")


def test_github_commits_ignore_local_only_commits(
    client: TestClient, remote: RemoteRepo, managed_dir: Path
) -> None:
    repository_id = _register_github(client)
    clone = managed_dir / "github" / "octocat" / "Hello-World"
    # 本機 clone 的 main 多了一個 commit（不在 origin），不應出現在 GitHub 來源的歷史
    local_only = _empty_commit(clone, "local only commit")

    shas = {c["sha"] for c in _ok(_commits(client, repository_id))["items"]}

    assert local_only not in shas


def test_github_refs_and_commits_follow_a_fetch(client: TestClient, remote: RemoteRepo) -> None:
    repository_id = _register_github(client)
    first_tip = _ok(_refs(client, repository_id))["head_sha"]
    new_sha = remote.commit({"new.txt": "new\n"}, message="after clone")

    assert client.post(f"{URL}/{repository_id}/fetch").status_code == 200

    refs = _ok(_refs(client, repository_id))
    commits = _ok(_commits(client, repository_id, ref=refs["default_ref"]))
    assert refs["head_sha"] == new_sha  # 預設分支的現況，不是 clone 時本機 HEAD 的舊值
    assert [c["sha"] for c in commits["items"]][:2] == [new_sha, first_tip]


def test_github_imported_flag_uses_the_same_import_flow(
    client: TestClient, remote: RemoteRepo
) -> None:
    repository_id = _register_github(client)
    sha = _ok(_refs(client, repository_id))["head_sha"]
    assert _ok(_commits(client, repository_id))["items"][0]["imported"] is False

    _import(client, repository_id, sha)

    assert _ok(_commits(client, repository_id))["items"][0]["imported"] is True


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_github_tampered_path_returns_422(
    client: TestClient,
    remote: RemoteRepo,
    managed_dir: Path,
    tmp_path: Path,
    db_engine: Engine,
    endpoint: str,
) -> None:
    repository_id = _register_github(client)
    _update_row(db_engine, repository_id, canonical_local_path=str(init_repo(tmp_path / "evil")))

    response = client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_github_path_to_another_managed_location_returns_422(
    client: TestClient, remote: RemoteRepo, managed_dir: Path, db_engine: Engine, endpoint: str
) -> None:
    repository_id = _register_github(client)
    other = init_repo(managed_dir / "github" / "octocat" / "Other")
    _update_row(db_engine, repository_id, canonical_local_path=str(other))

    response = client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


@pytest.mark.parametrize("endpoint", ["refs", "commits"])
def test_github_without_managed_dir_setting_returns_422(
    remote: RemoteRepo,
    client: TestClient,
    settings: Settings,
    endpoint: str,
    clean_repositories: None,
) -> None:
    repository_id = _register_github(client)
    without_managed = settings.model_copy(update={"managed_repos_dir": None})

    with TestClient(create_app(without_managed)) as other_client:
        response = other_client.get(f"{URL}/{repository_id}/{endpoint}")

    assert (response.status_code, _error_code(response)) == (422, "invalid_repository_path")


def test_github_removed_repository_returns_404(client: TestClient, remote: RemoteRepo) -> None:
    repository_id = _register_github(client)
    assert client.delete(f"{URL}/{repository_id}").status_code == 200

    assert _refs(client, repository_id).status_code == 404
    assert _commits(client, repository_id).status_code == 404


def test_commits_authored_at_is_reported_in_utc_regardless_of_the_author_timezone(
    client: TestClient, local_repo: Path
) -> None:
    sha = _empty_commit(local_repo, "tz", date="2090-05-06T07:08:09-05:00")
    repository_id = _register(client, local_repo)

    item = next(c for c in _ok(_commits(client, repository_id))["items"] if c["sha"] == sha)

    assert item["authored_at"] == datetime(2090, 5, 6, 12, 8, 9, tzinfo=UTC).isoformat().replace(
        "+00:00", "Z"
    )


def test_all_registered_commit_refs_are_listed_in_refs_endpoint_too(
    client: TestClient, local_repo: Path
) -> None:
    """`ref` 的契約：commits 接受的值就是 refs 回傳的 `ref`。"""
    git(local_repo, "branch", "feature/x")
    git(local_repo, "tag", "-a", "v1", "-m", "r")
    repository_id = _register(client, local_repo)
    refs = _ok(_refs(client, repository_id))

    for entry in [*refs["branches"], *refs["tags"]]:
        body = _ok(_commits(client, repository_id, ref=entry["ref"]))
        assert body["items"][0]["sha"] == entry["sha"]
