"""advisory lock 的鍵空間（切片 3、5，D40、D44）：純函式，不需要資料庫。

GitHub 的 clone／fetch／移除／匯入必須取到**同一把** repo 鎖：註冊階段資料列不存在（只有
owner／name），註冊後有 id，但 `repository_key()` 一律用不分大小寫的 owner／name，
所以前後是同一個鍵。
"""

import pytest

from repopilot.db.locks import lock_key, repo_lock_key, snapshot_lock_key
from repopilot.db.models import Repository
from repopilot.services.repositories import github_repo_key, repository_key


def _github_row(owner: str, name: str, repository_id: int = 7) -> Repository:
    row = Repository(
        name=f"{owner}/{name}",
        canonical_local_path="x",
        source_type="github",
        github_owner=owner,
        github_name=name,
    )
    row.id = repository_id
    return row


@pytest.mark.parametrize(
    ("owner", "name"),
    [("octocat", "Hello-World"), ("OCTOCAT", "hello-world"), ("OctoCat", "HELLO-world")],
)
def test_registration_and_the_registered_row_share_one_repo_key(owner: str, name: str) -> None:
    before_row_exists = repo_lock_key(github_repo_key(owner, name))
    after_row_exists = repo_lock_key(repository_key(_github_row("octocat", "Hello-World", 42)))
    assert before_row_exists == after_row_exists


def test_the_github_repo_key_is_lowercase_and_not_the_database_id() -> None:
    assert github_repo_key("OctoCat", "Hello-World") == "github:octocat/hello-world"
    assert repository_key(_github_row("OctoCat", "Hello-World", 1)) == (
        repository_key(_github_row("OctoCat", "Hello-World", 999))
    )


def test_local_and_github_keys_never_collide_and_different_repos_differ() -> None:
    local = Repository(name="x", canonical_local_path="x")
    local.id = 1
    keys = {
        repo_lock_key(repository_key(local)),
        repo_lock_key(github_repo_key("a", "b")),
        repo_lock_key(github_repo_key("a", "c")),
        repo_lock_key(github_repo_key("b", "b")),
    }
    assert len(keys) == 4


def test_namespaces_keep_repo_and_snapshot_keys_apart() -> None:
    assert repo_lock_key("local:1") != snapshot_lock_key(1, "abc", "def")
    assert lock_key("repo", "local:1") != lock_key("snapshot", "local:1")


def test_keys_are_signed_bigint_and_stable() -> None:
    key = repo_lock_key(github_repo_key("octocat", "Hello-World"))

    assert -(2**63) <= key < 2**63
    assert key == repo_lock_key("github:octocat/hello-world")  # 同樣的輸入永遠同一個鍵
