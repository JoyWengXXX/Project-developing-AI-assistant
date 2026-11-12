"""git/history.py：refs 與 commit 歷史的輸出解析、ref 驗證與真實 Git 查詢（切片 5e-1，D34）。

解析函式以手寫的位元組輸入測試（含畸形輸入）；查詢以真實暫存 Git repo 測試，不需資料庫。
"""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from repopilot.errors import GitCommandError, InvalidRevisionError
from repopilot.git import history
from repopilot.git.history import (
    LOCAL_BRANCHES,
    ORIGIN_BRANCHES,
    epoch_to_utc,
    find_ref,
    head_commit,
    list_refs,
    parse_commit_records,
    parse_ref_records,
    read_commits,
)
from tests.git_helpers import git, init_repo

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA256 = "d" * 64
NUL = b"\x00"


def _ref_record(
    refname: str = "refs/heads/main",
    object_id: str = SHA_A,
    object_type: str = "commit",
    peeled_id: str = "",
    peeled_type: str = "",
    symref: str = "",
    head: bytes = b" ",
) -> bytes:
    return NUL.join(
        [
            refname.encode("utf-8"),
            object_id.encode(),
            object_type.encode(),
            peeled_id.encode(),
            peeled_type.encode(),
            symref.encode("utf-8"),
            head,
        ]
    )


def _commit_fields(
    sha: str = SHA_A,
    parents: str = "",
    author: bytes = b"Alice",
    time: str = "1700000000",
    subject: bytes = b"hello",
) -> list[bytes]:
    return [sha.encode(), parents.encode(), author, time.encode(), subject]


def _commit_output(*commits: list[bytes]) -> bytes:
    return b"".join(NUL.join(fields) + NUL for fields in commits)


# --- parse_ref_records ---


def test_parse_ref_records_reads_branch_tag_and_head_marker() -> None:
    data = b"\n".join(
        [
            _ref_record("refs/heads/main", head=b"*"),
            _ref_record("refs/tags/v2", SHA_B, "tag", SHA_A, "commit"),
        ]
    )

    refs = parse_ref_records(data)

    assert [r.refname for r in refs] == ["refs/heads/main", "refs/tags/v2"]
    assert refs[0].is_head is True
    assert refs[1].is_head is False
    assert (refs[1].object_id, refs[1].peeled_id, refs[1].peeled_type) == (SHA_B, SHA_A, "commit")


def test_parse_ref_records_empty_output_is_empty_list() -> None:
    assert parse_ref_records(b"") == []
    assert parse_ref_records(b"\n\n") == []


def test_parse_ref_records_accepts_sha256_object_ids() -> None:
    assert parse_ref_records(_ref_record(object_id=SHA256))[0].object_id == SHA256


def test_parse_ref_records_keeps_unicode_and_special_characters_in_names() -> None:
    names = ["refs/heads/功能/中文.x", "refs/heads/a;b$c%d&e'f", "refs/heads/emoji-😀"]

    refs = parse_ref_records(b"\n".join(_ref_record(n) for n in names))

    assert [r.refname for r in refs] == names


def test_parse_ref_records_skips_refs_whose_name_is_not_utf8() -> None:
    bad = NUL.join([b"refs/heads/\xff\xfe", SHA_A.encode(), b"commit", b"", b"", b"", b" "])
    data = b"\n".join([bad, _ref_record("refs/heads/ok")])

    assert [r.refname for r in parse_ref_records(data)] == ["refs/heads/ok"]


@pytest.mark.parametrize(
    "record",
    [
        b"refs/heads/main",  # 欄位不足
        NUL.join([b"refs/heads/main", SHA_A.encode(), b"commit"]),
        _ref_record() + NUL + b"extra",  # 欄位過多
        _ref_record(object_id="not-a-sha"),
        _ref_record(object_id="A" * 40),  # 大寫不是 Git 的輸出格式
        _ref_record(object_id="a" * 39),
        _ref_record("refs/tags/v", SHA_B, "tag", "zz", "commit"),
        _ref_record("heads/main"),  # 不是完整 ref
    ],
)
def test_parse_ref_records_rejects_malformed_records(record: bytes) -> None:
    with pytest.raises(GitCommandError):
        parse_ref_records(record)


# --- parse_commit_records ---


