import re
from dataclasses import dataclass
from pathlib import Path

from repopilot.errors import GitCommandError, InvalidRevisionError
from repopilot.git.runner import run_git

_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
MAX_REVISION_LENGTH = 256


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    object_id: str
    # gitlink（submodule）沒有大小
    size: int | None
    path: str


def resolve_commit(repo: Path, revision: str, *, timeout: float) -> str:
    """將 revision 解析為完整 commit SHA；格式不合法或不是 commit 時拋出 InvalidRevisionError。"""
    if not revision or len(revision) > MAX_REVISION_LENGTH:
        raise InvalidRevisionError("The revision is empty or too long.")
    if revision.startswith("-"):
        raise InvalidRevisionError("The revision must not start with '-'.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in revision):
        raise InvalidRevisionError("The revision contains control characters.")

    result = run_git(
        repo,
        ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
        timeout=timeout,
    )
    # --quiet：無法解析時 exit 1 且無輸出；其他非 0（例如 128）是 Git 本身的錯誤
    if result.returncode == 1:
        raise InvalidRevisionError("The revision does not resolve to a commit.")
    if result.returncode != 0:
        raise GitCommandError(f"git rev-parse failed: {result.stderr.strip()}")
    sha = result.stdout.decode("ascii", errors="replace").strip()
    if not _FULL_SHA.match(sha):
        raise GitCommandError("Git returned an unexpected commit identifier.")
    return sha


def list_tree(repo: Path, commit_sha: str, *, timeout: float) -> list[TreeEntry]:
    """以 `ls-tree -r -l -z` 列出 commit 內所有檔案（不讀工作目錄）。"""
    if not _FULL_SHA.match(commit_sha):
        raise ValueError("commit_sha must be a full hexadecimal object ID.")
    result = run_git(
        repo, ["ls-tree", "-r", "-l", "-z", "--full-tree", commit_sha], timeout=timeout
    )
    if result.returncode != 0:
        raise GitCommandError(f"git ls-tree failed: {result.stderr.strip()}")
    return [_parse_tree_entry(raw) for raw in result.stdout.split(b"\x00") if raw]


def _parse_tree_entry(raw: bytes) -> TreeEntry:
    # 格式：<mode> SP <type> SP <object> SP+ <size> TAB <path>；size 前可能有對齊用的空白
    meta, separator, raw_path = raw.partition(b"\t")
    fields = meta.decode("ascii", errors="replace").split()
    if not separator or len(fields) != 4:
        raise GitCommandError("Git returned an unexpected tree entry.")
    mode, object_type, object_id, raw_size = fields
    size = None if raw_size == "-" else _parse_size(raw_size)
    # 路徑無法以 UTF-8 解碼時保留為 \xNN 字面文字，避免資料庫拒收；詳見 docs/architecture.md
    path = raw_path.decode("utf-8", errors="backslashreplace")
    return TreeEntry(mode, object_type, object_id, size, path)


def _parse_size(raw_size: str) -> int:
    if not raw_size.isdecimal():
        raise GitCommandError("Git returned an unexpected tree entry.")
    return int(raw_size)


def plan_blob_batches(
    expected_sizes: dict[str, int], *, max_files: int, max_bytes: int
) -> list[dict[str, int]]:
    """把「object ID → ls-tree 大小」依原順序切成多批，每批交給一次 `read_blobs`（D43）。

    純函式、不碰 Git：一批最多 `max_files` 個 blob，且合計大小不超過 `max_bytes`；單一 blob 本身
    就超過 `max_bytes` 時獨占一批（不拆、不丟棄）。批次保持輸入的順序，並且**每個 object ID
    恰好出現在一批**（切分不會遺漏、重複或改變順序，這是「結果與一次讀完逐位元相同」的前提）。
    空輸入回傳空串列。大小用的是 ls-tree 的值（讀取前就已知），所以規劃本身不需要讀任何內容。
    """
    if max_files < 1 or max_bytes < 1:
        raise ValueError("max_files and max_bytes must be at least 1.")
    batches: list[dict[str, int]] = []
    current: dict[str, int] = {}
    current_bytes = 0
    for object_id, size in expected_sizes.items():
        if current and (len(current) >= max_files or current_bytes + size > max_bytes):
            batches.append(current)
            current, current_bytes = {}, 0
        current[object_id] = size
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def read_blobs(repo: Path, expected_sizes: dict[str, int], *, timeout: float) -> dict[str, bytes]:
    """以單一 `cat-file --batch` 讀取多個 blob；呼叫端須已檢查各 blob 大小。

    回傳 object ID → 原始 bytes。header 的大小必須與 `ls-tree` 一致，否則視為 Git 錯誤。
    """
    if not expected_sizes:
        return {}
    for object_id in expected_sizes:
        if not _FULL_SHA.match(object_id):
            raise ValueError("object IDs must be full hexadecimal object IDs.")
    stdin = "".join(f"{object_id}\n" for object_id in expected_sizes).encode("ascii")
    result = run_git(repo, ["cat-file", "--batch"], timeout=timeout, stdin=stdin)
    if result.returncode != 0:
        raise GitCommandError(f"git cat-file failed: {result.stderr.strip()}")

    output = result.stdout
    blobs: dict[str, bytes] = {}
    position = 0
    for object_id, expected_size in expected_sizes.items():
        newline = output.find(b"\n", position)
        if newline == -1:
            raise GitCommandError("Git returned truncated blob data.")
        header = output[position:newline].decode("ascii", errors="replace").split()
        if len(header) != 3 or header[0] != object_id or header[1] != "blob":
            raise GitCommandError("Git returned an unexpected blob header.")
        size = int(header[2]) if header[2].isdecimal() else -1
        if size != expected_size:
            raise GitCommandError("Git returned a blob with an unexpected size.")
        start = newline + 1
        end = start + size
        if end >= len(output) or output[end : end + 1] != b"\n":
            raise GitCommandError("Git returned truncated blob data.")
        blobs[object_id] = output[start:end]
        position = end + 1
    return blobs
