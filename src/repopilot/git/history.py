"""唯讀的 refs 與 commit 歷史查詢（切片 5e-1，D34）。

只使用讀取類指令（`for-each-ref`、`log`、`rev-parse`），不 fetch、不 checkout、不寫入。

輸出解析的安全約定：
- refs：欄位以 NUL（`%00`）分隔、記錄以換行分隔。Git 的 ref 名稱不可能含控制字元（含 NUL、
  換行），所以兩個分隔符都不會被 ref 名稱破壞；其他欄位是 hex 或固定字樣。
- commits：`git log -z` 讓記錄以 NUL 結尾，欄位也以 NUL 分隔，因此輸出是「每筆固定 5 個欄位」
  的扁平 NUL 序列，依欄位數量切塊，任何欄位內即使含換行也不會錯位；每筆都驗證 SHA 格式。
- 使用者提供的 `ref` 不直接交給 `git log`：先通過語法檢查，再以 `for-each-ref` 查出「確實存在」
  的完整 ref，之後 `git log` 只收到查出來的完整 SHA。因此不可能被解讀為選項或 revision 表達式。
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from repopilot.errors import GitCommandError, InvalidRevisionError
from repopilot.git.objects import resolve_commit
from repopilot.git.runner import run_git

LOCAL_BRANCHES = "refs/heads/"
ORIGIN_BRANCHES = "refs/remotes/origin/"
TAGS = "refs/tags/"
# GitHub 來源 clone 時由 Git 建立的符號參照（指向預設分支），不是真正的分支
ORIGIN_HEAD = "refs/remotes/origin/HEAD"

MAX_REFS_PER_KIND = 500
MAX_REF_LENGTH = 1024
SUBJECT_MAX_CHARS = 200
AUTHOR_MAX_CHARS = 200

_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EPOCH_SECONDS = re.compile(r"^-?[0-9]{1,20}$")
# Git 本身不允許這些字元出現在 ref 名稱中；任何控制字元與空白也不允許
_FORBIDDEN_REF_CHARS = frozenset("~^:?*[\\")

_REF_FIELDS = 7
_REF_FORMAT = "%00".join(
    [
        "%(refname)",
        "%(objectname)",
        "%(objecttype)",
        "%(*objectname)",  # 附註標籤：完整還原（含巢狀標籤）後的物件
        "%(*objecttype)",
        "%(symref)",  # 符號參照（例如 origin/HEAD）才有值
        "%(HEAD)",  # 目前簽出的分支為 `*`
    ]
)
_COMMIT_FIELDS = 5
_COMMIT_FORMAT = "%x00".join(["%H", "%P", "%an", "%at", "%s"])

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class RefEntry:
    """可選取的 ref：`ref` 是完整名稱，`name` 是去掉命名空間前綴的顯示名稱，`sha` 是 commit。"""

    ref: str
    name: str
    sha: str
    is_head: bool


@dataclass(frozen=True)
class RefListing:
    branches: list[RefEntry]
    tags: list[RefEntry]
    # 分支或標籤任一種超過 MAX_REFS_PER_KIND（只回傳依名稱排序的前 500 個）
    truncated: bool


@dataclass(frozen=True)
class RawRef:
    refname: str
    object_id: str
    object_type: str
    peeled_id: str
    peeled_type: str
    symref: str
    is_head: bool


@dataclass(frozen=True)
class CommitRecord:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    authored_at: datetime
    subject: str


@dataclass(frozen=True)
class CommitPage:
    records: list[CommitRecord]
    has_more: bool


# --- refs ---


def list_refs(repo: Path, *, branch_namespace: str, timeout: float) -> RefListing:
    """列出分支（`branch_namespace` 下）與標籤；各自最多 500 個，依 ref 名稱排序。

    `branch_namespace` 是 `LOCAL_BRANCHES`（本機來源）或 `ORIGIN_BRANCHES`（GitHub 來源）。
    標籤以還原後的 commit SHA 表示；指向 tree／blob 的標籤略過。
    """
    _check_namespace(branch_namespace)
    branches, branches_truncated = _list_namespace(repo, branch_namespace, timeout)
    tags, tags_truncated = _list_namespace(repo, TAGS, timeout)
    return RefListing(branches, tags, branches_truncated or tags_truncated)


def find_ref(repo: Path, ref: str, *, branch_namespace: str, timeout: float) -> RefEntry:
    """確認 `ref` 是此 repo 目前存在、且可選取的完整 ref；否則拋出 InvalidRevisionError。

    可選取＝位於 `branch_namespace` 或 `refs/tags/` 之下、不是符號參照、且（還原後）指向 commit。
    不接受 `HEAD~3`、`a..b`、SHA、短名稱等 revision 表達式。查詢範圍是整個 repo（不受
    列表 500 個上限限制）。
    """
    _check_namespace(branch_namespace)
    if not (ref.startswith(branch_namespace) or ref.startswith(TAGS)):
        raise InvalidRevisionError("The ref must be a full ref name from the refs list.")
    _check_ref_syntax(ref)

    # ref 以 `refs/` 開頭，不會被當成選項；為 fnmatch 樣式，但萬用字元已在語法檢查被拒絕。
    # 檔案／目錄衝突使 `refs/heads/a` 與 `refs/heads/a/b` 無法並存，所以 `--count=1` 在 ref 存在
    # 時一定是它本身；不存在時可能回傳其子孫，由下方的完全相等比對排除
    result = run_git(
        repo,
        ["for-each-ref", f"--format={_REF_FORMAT}", "--count=1", ref],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise GitCommandError(f"git for-each-ref failed: {result.stderr.strip()}")
    for raw in parse_ref_records(result.stdout):
        entry = _to_entry(raw, branch_namespace)
        if entry is not None and entry.ref == ref:
            return entry
    raise InvalidRevisionError("The ref does not exist in this repository.")


def head_commit(repo: Path, *, timeout: float) -> str | None:
    """HEAD 指向的 commit SHA；沒有任何 commit 的空 repo 回傳 None。"""
    try:
        return resolve_commit(repo, "HEAD", timeout=timeout)
    except InvalidRevisionError:
        return None


def _check_namespace(branch_namespace: str) -> None:
    if branch_namespace not in (LOCAL_BRANCHES, ORIGIN_BRANCHES):
        raise ValueError("branch_namespace must be LOCAL_BRANCHES or ORIGIN_BRANCHES.")


def _check_ref_syntax(ref: str) -> None:
    """拒絕不可能是合法 ref 名稱的字串（check-ref-format 規則的子集；最終由存在性查詢把關）。"""
    if not ref or len(ref) > MAX_REF_LENGTH:
        raise InvalidRevisionError("The ref is empty or too long.")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F or ch in _FORBIDDEN_REF_CHARS for ch in ref):
        raise InvalidRevisionError("The ref contains characters that are not allowed.")
    if ".." in ref or "@{" in ref or "//" in ref or ref.endswith(("/", ".")):
        raise InvalidRevisionError("The ref is not a valid ref name.")
    try:
        ref.encode("utf-8")  # 孤立的 surrogate 無法放進 argv
    except UnicodeEncodeError:
        raise InvalidRevisionError("The ref is not valid text.") from None


def _list_namespace(repo: Path, namespace: str, timeout: float) -> tuple[list[RefEntry], bool]:
    # `--count` 比上限多 1：只用來判斷是否被截斷
    result = run_git(
        repo,
        [
            "for-each-ref",
            f"--format={_REF_FORMAT}",
            "--sort=refname",
            f"--count={MAX_REFS_PER_KIND + 1}",
            namespace.rstrip("/"),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise GitCommandError(f"git for-each-ref failed: {result.stderr.strip()}")
    raw_refs = parse_ref_records(result.stdout)
    truncated = len(raw_refs) > MAX_REFS_PER_KIND
    entries = [_to_entry(raw, namespace) for raw in raw_refs[:MAX_REFS_PER_KIND]]
    return [entry for entry in entries if entry is not None], truncated


def parse_ref_records(data: bytes) -> list[RawRef]:
    """解析 `for-each-ref` 輸出；格式不符時拋出 GitCommandError。

    名稱不是有效 UTF-8 的 ref 無法安全放進 JSON 回應，直接略過。
    """
    refs: list[RawRef] = []
    for record in data.split(b"\n"):
        if not record:
            continue
        fields = record.split(b"\x00")
        if len(fields) != _REF_FIELDS:
            raise GitCommandError("Git returned an unexpected ref record.")
        object_id, object_type, peeled_id, peeled_type = (_ascii(fields[i]) for i in (1, 2, 3, 4))
        if not _FULL_SHA.match(object_id) or (peeled_id and not _FULL_SHA.match(peeled_id)):
            raise GitCommandError("Git returned an unexpected object identifier.")
        try:
            refname = fields[0].decode("utf-8")
            symref = fields[5].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not refname.startswith("refs/"):
            raise GitCommandError("Git returned an unexpected ref name.")
        refs.append(
            RawRef(
                refname=refname,
                object_id=object_id,
                object_type=object_type,
                peeled_id=peeled_id,
                peeled_type=peeled_type,
                symref=symref,
                is_head=fields[6] == b"*",
            )
        )
    return refs


def _ascii(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace")


def _to_entry(raw: RawRef, branch_namespace: str) -> RefEntry | None:
    """轉成可選取的 ref；符號參照、不在可選命名空間、或不指向 commit 者回傳 None。"""
    if raw.symref or raw.refname == ORIGIN_HEAD:
        return None
    if raw.refname.startswith(TAGS):
        prefix = TAGS
    elif raw.refname.startswith(branch_namespace):
        prefix = branch_namespace
    else:
        return None
    name = raw.refname.removeprefix(prefix)
    if not name:
        return None
    # 附註標籤：`*objectname` 是還原後的物件，只接受 commit；其餘（指向 tree／blob）略過
    if raw.object_type == "commit":
        sha = raw.object_id
    elif raw.object_type == "tag" and raw.peeled_type == "commit":
        sha = raw.peeled_id
    else:
        return None
    return RefEntry(ref=raw.refname, name=name, sha=sha, is_head=raw.is_head)


# --- commits ---


def read_commits(
    repo: Path,
    *,
    start_sha: str | None,
    branch_namespace: str,
    limit: int,
    offset: int,
    timeout: float,
) -> CommitPage:
    """以單一 `git log --topo-order` 讀取一頁 commit；多取一筆判斷 `has_more`。

    `start_sha` 是 `find_ref` 查出的完整 SHA；None 表示「所有分支與標籤」：本機來源
    `--branches --tags`，GitHub 來源 `--remotes=origin --tags`（排除 `origin/HEAD`）。
    刻意不使用 `--all`，避免納入 stash、notes 等非分支 refs。
    """
    _check_namespace(branch_namespace)
    if limit < 1 or offset < 0:
        raise ValueError("limit must be positive and offset must not be negative.")
    if start_sha is not None:
        if not _FULL_SHA.match(start_sha):
            raise ValueError("start_sha must be a full hexadecimal object ID.")
        revisions = [start_sha]
    elif branch_namespace == LOCAL_BRANCHES:
        revisions = ["--branches", "--tags"]
    else:
        revisions = ["--exclude=origin/HEAD", "--remotes=origin", "--tags"]

    result = run_git(
        repo,
        [
            "-c",
            "i18n.logOutputEncoding=UTF-8",
            "log",
            "--topo-order",
            "-z",
            "--no-show-signature",  # 使用者設定 log.showSignature 時，GPG 輸出會混進記錄
            f"--format={_COMMIT_FORMAT}",
            f"--skip={offset}",
            f"--max-count={limit + 1}",
            *revisions,
            "--",
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise GitCommandError(f"git log failed: {result.stderr.strip()}")
    records = parse_commit_records(result.stdout)
    return CommitPage(records[:limit], has_more=len(records) > limit)


def parse_commit_records(data: bytes) -> list[CommitRecord]:
    """解析 `git log -z` 的輸出；欄位數不是 5 的倍數或 SHA 格式不符時拋出 GitCommandError。"""
    if not data:
        return []
    fields = data.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()  # -z 以 NUL 結尾，最後一個 NUL 之後是空字串
    if len(fields) % _COMMIT_FIELDS != 0:
        raise GitCommandError("Git returned an unexpected commit record.")
    records: list[CommitRecord] = []
    for index in range(0, len(fields), _COMMIT_FIELDS):
        raw_sha, raw_parents, raw_author, raw_time, raw_subject = fields[
            index : index + _COMMIT_FIELDS
        ]
        sha = _ascii(raw_sha)
        parents = tuple(_ascii(raw_parents).split()) if raw_parents else ()
        if not _FULL_SHA.match(sha) or not all(_FULL_SHA.match(p) for p in parents):
            raise GitCommandError("Git returned an unexpected commit identifier.")
        records.append(
            CommitRecord(
                sha=sha,
                parents=parents,
                author_name=_text(raw_author, AUTHOR_MAX_CHARS),
                authored_at=epoch_to_utc(_ascii(raw_time)),
                subject=_text(raw_subject, SUBJECT_MAX_CHARS),
            )
        )
    return records


def _text(raw: bytes, max_chars: int) -> str:
    return raw.decode("utf-8", errors="replace")[:max_chars]


def epoch_to_utc(raw: str) -> datetime:
    """Unix 時間（秒）轉為 UTC datetime；格式不符視為 0，超出 datetime 範圍者夾到最小／最大值。

    commit 的作者日期可以是任意值（包含惡意的超大數字），不能讓單一 commit 使整頁回應失敗。
    """
    seconds = int(raw) if _EPOCH_SECONDS.match(raw) else 0
    try:
        return _UNIX_EPOCH + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.min.replace(tzinfo=UTC) if seconds < 0 else datetime.max.replace(tzinfo=UTC)