def test_parse_commit_records_reads_root_linear_and_merge_commits() -> None:
    data = _commit_output(
        _commit_fields(SHA_A, f"{SHA_B} {SHA_C}", subject=b"merge"),
        _commit_fields(SHA_B, SHA_C, author=b"Bob"),
        _commit_fields(SHA_C, ""),
    )

    records = parse_commit_records(data)

    assert [r.sha for r in records] == [SHA_A, SHA_B, SHA_C]
    assert records[0].parents == (SHA_B, SHA_C)
    assert records[1].parents == (SHA_C,)
    assert records[2].parents == ()
    assert records[0].subject == "merge"
    assert records[1].author_name == "Bob"
    assert records[0].authored_at == datetime.fromtimestamp(1700000000, UTC)


def test_parse_commit_records_supports_octopus_merges() -> None:
    parents = " ".join([SHA_B, SHA_C, SHA_A, SHA256[:40]])

    records = parse_commit_records(_commit_output(_commit_fields(SHA_A, parents)))

    assert len(records[0].parents) == 4


def test_parse_commit_records_empty_output_is_empty_list() -> None:
    assert parse_commit_records(b"") == []


def test_parse_commit_records_truncates_long_subject_and_author() -> None:
    data = _commit_output(
        _commit_fields(subject=("長" * 500).encode(), author=("A" * 500).encode())
    )

    record = parse_commit_records(data)[0]

    assert record.subject == "長" * history.SUBJECT_MAX_CHARS
    assert len(record.author_name) == history.AUTHOR_MAX_CHARS


def test_parse_commit_records_handles_empty_subject_and_author() -> None:
    record = parse_commit_records(_commit_output(_commit_fields(subject=b"", author=b"")))[0]

    assert (record.subject, record.author_name) == ("", "")


def test_parse_commit_records_does_not_misalign_when_fields_contain_newlines_or_quotes() -> None:
    data = _commit_output(
        _commit_fields(SHA_A, subject=b'line1\nline2 "q" \\ %s \r tab\t'),
        _commit_fields(SHA_B, author="名字\n換行".encode()),
    )

    records = parse_commit_records(data)

    assert [r.sha for r in records] == [SHA_A, SHA_B]
    assert records[0].subject == 'line1\nline2 "q" \\ %s \r tab\t'
    assert records[1].author_name == "名字\n換行"


def test_parse_commit_records_replaces_invalid_utf8() -> None:
    record = parse_commit_records(_commit_output(_commit_fields(subject=b"bad \xff byte")))[0]

    assert record.subject == "bad \ufffd byte"


def test_parse_commit_records_rejects_misaligned_field_counts() -> None:
    fields = _commit_fields()

    with pytest.raises(GitCommandError):
        parse_commit_records(NUL.join(fields[:4]) + NUL)  # 少一個欄位
    with pytest.raises(GitCommandError):
        parse_commit_records(NUL.join([*fields, b"extra"]) + NUL)  # 多一個欄位


@pytest.mark.parametrize(
    "fields",
    [
        _commit_fields(sha="not-a-sha"),
        _commit_fields(sha="a" * 41),
        _commit_fields(parents="xyz"),
        _commit_fields(parents=f"{SHA_B} nope"),
    ],
)
def test_parse_commit_records_rejects_invalid_identifiers(fields: list[bytes]) -> None:
    with pytest.raises(GitCommandError):
        parse_commit_records(_commit_output(fields))


# --- epoch_to_utc ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", datetime(1970, 1, 1, tzinfo=UTC)),
        ("1700000000", datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        ("-86400", datetime(1969, 12, 31, tzinfo=UTC)),
        ("", datetime(1970, 1, 1, tzinfo=UTC)),
        ("abc", datetime(1970, 1, 1, tzinfo=UTC)),
        ("1.5", datetime(1970, 1, 1, tzinfo=UTC)),
        ("+5", datetime(1970, 1, 1, tzinfo=UTC)),
    ],
)
def test_epoch_to_utc_converts_and_tolerates_malformed_values(raw: str, expected: datetime) -> None:
    assert epoch_to_utc(raw) == expected


def test_epoch_to_utc_clamps_out_of_range_values_instead_of_failing() -> None:
    assert epoch_to_utc("9" * 20) == datetime.max.replace(tzinfo=UTC)
    assert epoch_to_utc("-" + "9" * 20) == datetime.min.replace(tzinfo=UTC)
    assert epoch_to_utc("99999999999999") == datetime.max.replace(tzinfo=UTC)


