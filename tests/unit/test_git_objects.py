from pathlib import Path

import pytest

from repopilot.errors import GitCommandError, InvalidRevisionError
from repopilot.git.objects import list_tree, read_blobs, resolve_commit
from tests.git_helpers import commit_files, commit_special_entries, git, init_repo


def test_resolve_commit_accepts_head_branch_tag_and_abbreviation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v1")

    assert resolve_commit(repo, "HEAD", timeout=30) == sha
    assert resolve_commit(repo, "main", timeout=30) == sha
    assert resolve_commit(repo, "v1", timeout=30) == sha
    assert resolve_commit(repo, sha[:10], timeout=30) == sha


def test_resolve_commit_peels_annotated_tag_to_commit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    git(repo, "tag", "-a", "v2", "-m", "release")

    assert resolve_commit(repo, "v2", timeout=30) == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "revision",
    [
        "",
        "-h",
        "--all",
        "--output=x",
        "HEAD\nmain",
        "HEAD\x00",
        "HEAD\x1b",
        "no-such-branch",
        "HEAD^{tree}",
        "HEAD:README.md",
        "x" * 300,
    ],
)
def test_resolve_commit_rejects_invalid_revisions(tmp_path: Path, revision: str) -> None:
    repo = init_repo(tmp_path / "demo")

    with pytest.raises(InvalidRevisionError):
        resolve_commit(repo, revision, timeout=30)


def test_resolve_commit_rejects_unborn_head(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")

    with pytest.raises(InvalidRevisionError):
        resolve_commit(repo, "HEAD", timeout=30)


def test_list_tree_handles_spaces_unicode_and_nested_paths(tmp_path: Path) -> None:
    files = {"a b.txt": "1", "目錄/檔 案.py": "22", "x/y/z.txt": "333", "it's #1.txt": "4"}
    repo = init_repo(tmp_path / "demo", files=files)
    sha = git(repo, "rev-parse", "HEAD")

    entries = {entry.path: entry for entry in list_tree(repo, sha, timeout=30)}

    assert set(entries) == set(files)
    assert entries["x/y/z.txt"].size == 3
    assert entries["目錄/檔 案.py"].mode == "100644"
    assert entries["a b.txt"].object_type == "blob"


def test_list_tree_reports_symlink_and_gitlink(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = commit_special_entries(
        repo, symlinks={"link": "README.md"}, gitlinks={"vendor/lib": "a" * 40}
    )

    entries = {entry.path: entry for entry in list_tree(repo, sha, timeout=30)}

    assert entries["link"].mode == "120000"
    assert entries["link"].size == len("README.md")
    assert entries["vendor/lib"].mode == "160000"
    assert entries["vendor/lib"].object_type == "commit"
    assert entries["vendor/lib"].size is None


def test_list_tree_uses_commit_content_not_working_tree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo", files={"a.txt": "committed\n"})
    sha = git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("changed in working tree, much longer\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("x", encoding="utf-8")

    entries = list_tree(repo, sha, timeout=30)

    assert [(entry.path, entry.size) for entry in entries] == [("a.txt", len("committed\n"))]


def test_list_tree_rejects_non_hex_commit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")

    with pytest.raises(ValueError, match="commit_sha"):
        list_tree(repo, "--all", timeout=30)


def test_read_blobs_returns_exact_bytes_including_nul_and_crlf(tmp_path: Path) -> None:
    payloads = {"bin.dat": b"\x00\x01\xff\n", "win.txt": b"a\r\nb\r\n", "empty.txt": b""}
    repo = init_repo(tmp_path / "demo", files={"seed.txt": "x"})
    sha = commit_files(repo, payloads)
    entries = {entry.path: entry for entry in list_tree(repo, sha, timeout=30)}
    expected = {entries[name].object_id: len(data) for name, data in payloads.items()}

    blobs = read_blobs(repo, expected, timeout=30)

    for name, data in payloads.items():
        assert blobs[entries[name].object_id] == data


def test_read_blobs_with_no_objects_does_not_call_git(tmp_path: Path) -> None:
    assert read_blobs(tmp_path / "missing", {}, timeout=30) == {}


def test_read_blobs_rejects_size_mismatch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo", files={"a.txt": "12345"})
    sha = git(repo, "rev-parse", "HEAD")
    entry = list_tree(repo, sha, timeout=30)[0]

    with pytest.raises(GitCommandError, match="unexpected size"):
        read_blobs(repo, {entry.object_id: 999}, timeout=30)


def test_read_blobs_reports_missing_object(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")

    with pytest.raises(GitCommandError):
        read_blobs(repo, {"b" * 40: 1}, timeout=30)
