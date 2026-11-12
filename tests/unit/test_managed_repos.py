import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from repopilot.errors import InvalidRepositoryPathError, ManagedStorageError
from repopilot.services import managed_repos
from tests.git_helpers import link_directory


@pytest.fixture
def managed_root(tmp_path: Path) -> Path:
    return managed_repos.prepare_managed_root(tmp_path / "managed")


def test_prepare_creates_directory_lazily_and_returns_resolved_path(tmp_path: Path) -> None:
    target = tmp_path / "a" / "managed"
    assert not target.exists()

    root = managed_repos.prepare_managed_root(target)

    assert root == target.resolve()
    assert root.is_dir()


def test_prepare_fails_with_domain_error_when_path_is_a_file(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")

    with pytest.raises(ManagedStorageError):
        managed_repos.prepare_managed_root(file)


def test_temp_dirs_are_unique_children_of_dot_tmp(managed_root: Path) -> None:
    first = managed_repos.create_temp_dir(managed_root)
    second = managed_repos.create_temp_dir(managed_root)

    assert first != second
    assert first.parent == second.parent == managed_root / ".tmp"
    assert first.is_dir()
    assert len(first.name) == 16


def test_github_destination_layout(managed_root: Path) -> None:
    assert managed_repos.github_destination(managed_root, "Octo", "Repo") == (
        managed_root / "github" / "Octo" / "Repo"
    )


def test_publish_renames_atomically_and_never_overwrites(managed_root: Path) -> None:
    temp = managed_repos.create_temp_dir(managed_root)
    (temp / "marker").write_text("new", encoding="utf-8")
    destination = managed_repos.github_destination(managed_root, "o", "n")

    managed_repos.publish(temp, destination)

    assert not temp.exists()
    assert (destination / "marker").read_text(encoding="utf-8") == "new"

    other = managed_repos.create_temp_dir(managed_root)
    with pytest.raises(FileExistsError):
        managed_repos.publish(other, destination)
    assert (destination / "marker").read_text(encoding="utf-8") == "new"
    assert other.exists()


def test_remove_created_tree_deletes_nested_content_including_readonly_files(
    managed_root: Path,
) -> None:
    temp = managed_repos.create_temp_dir(managed_root)
    objects = temp / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    readonly = objects / "cdef"
    readonly.write_text("packed", encoding="utf-8")
    readonly.chmod(stat.S_IREAD)  # Windows 上 Git 的物件檔是唯讀的

    managed_repos.remove_created_tree(
        temp, managed_root=managed_root, expected_parent=managed_repos.temp_root(managed_root)
    )

    assert not temp.exists()
    assert (managed_root / ".tmp").exists()


def test_remove_refuses_paths_whose_parent_is_not_the_expected_parent(
    managed_root: Path, tmp_path: Path
) -> None:
    victim = tmp_path / "outside" / "precious"
    victim.mkdir(parents=True)
    (victim / "data.txt").write_text("keep", encoding="utf-8")
    managed_repos.create_temp_dir(managed_root)

    managed_repos.remove_created_tree(
        victim, managed_root=managed_root, expected_parent=managed_repos.temp_root(managed_root)
    )

    assert (victim / "data.txt").read_text(encoding="utf-8") == "keep"


def test_remove_refuses_expected_parent_outside_managed_root(
    managed_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    victim = outside / "precious"
    victim.mkdir(parents=True)

    managed_repos.remove_created_tree(victim, managed_root=managed_root, expected_parent=outside)

    assert victim.exists()


def test_remove_refuses_to_delete_direct_children_of_the_managed_root(managed_root: Path) -> None:
    child = managed_root / "github"
    child.mkdir()

    managed_repos.remove_created_tree(
        child, managed_root=managed_root, expected_parent=managed_root
    )

    assert child.exists()


def test_remove_refuses_the_managed_root_itself(managed_root: Path, tmp_path: Path) -> None:
    managed_repos.remove_created_tree(
        managed_root, managed_root=managed_root, expected_parent=managed_root.parent
    )

    assert managed_root.exists()


def test_remove_refuses_dot_dot_names(managed_root: Path) -> None:
    temp_root = managed_repos.temp_root(managed_root)
    temp_root.mkdir()
    sibling = managed_root / "sibling"
    sibling.mkdir()
    (sibling / "keep").write_text("x", encoding="utf-8")

    managed_repos.remove_created_tree(
        temp_root / ".." / "sibling", managed_root=managed_root, expected_parent=temp_root
    )
    managed_repos.remove_created_tree(
        temp_root / "..", managed_root=managed_root, expected_parent=temp_root
    )

    assert (sibling / "keep").exists()
    assert managed_root.exists()


def test_remove_missing_path_is_a_noop(managed_root: Path) -> None:
    temp_root = managed_repos.temp_root(managed_root)
    temp_root.mkdir()

    managed_repos.remove_created_tree(
        temp_root / "gone", managed_root=managed_root, expected_parent=temp_root
    )


@pytest.mark.skipif(os.name != "nt", reason="junction 只存在於 Windows")
def test_remove_does_not_follow_a_junction_into_other_directories(
    managed_root: Path, tmp_path: Path
) -> None:
    import subprocess

    target = tmp_path / "outside-target"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    temp_root = managed_repos.temp_root(managed_root)
    temp_root.mkdir()
    junction = temp_root / "link"
    completed = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("無法建立 junction")

    managed_repos.remove_created_tree(
        junction, managed_root=managed_root, expected_parent=temp_root
    )

    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


# --- remove_github_clone（D33：移除已註冊的 GitHub repo 的 clone） ---


def _make_clone(managed_root: Path, owner: str = "octo", name: str = "repo") -> Path:
    clone = managed_repos.github_destination(managed_root, owner, name)
    objects = clone / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    packed = objects / "cdef"
    packed.write_text("packed", encoding="utf-8")
    packed.chmod(stat.S_IREAD)  # Windows 上 Git 的物件檔是唯讀的
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return clone


def _remove(managed_root: Path, owner: str = "octo", name: str = "repo") -> bool:
    return managed_repos.remove_github_clone(
        managed_root,
        owner=owner,
        name=name,
        recorded_path=str(managed_repos.github_destination(managed_root, owner, name)),
    )


def test_remove_github_clone_deletes_readonly_files_and_the_empty_owner_directory(
    managed_root: Path,
) -> None:
    clone = _make_clone(managed_root)

    assert _remove(managed_root) is True

    assert not clone.exists()
    assert not (managed_root / "github" / "octo").exists()
    # 受管目錄與 github 目錄本身絕不刪除
    assert (managed_root / "github").is_dir()
    assert managed_root.is_dir()


def test_remove_github_clone_keeps_the_owner_directory_while_other_repos_remain(
    managed_root: Path,
) -> None:
    _make_clone(managed_root, "octo", "repo")
    sibling = _make_clone(managed_root, "octo", "sibling")
    other_owner = _make_clone(managed_root, "someone", "else")

    assert _remove(managed_root) is True

    assert (sibling / ".git" / "HEAD").exists()
    assert (other_owner / ".git" / "HEAD").exists()
    assert not (managed_root / "github" / "octo" / "repo").exists()


def test_remove_github_clone_treats_a_missing_directory_as_success(managed_root: Path) -> None:
    assert _remove(managed_root) is False
    assert managed_root.is_dir()


def test_remove_github_clone_works_when_the_managed_dir_itself_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "never-created"

    assert _remove(missing) is False
    assert not missing.exists()


@pytest.mark.parametrize(
    ("owner", "name"),
    [("..", "repo"), ("octo", ".."), (".", "repo"), ("octo", "."), ("octo", "a/b"), ("o\\x", "r")],
)
def test_remove_github_clone_refuses_unsafe_names_without_deleting_anything(
    managed_root: Path, owner: str, name: str
) -> None:
    victim = _make_clone(managed_root, "octo", "repo")
    github_dir = managed_root / "github"
    recorded = f"{github_dir}{os.sep}{owner}{os.sep}{name}"

    with pytest.raises(InvalidRepositoryPathError):
        managed_repos.remove_github_clone(
            managed_root, owner=owner, name=name, recorded_path=recorded
        )

    assert (victim / ".git" / "HEAD").exists()
    assert github_dir.is_dir()


@pytest.mark.parametrize("where", ["outside", "other_clone", "managed_root"])
def test_remove_github_clone_refuses_a_recorded_path_that_is_not_the_expected_location(
    managed_root: Path, tmp_path: Path, where: str
) -> None:
    _make_clone(managed_root, "octo", "repo")
    other = _make_clone(managed_root, "octo", "other")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    recorded = {"outside": outside, "other_clone": other, "managed_root": managed_root}[where]

    with pytest.raises(InvalidRepositoryPathError):
        managed_repos.remove_github_clone(
            managed_root, owner="octo", name="repo", recorded_path=str(recorded)
        )

    assert (outside / "keep.txt").exists()
    assert (other / ".git" / "HEAD").exists()
    assert (managed_root / "github" / "octo" / "repo" / ".git" / "HEAD").exists()


def test_remove_github_clone_ignores_case_differences_in_the_recorded_path_on_windows(
    managed_root: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("大小寫不敏感只適用於 Windows")
    _make_clone(managed_root)
    recorded = str(managed_repos.github_destination(managed_root, "octo", "repo")).upper()

    removed = managed_repos.remove_github_clone(
        managed_root, owner="octo", name="repo", recorded_path=recorded
    )

    assert removed is True


@pytest.mark.parametrize("link_at", ["clone", "owner", "github"])
def test_remove_github_clone_refuses_links_and_never_touches_the_target(
    managed_root: Path, tmp_path: Path, link_at: str
) -> None:
    outside = tmp_path / "outside"
    (outside / "octo" / "repo").mkdir(parents=True)
    victim = outside / "octo" / "repo" / "keep.txt"
    victim.write_text("keep", encoding="utf-8")
    github_dir = managed_root / "github"
    if link_at == "clone":
        (github_dir / "octo").mkdir(parents=True)
        link_directory(github_dir / "octo" / "repo", outside / "octo" / "repo")
    elif link_at == "owner":
        github_dir.mkdir()
        link_directory(github_dir / "octo", outside / "octo")
    else:
        link_directory(github_dir, outside)

    with pytest.raises(InvalidRepositoryPathError):
        _remove(managed_root)

    assert victim.read_text(encoding="utf-8") == "keep"


def test_remove_github_clone_does_not_follow_a_link_inside_the_clone(
    managed_root: Path, tmp_path: Path
) -> None:
    clone = _make_clone(managed_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link_directory(clone / ".git" / "linked", outside)

    assert _remove(managed_root) is True

    assert not clone.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_remove_github_clone_refuses_a_file_at_the_clone_path(managed_root: Path) -> None:
    clone = managed_repos.github_destination(managed_root, "octo", "repo")
    clone.parent.mkdir(parents=True)
    clone.write_text("not a directory", encoding="utf-8")

    with pytest.raises(InvalidRepositoryPathError):
        _remove(managed_root)

    assert clone.read_text(encoding="utf-8") == "not a directory"


def test_remove_github_clone_reports_storage_error_without_leaking_the_path(
    managed_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = _make_clone(managed_root)

    def fail(*_: object, **__: object) -> None:
        raise PermissionError(13, "locked", str(clone))

    monkeypatch.setattr(shutil, "rmtree", fail)

    with pytest.raises(ManagedStorageError) as raised:
        _remove(managed_root)

    assert str(clone) not in raised.value.message
    assert (clone / ".git" / "HEAD").exists()


# --- 上一個 attempt 的暫存目錄與孤兒清理（第二階段切片 5，D44）---


def _hex_dir(root: Path, name: str, *, age_seconds: float = 0) -> Path:
    path = root / ".tmp" / name
    (path / ".git" / "objects").mkdir(parents=True)
    target = path / ".git" / "objects" / "pack.idx"
    target.write_bytes(b"x")
    target.chmod(stat.S_IREAD)  # Windows 的唯讀屬性（Git 物件常見）
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def test_recorded_temp_dir_is_removed_including_readonly_files(managed_root: Path) -> None:
    leftover = _hex_dir(managed_root, "0123456789abcdef")

    assert managed_repos.remove_recorded_temp_dir(managed_root, "0123456789abcdef") is True

    assert not leftover.exists()
    assert (managed_root / ".tmp").is_dir()  # `.tmp` 本身不動


def test_recorded_temp_dir_that_is_already_gone_is_not_an_error(managed_root: Path) -> None:
    assert managed_repos.remove_recorded_temp_dir(managed_root, "0123456789abcdef") is False
    assert managed_repos.remove_recorded_temp_dir(managed_root, None) is False


TAMPERED_RECORDS: list[object] = [
    "..",
    ".",
    "",
    "../victim",
    "../../victim",
    "0123456789abcdef/..",
    "0123456789abcdef/x",  # 子路徑：只接受單一名稱
    "..\\victim",
    "/etc",
    "C:\\Windows",
    "0123456789ABCDEF",  # 大寫：不是 `secrets.token_hex` 的格式
    "0123456789abcde",  # 15 碼
    "0123456789abcdef0",  # 17 碼
    "0123456789abcdeg",  # 非 hex
    "0123456789abcdef\n",
    "x" * 1000,
    123,
    ["0123456789abcdef"],
    {"temp_dir": "0123456789abcdef"},
    True,
]


@pytest.mark.parametrize("recorded", TAMPERED_RECORDS)
def test_a_tampered_record_never_deletes_anything(managed_root: Path, recorded: object) -> None:
    victim = managed_root.parent / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep", encoding="utf-8")
    sibling = _hex_dir(managed_root, "fedcba9876543210")
    github = managed_root / "github" / "octo" / "repo"
    github.mkdir(parents=True)
    before = sorted(str(p.relative_to(managed_root.parent)) for p in managed_root.parent.rglob("*"))

    assert managed_repos.remove_recorded_temp_dir(managed_root, recorded) is False

    after = sorted(str(p.relative_to(managed_root.parent)) for p in managed_root.parent.rglob("*"))
    assert after == before
    assert (victim / "precious.txt").exists()
    assert sibling.exists()


@pytest.mark.parametrize("recorded", TAMPERED_RECORDS)
def test_a_tampered_record_is_rejected_before_any_deletion_is_attempted(
    managed_root: Path, recorded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 第一層（名稱格式）本身就要擋下：不依賴 remove_created_tree 的第二層驗證
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("a deletion was attempted for a tampered record")

    monkeypatch.setattr(managed_repos, "remove_created_tree", forbidden)
    monkeypatch.setattr(shutil, "rmtree", forbidden)

    assert managed_repos.remove_recorded_temp_dir(managed_root, recorded) is False


def test_a_recorded_symlink_or_junction_is_neither_followed_nor_removed(
    managed_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (managed_root / ".tmp").mkdir()
    link_directory(managed_root / ".tmp" / "0123456789abcdef", outside)

    assert managed_repos.remove_recorded_temp_dir(managed_root, "0123456789abcdef") is False

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert os.path.lexists(managed_root / ".tmp" / "0123456789abcdef")  # 連結本身也沒被刪


def test_a_symlinked_dot_tmp_is_refused(managed_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "0123456789abcdef").mkdir(parents=True)
    (outside / "0123456789abcdef" / "keep.txt").write_text("keep", encoding="utf-8")
    link_directory(managed_root / ".tmp", outside)

    assert managed_repos.remove_recorded_temp_dir(managed_root, "0123456789abcdef") is False
    assert managed_repos.sweep_orphan_temp_dirs(managed_root, older_than_seconds=0) == []

    assert (outside / "0123456789abcdef" / "keep.txt").exists()


def test_sweep_removes_only_old_unreferenced_dirs_named_like_ours(managed_root: Path) -> None:
    old = _hex_dir(managed_root, "a" * 16, age_seconds=10_000)
    young = _hex_dir(managed_root, "b" * 16, age_seconds=10)
    referenced = _hex_dir(managed_root, "c" * 16, age_seconds=10_000)
    stranger = managed_root / ".tmp" / "someone-elses-dir"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("keep", encoding="utf-8")
    old_time = time.time() - 10_000
    os.utime(stranger, (old_time, old_time))
    stray_file = managed_root / ".tmp" / ("d" * 16)
    stray_file.write_text("a file named like ours", encoding="utf-8")
    os.utime(stray_file, (old_time, old_time))

    removed = managed_repos.sweep_orphan_temp_dirs(
        managed_root, older_than_seconds=3_600, keep={"c" * 16}
    )

    assert removed == ["a" * 16]
    assert not old.exists()
    assert young.exists()
    assert referenced.exists()
    assert (stranger / "keep.txt").exists()
    assert stray_file.read_text(encoding="utf-8") == "a file named like ours"


def test_sweep_age_boundary_uses_the_directory_modification_time(managed_root: Path) -> None:
    directory = _hex_dir(managed_root, "a" * 16, age_seconds=100)
    now = time.time()

    assert managed_repos.sweep_orphan_temp_dirs(managed_root, older_than_seconds=200, now=now) == []
    assert directory.exists()
    assert managed_repos.sweep_orphan_temp_dirs(managed_root, older_than_seconds=50, now=now) == [
        "a" * 16
    ]


def test_sweep_without_a_dot_tmp_dir_does_nothing(managed_root: Path) -> None:
    assert managed_repos.sweep_orphan_temp_dirs(managed_root, older_than_seconds=0) == []
    assert not (managed_root / ".tmp").exists()  # 清理不會順手建立目錄


def test_new_temp_path_is_not_created_until_asked(managed_root: Path) -> None:
    path = managed_repos.new_temp_path(managed_root)

    assert not path.exists()
    assert managed_repos.TEMP_NAME.fullmatch(path.name)
    managed_repos.create_temp_dir_at(path)
    assert path.is_dir()
    with pytest.raises(ManagedStorageError):  # 不帶 exist_ok：一定是本次建立的
        managed_repos.create_temp_dir_at(path)


def test_directory_size_sums_nested_files_and_ignores_links(
    managed_root: Path, tmp_path: Path
) -> None:
    directory = managed_root / "measured"
    (directory / "sub").mkdir(parents=True)
    (directory / "a.bin").write_bytes(b"x" * 100)
    (directory / "sub" / "b.bin").write_bytes(b"y" * 250)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"z" * 10_000)
    link_directory(directory / "link", outside)

    assert managed_repos.directory_size(directory) == 350
    assert managed_repos.directory_size(directory / "missing") == 0