# --- ref 驗證：語法錯誤在呼叫 Git 之前就被拒絕 ---

_NOWHERE = Path("this-path-does-not-exist")


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "HEAD",
        "main",
        "HEAD~1",
        "HEAD^",
        "refs/heads/main..refs/heads/dev",
        "refs/heads/main...dev",
        "--output=x",
        "--all",
        "-h",
        " refs/heads/main",
        "refs/heads/",
        "refs/heads/../x",
        "refs/heads/a/../../b",
        "refs/heads/a b",
        "refs/heads/a\tb",
        "refs/heads/a\nb",
        "refs/heads/a\x00b",
        "refs/heads/a\x1bb",
        "refs/heads/a\x7fb",
        "refs/heads/a*",
        "refs/heads/a?",
        "refs/heads/[a]",
        "refs/heads/a~1",
        "refs/heads/a^2",
        "refs/heads/a:b",
        "refs/heads/a\\b",
        "refs/heads/a@{1}",
        "refs/heads//a",
        "refs/heads/a.",
        "refs/heads/\ud800",
        "refs/heads/" + "x" * 2000,
        "refs/stash",
        "refs/notes/commits",
        "refs/remotes/origin/main",  # 本機來源不接受 origin 命名空間
        "a" * 40,
        "REFS/HEADS/main",
    ],
)
def test_find_ref_rejects_bad_syntax_without_running_git(ref: str) -> None:
    # 路徑不存在：若程式碼呼叫了 Git，會得到 GitCommandError 而不是 InvalidRevisionError
    with pytest.raises(InvalidRevisionError):
        find_ref(_NOWHERE, ref, branch_namespace=LOCAL_BRANCHES, timeout=30)


def test_find_ref_rejects_local_branches_for_github_namespace() -> None:
    with pytest.raises(InvalidRevisionError):
        find_ref(_NOWHERE, "refs/heads/main", branch_namespace=ORIGIN_BRANCHES, timeout=30)


def test_branch_namespace_must_be_known() -> None:
    with pytest.raises(ValueError, match="branch_namespace"):
        list_refs(_NOWHERE, branch_namespace="refs/heads", timeout=30)


# --- 真實 Git：list_refs / find_ref / head_commit ---


def _empty_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "--quiet", "--initial-branch=main")
    return path


def _empty_commit(repo: Path, message: str = "c") -> str:
    git(repo, "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", message)
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


def test_list_refs_local_branches_tags_and_current_branch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "dev")
    git(repo, "tag", "v1")
    git(repo, "tag", "-a", "v2", "-m", "annotated")

    listing = list_refs(repo, branch_namespace=LOCAL_BRANCHES, timeout=30)

    assert [(b.ref, b.name, b.sha, b.is_head) for b in listing.branches] == [
        ("refs/heads/dev", "dev", sha, False),
        ("refs/heads/main", "main", sha, True),
    ]
    # 附註標籤（v2）的 SHA 還原為 commit，不是 tag 物件的 SHA
    assert [(t.name, t.sha) for t in listing.tags] == [("v1", sha), ("v2", sha)]
    assert git(repo, "rev-parse", "v2") != sha
    assert listing.truncated is False


def test_list_refs_skips_tags_pointing_to_tree_or_blob_and_peels_nested_tags(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "treetag", git(repo, "rev-parse", "HEAD^{tree}"))
    git(repo, "tag", "blobtag", git(repo, "rev-parse", "HEAD:README.md"))
    git(repo, "tag", "-a", "-m", "to-tree", "annotated-tree", git(repo, "rev-parse", "HEAD^{tree}"))
    git(repo, "-c", "advice.nestedTag=false", "tag", "-a", "-m", "n1", "inner", "HEAD")
    git(repo, "-c", "advice.nestedTag=false", "tag", "-a", "-m", "n2", "nested", "inner")

    tags = list_refs(repo, branch_namespace=LOCAL_BRANCHES, timeout=30).tags

    assert {t.name: t.sha for t in tags} == {"inner": sha, "nested": sha}


def test_list_refs_skips_symbolic_refs_and_non_branch_namespaces(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    git(repo, "update-ref", "refs/stash", sha)
    git(repo, "update-ref", "refs/notes/commits", sha)
    git(repo, "update-ref", "refs/remotes/origin/main", sha)

    listing = list_refs(repo, branch_namespace=LOCAL_BRANCHES, timeout=30)

    assert [b.name for b in listing.branches] == ["main"]
    assert listing.tags == []


def test_list_refs_origin_namespace_excludes_origin_head_and_local_branches(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    git(repo, "update-ref", "refs/remotes/origin/release/1.0", sha)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(repo, "update-ref", "refs/remotes/upstream/other", sha)

    listing = list_refs(repo, branch_namespace=ORIGIN_BRANCHES, timeout=30)

    assert [(b.ref, b.name) for b in listing.branches] == [
        ("refs/remotes/origin/main", "main"),
        ("refs/remotes/origin/release/1.0", "release/1.0"),
    ]


def test_list_refs_in_empty_repository_is_empty(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "empty")

    listing = list_refs(repo, branch_namespace=LOCAL_BRANCHES, timeout=30)

    assert (listing.branches, listing.tags, listing.truncated) == ([], [], False)


def test_list_refs_caps_each_kind_at_500_and_reports_truncation(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    _bulk_refs(repo, "refs/heads/", 520, sha)

    listing = list_refs(repo, branch_namespace=LOCAL_BRANCHES, timeout=30)

    assert len(listing.branches) == 500
    assert listing.truncated is True
    # 依 ref 名稱排序：`b0000`～`b0498` 與 `main` 之中取前 500 個，`main` 排在 `b*` 之後被截掉
    assert [b.name for b in listing.branches] == sorted(b.name for b in listing.branches)


def test_list_refs_exactly_500_is_not_truncated(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    _bulk_refs(repo, "refs/heads/", 499, sha)  # 加上 main 剛好 500

    listing = list_refs(repo, branch_namespace=LOCAL_BRANCHES, timeout=30)

    assert len(listing.branches) == 500
    assert listing.truncated is False


SPECIAL_BRANCHES = [
    "feature/功能-中文",
    "release/v1.0",
    "a.b.c",
    "with'quote",
    "semi;colon",
    "dollar$x",
    "percent%x",
    "amp&x",
    "back`tick",
    "paren(x)",
    "bang!x",
    "hash#1",
    "plus+x",
    "eq=x",
    "comma,x",
    "at@x",
    "brace{x}",
    "emoji-😀",
]


@pytest.mark.parametrize("name", SPECIAL_BRANCHES)
def test_special_character_branch_names_round_trip(tmp_path: Path, name: str) -> None:
    repo = init_repo(tmp_path / "demo")
    git(repo, "branch", "--", name)
    sha = git(repo, "rev-parse", "HEAD")

    listing = list_refs(repo, branch_namespace=LOCAL_BRANCHES, timeout=30)
    entry = find_ref(repo, f"refs/heads/{name}", branch_namespace=LOCAL_BRANCHES, timeout=30)

    assert name in [b.name for b in listing.branches]
    assert (entry.name, entry.sha) == (name, sha)


def test_find_ref_returns_commit_for_branch_and_peeled_tag(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "-a", "v2", "-m", "annotated")

    branch = find_ref(repo, "refs/heads/main", branch_namespace=LOCAL_BRANCHES, timeout=30)
    tag = find_ref(repo, "refs/tags/v2", branch_namespace=LOCAL_BRANCHES, timeout=30)

    assert branch.sha == sha
    assert tag.sha == sha  # 不是 tag 物件的 SHA


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/missing",
        "refs/heads/feature",  # 只是目錄（feature/x 存在），本身不是 ref
        "refs/heads/fea",  # 前綴但不是完整名稱
        "refs/heads/Main",  # 大小寫不同
        "refs/tags/treetag",  # 指向 tree
        "refs/heads/alias",  # 符號參照
        "refs/tags/missing",
    ],
)
def test_find_ref_rejects_refs_that_are_not_selectable(tmp_path: Path, ref: str) -> None:
    repo = init_repo(tmp_path / "demo")
    git(repo, "branch", "feature/x")
    git(repo, "tag", "treetag", git(repo, "rev-parse", "HEAD^{tree}"))
    git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/main")

    with pytest.raises(InvalidRevisionError):
        find_ref(repo, ref, branch_namespace=LOCAL_BRANCHES, timeout=30)


def test_find_ref_never_matches_by_glob_or_other_repo_sha(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    other = init_repo(tmp_path / "other", files={"x.txt": "other\n"})
    other_sha = git(other, "rev-parse", "HEAD")

    for ref in ("refs/heads/*", "refs/heads/m*", other_sha):
        with pytest.raises(InvalidRevisionError):
            find_ref(repo, ref, branch_namespace=LOCAL_BRANCHES, timeout=30)


def test_find_ref_origin_namespace_rejects_origin_head_and_local_branches(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", sha)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    assert (
        find_ref(
            repo, "refs/remotes/origin/main", branch_namespace=ORIGIN_BRANCHES, timeout=30
        ).name
        == "main"
    )
    for ref in ("refs/remotes/origin/HEAD", "refs/heads/main"):
        with pytest.raises(InvalidRevisionError):
            find_ref(repo, ref, branch_namespace=ORIGIN_BRANCHES, timeout=30)


def test_head_commit_for_normal_detached_and_empty_repositories(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    sha = git(repo, "rev-parse", "HEAD")
    assert head_commit(repo, timeout=30) == sha

    second = _empty_commit(repo)
    git(repo, "checkout", "--quiet", "--detach", sha)
    assert head_commit(repo, timeout=30) == sha
    assert second != sha

    empty = _empty_repo(tmp_path / "empty")
    assert head_commit(empty, timeout=30) is None


# --- 真實 Git：read_commits ---


def test_read_commits_returns_parents_and_orders_children_before_parents(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--quiet", "-b", "topic")
    topic = _empty_commit(repo, "topic work")
    git(repo, "checkout", "--quiet", "main")
    main = _empty_commit(repo, "main work")
    git(repo, "-c", "commit.gpgsign=false", "merge", "--quiet", "--no-ff", "-m", "merge", "topic")
    merge = git(repo, "rev-parse", "HEAD")

    page = read_commits(
        repo,
        start_sha=None,
        branch_namespace=LOCAL_BRANCHES,
        limit=50,
        offset=0,
        timeout=30,
    )

    by_sha = {r.sha: r for r in page.records}
    assert set(by_sha) == {base, topic, main, merge}
    assert set(by_sha[merge].parents) == {main, topic}
    assert by_sha[base].parents == ()
    order = [r.sha for r in page.records]
    for record in page.records:
        for parent in record.parents:
            assert order.index(record.sha) < order.index(parent)  # topo-order：子在父之前
    assert page.has_more is False


def test_read_commits_pagination_and_has_more(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    for n in range(6):
        _empty_commit(repo, f"c{n}")  # 共 7 個 commit

    def page(limit: int, offset: int) -> history.CommitPage:
        return read_commits(
            repo,
            start_sha=None,
            branch_namespace=LOCAL_BRANCHES,
            limit=limit,
            offset=offset,
            timeout=30,
        )

    first, second, last, beyond = page(3, 0), page(3, 3), page(3, 6), page(3, 7)
    assert (len(first.records), first.has_more) == (3, True)
    assert (len(second.records), second.has_more) == (3, True)
    assert (len(last.records), last.has_more) == (1, False)
    assert (len(beyond.records), beyond.has_more) == (0, False)
    assert page(7, 0).has_more is False
    assert page(6, 0).has_more is True
    shas = [r.sha for p in (first, second, last) for r in p.records]
    assert len(set(shas)) == 7  # 分頁之間不重複、不遺漏


def test_read_commits_start_sha_limits_to_that_history(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--quiet", "-b", "topic")
    topic = _empty_commit(repo, "topic")
    git(repo, "checkout", "--quiet", "main")
    main = _empty_commit(repo, "main")

    only_main = read_commits(
        repo, start_sha=main, branch_namespace=LOCAL_BRANCHES, limit=10, offset=0, timeout=30
    )
    only_topic = read_commits(
        repo, start_sha=topic, branch_namespace=LOCAL_BRANCHES, limit=10, offset=0, timeout=30
    )

    assert [r.sha for r in only_main.records] == [main, base]
    assert [r.sha for r in only_topic.records] == [topic, base]


def test_read_commits_octopus_and_multiple_roots(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    root_a = git(repo, "rev-parse", "HEAD")
    for name in ("b1", "b2"):
        git(repo, "checkout", "--quiet", "-b", name, "main")
        _empty_commit(repo, name)
    git(repo, "checkout", "--quiet", "--orphan", "other-root")
    root_b = _empty_commit(repo, "second root")
    git(repo, "checkout", "--quiet", "main")
    _empty_commit(repo, "main moves on")  # 否則 main 是兩個分支的祖先，Git 會縮減親代
    git(repo, "-c", "commit.gpgsign=false", "merge", "--quiet", "-m", "octopus", "b1", "b2")
    octopus = git(repo, "rev-parse", "HEAD")

    records = read_commits(
        repo, start_sha=None, branch_namespace=LOCAL_BRANCHES, limit=50, offset=0, timeout=30
    ).records

    by_sha = {r.sha: r for r in records}
    assert len(by_sha[octopus].parents) == 3
    assert by_sha[root_a].parents == () and by_sha[root_b].parents == ()
    assert len(records) == 6  # 兩個 root、b1、b2、main 前進的一個、octopus


def test_read_commits_without_ref_ignores_stash_and_notes_but_includes_tags(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "demo")
    main = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "--quiet", "--detach")
    stash_like = _empty_commit(repo, "only reachable from stash")
    git(repo, "update-ref", "refs/stash", stash_like)
    git(repo, "checkout", "--quiet", "main")
    git(repo, "checkout", "--quiet", "--detach")
    tagged = _empty_commit(repo, "only reachable from a tag")
    git(repo, "tag", "release", tagged)
    git(repo, "checkout", "--quiet", "main")

    shas = {
        r.sha
        for r in read_commits(
            repo, start_sha=None, branch_namespace=LOCAL_BRANCHES, limit=50, offset=0, timeout=30
        ).records
    }

    assert shas == {main, tagged}
    assert stash_like not in shas


def test_read_commits_tag_pointing_to_tree_does_not_break_the_log(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    git(repo, "tag", "treetag", git(repo, "rev-parse", "HEAD^{tree}"))
    git(repo, "tag", "blobtag", git(repo, "rev-parse", "HEAD:README.md"))

    page = read_commits(
        repo, start_sha=None, branch_namespace=LOCAL_BRANCHES, limit=10, offset=0, timeout=30
    )

    assert len(page.records) == 1


def test_read_commits_origin_namespace_uses_remote_branches_only(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    local_only = _empty_commit(repo, "local only")
    git(repo, "checkout", "--quiet", "--detach", "HEAD~1")
    remote_tip = _empty_commit(repo, "remote tip")
    git(repo, "update-ref", "refs/remotes/origin/main", remote_tip)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    shas = [
        r.sha
        for r in read_commits(
            repo, start_sha=None, branch_namespace=ORIGIN_BRANCHES, limit=10, offset=0, timeout=30
        ).records
    ]

    assert remote_tip in shas
    assert local_only not in shas
    assert len(shas) == len(set(shas))  # origin/HEAD 與 origin/main 指向同處，不重複


def test_read_commits_in_empty_repository_returns_nothing(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path / "empty")

    page = read_commits(
        repo, start_sha=None, branch_namespace=LOCAL_BRANCHES, limit=10, offset=0, timeout=30
    )

    assert (page.records, page.has_more) == ([], False)


def test_read_commits_special_author_subject_and_dates(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo")
    git(
        repo,
        "-c",
        'user.name=Ünï "quoted" 名字; $(whoami) `x` %s',
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--allow-empty",
        "--date=2020-02-03T04:05:06+08:00",
        "-m",
        'first line\nsecond line\n\nbody with "quotes" and 100% $HOME',
    )

    record = read_commits(
        repo, start_sha=None, branch_namespace=LOCAL_BRANCHES, limit=1, offset=0, timeout=30
    ).records[0]

    assert record.author_name == 'Ünï "quoted" 名字; $(whoami) `x` %s'
    assert record.subject == "first line second line"  # 只取第一段，換行以空白連接
    assert record.authored_at == datetime(2020, 2, 2, 20, 5, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_sha": "--output=x"},
        {"start_sha": "HEAD"},
        {"start_sha": "a" * 39},
        {"limit": 0},
        {"offset": -1},
    ],
)
def test_read_commits_rejects_unsafe_arguments(tmp_path: Path, kwargs: dict[str, object]) -> None:
    repo = init_repo(tmp_path / "demo")
    arguments: dict[str, object] = {"start_sha": None, "limit": 10, "offset": 0}
    arguments.update(kwargs)

    with pytest.raises(ValueError):
        read_commits(
            repo,
            branch_namespace=LOCAL_BRANCHES,
            timeout=30,
            **arguments,  # type: ignore[arg-type]
        )
    assert not (repo / "x").exists()
