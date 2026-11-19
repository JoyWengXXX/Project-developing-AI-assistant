"""GitHub 註冊與 fetch 的真實堆疊測試（切片 5，D44）：PostgreSQL＋Redis＋dispatcher＋worker 容器。

「GitHub」是 worker-test 容器內的假伺服器（`tests/fake_remote/server.py`：GitHub API＋
`git http-backend`，資料放在 `.test-remote` bind mount，由這裡的 `FakeRemote` 在主機端操作）；
clone 與 fetch 都由容器內的 Linux Git 對 Windows 掛載的受管目錄（`.test-managed` →
`/mnt/managed`，讀寫）執行，所以也一併驗證：受管目錄 rename 與讀寫、容器寫入的檔案主機端可讀、
取消與終止 git 程序樹、SIGKILL 後的暫存清理。不連真實 GitHub；token 一律是假值。
"""

import base64
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from repopilot.services import github_credentials
from tests.git_helpers import git
from tests.github_helpers import FAKE_TOKEN, FakeGitHub
from tests.integration.stack.conftest import Stack, wait_for_status, wait_until
from tests.stack_control import compose

B64_TOKEN = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
REPO_FILES = {"README.md": "hello\n", "src/app.py": "x = 1\n"}
DONE = {"succeeded", "failed", "cancelled"}


# --- helpers ---


def _register_job(client: TestClient, repo: str = "octocat/Hello-World") -> int:
    response = client.post("/api/v1/github/registration-jobs", json={"repo": repo})
    assert response.status_code == 202, response.text
    return int(response.json()["job"]["id"])


def _fetch_job(client: TestClient, repository_id: int) -> int:
    response = client.post(f"/api/v1/repositories/{repository_id}/fetch-jobs")
    assert response.status_code == 202, response.text
    return int(response.json()["job"]["id"])


def _register(client: TestClient, stack: Stack, repo: str = "octocat/Hello-World") -> int:
    """（在容器內）註冊並回傳 repository_id。"""
    job = wait_for_status(client, _register_job(client, repo), DONE, timeout=120)
    assert job["status"] == "succeeded", job
    return int(job["result"]["repository_id"])


def _clone_path(stack: Stack, owner: str = "octocat", name: str = "Hello-World") -> Path:
    return stack.managed_dir / "github" / owner / name


def _temp_children(stack: Stack) -> list[str]:
    root = stack.managed_dir / ".tmp"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _job_work_state(stack: Stack, job_id: int) -> dict[str, Any]:
    value = stack.scalar("SELECT work_state FROM jobs WHERE id = :i", i=job_id)
    return dict(value or {})


def _repository_count(stack: Stack) -> int:
    return int(stack.scalar("SELECT count(*) FROM repositories"))


def _wait_cloning(stack: Stack, job_id: int) -> str:
    """等到容器內的 clone 真的在跑：階段 cloning、暫存目錄已記錄且在主機端看得到。"""

    def ready() -> str | None:
        temp = _job_work_state(stack, job_id).get("temp_dir")
        phase = stack.scalar("SELECT phase FROM jobs WHERE id = :i", i=job_id)
        if phase == "cloning" and temp and (stack.managed_dir / ".tmp" / temp).exists():
            return str(temp)
        return None

    return str(wait_until(ready, f"job {job_id} to be cloning", timeout=60))


def _save_token(stack: Stack) -> None:
    with Session(stack.engine) as session:
        github_credentials.connect_token(
            session, token=FAKE_TOKEN, settings=stack.api_settings, client=FakeGitHub().client()
        )


def _container_commands(stack: Stack) -> str:
    """worker-test 容器內所有程序的命令列（讀 /proc）。"""
    script = (
        "import os\n"
        "for p in os.listdir('/proc'):\n"
        "    if p.isdigit():\n"
        "        try:\n"
        "            print(open(f'/proc/{p}/cmdline','rb').read().replace(b'\\0', b' ').decode())\n"
        "        except OSError:\n"
        "            pass\n"
    )
    return compose("exec", "-T", "worker-test", "python", "-c", script, check=False).stdout


def _redis_dump(stack: Stack) -> str:
    """Redis 內所有鍵與值的文字（用來證明訊息裡沒有 token）。"""

    def redis(*args: str) -> str:
        return compose("exec", "-T", "redis-test", "redis-cli", "--raw", *args, check=False).stdout

    chunks: list[str] = []
    for key in redis("keys", "*").split():
        chunks.append(key)
        kind = redis("type", key).strip()
        if kind == "list":
            chunks.append(redis("lrange", key, "0", "-1"))
        elif kind == "string":
            chunks.append(redis("get", key))
        elif kind == "hash":
            chunks.append(redis("hgetall", key))
        elif kind == "set":
            chunks.append(redis("smembers", key))
        elif kind == "zset":
            chunks.append(redis("zrange", key, "0", "-1"))
    return "\n".join(chunks)


def _files_containing(root: Path, needles: list[str]) -> list[str]:
    hits: list[str] = []
    for current, _dirs, files in os.walk(root):
        for name in files:
            data = (Path(current) / name).read_bytes()
            if any(n.encode() in data for n in needles):
                hits.append(str(Path(current) / name))
    return hits


# --- 註冊：容器內 clone → rename → 資料列 ---


def test_a_registration_job_clones_in_the_container_and_the_host_can_read_it(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    assert remote is not None

    job_id = _register_job(client, "octocat/hello-world")
    job = wait_for_status(client, job_id, DONE, timeout=120)

    assert job["status"] == "succeeded", job
    assert job["phase"] == "completed"
    repository_id = job["result"]["repository_id"]
    row = client.get(f"/api/v1/repositories/{repository_id}").json()
    assert (row["source_type"], row["github_owner"], row["github_name"]) == (
        "github",
        "octocat",
        "Hello-World",
    )
    clone = _clone_path(stack)
    assert Path(row["canonical_local_path"]) == clone.resolve()
    # 容器內 rename（.tmp → github/owner/name）後，主機端可以讀：Windows Git 讀 Linux Git 寫的 repo
    assert git(clone, "rev-parse", "HEAD") == remote.head()
    assert "README.md" in git(clone, "ls-tree", "-r", "--name-only", "HEAD")
    assert _temp_children(stack) == []
    assert stack.scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0
    # 同一個 repo 在容器內也能匯入（GitHub 來源的 worker 匯入：容器端重驗與 safe.directory）
    imported = client.post(f"/api/v1/repositories/{repository_id}/import-jobs", json={})
    assert imported.status_code == 202, imported.text
    finished = wait_for_status(client, imported.json()["job"]["id"], DONE, timeout=120)
    assert finished["status"] == "succeeded", finished
    assert finished["snapshot_id"] is not None


def test_a_fetch_job_in_the_container_brings_new_commits_that_can_then_be_imported(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    assert remote is not None
    repository_id = _register(client, stack)
    new_sha = remote.commit({"src/new.py": "y = 2\n"}, message="new work")

    job = wait_for_status(client, _fetch_job(client, repository_id), DONE, timeout=120)

    assert job["status"] == "succeeded", job
    assert job["phase"] == "completed"
    assert [(c["name"], c["change"]) for c in job["result"]["updated_refs"]] == [
        ("origin/main", "updated")
    ]
    assert git(_clone_path(stack), "rev-parse", "origin/main") == new_sha  # 主機端讀到容器寫的 ref
    imported = client.post(
        f"/api/v1/repositories/{repository_id}/import-jobs", json={"revision": "origin/main"}
    )
    finished = wait_for_status(client, imported.json()["job"]["id"], DONE, timeout=120)
    assert finished["status"] == "succeeded", finished
    files = client.get(f"/api/v1/snapshots/{finished['snapshot_id']}/files").json()["items"]
    assert "src/new.py" in {f["path"] for f in files}


# --- token：送得到 Git 伺服器，其他地方都沒有 ---


def test_the_token_reaches_git_in_the_container_and_appears_nowhere_else(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Secret", {"s.txt": "s\n"}, private=True)
    assert remote is not None
    stack.remote.control(required_authorization=f"basic {B64_TOKEN}", accepted_token=FAKE_TOKEN)
    _save_token(stack)
    stack.stop("worker-test")  # 讓訊息停在 Redis：確定性地掃描「訊息裡有沒有 token」
    job_id = _register_job(client, "octocat/Secret")
    wait_until(
        lambda: (
            stack.scalar("SELECT status FROM job_dispatches WHERE job_id = :i", i=job_id) == "sent"
        ),
        "the dispatcher to publish the message",
    )
    in_redis = _redis_dump(stack)

    stack.start("worker-test")
    stack.wait_online()
    job = wait_for_status(client, job_id, DONE, timeout=120)

    assert job["status"] == "succeeded", job
    assert "repopilot.run_job" in in_redis  # 掃描到的確實是任務訊息
    after_redis = _redis_dump(stack)
    everything = "\n".join(
        [
            in_redis,
            after_redis,
            str(stack.scalar("SELECT row_to_json(j)::text FROM jobs j WHERE id = :i", i=job_id)),
            str(stack.scalar("SELECT coalesce(string_agg(d::text, ''), '') FROM job_dispatches d")),
            client.get(f"/api/v1/jobs/{job_id}").text,
            stack.logs("worker-test"),
            stack.logs("dispatcher-test"),
        ]
    )
    for needle in (FAKE_TOKEN, B64_TOKEN, "x-access-token", "extraheader"):
        assert needle not in everything
    # Git（容器內）真的送出了 token；API 呼叫帶 Bearer
    sent = stack.remote.requests()
    git_auth = {r["authorization"] for r in sent if not r["path"].startswith("/repos/")}
    api_auth = {r["authorization"] for r in sent if r["path"].startswith("/repos/")}
    assert git_auth == {f"basic {B64_TOKEN}"}
    assert api_auth == {f"Bearer {FAKE_TOKEN}"}
    # 容器寫入的 clone 沒有 token（掃描整個目錄，含 .git/config）
    clone = _clone_path(stack, name="Secret")
    assert _files_containing(clone, [FAKE_TOKEN, B64_TOKEN, "x-access-token", "extraheader"]) == []


# --- 取消 ---


def test_cancelling_a_clone_in_the_container_kills_git_and_cleans_the_mount(
    stack: Stack, client: TestClient
) -> None:
    stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    stack.remote.control(delay_seconds=300)  # 很慢的遠端
    job_id = _register_job(client)
    temp = _wait_cloning(stack, job_id)
    assert "clone" in _container_commands(stack)  # git clone 確實在容器內執行

    started = time.monotonic()
    cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
    job = wait_for_status(client, job_id, DONE, timeout=60)
    elapsed = time.monotonic() - started

    assert cancelled.status_code == 202
    assert job["status"] == "cancelled", job
    assert elapsed < 30  # 輪詢 0.5 秒＋SIGTERM 寬限；不是等遠端的 300 秒
    assert not (stack.managed_dir / ".tmp" / temp).exists()
    assert _temp_children(stack) == []
    assert not (stack.managed_dir / "github").exists()
    assert _repository_count(stack) == 0
    assert " clone " not in _container_commands(stack)  # git 程序樹已被終止
    assert stack.scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0


def test_cancelling_a_fetch_in_the_container_leaves_the_clone_usable(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    assert remote is not None
    repository_id = _register(client, stack)
    head = git(_clone_path(stack), "rev-parse", "HEAD")
    remote.commit({"later.txt": "later\n"})
    stack.remote.control(delay_seconds=300)
    job_id = _fetch_job(client, repository_id)
    wait_until(
        lambda: stack.scalar("SELECT phase FROM jobs WHERE id = :i", i=job_id) == "fetching",
        "the fetch to start",
    )
    wait_until(lambda: " fetch " in _container_commands(stack), "git fetch to run")

    client.post(f"/api/v1/jobs/{job_id}/cancel")
    job = wait_for_status(client, job_id, DONE, timeout=60)

    assert job["status"] == "cancelled", job
    assert " fetch " not in _container_commands(stack)
    # 沒有殘留的鎖檔：取消後的 fetch 可以成功
    assert git(_clone_path(stack), "rev-parse", "HEAD") == head
    stack.remote.control()
    again = wait_for_status(client, _fetch_job(client, repository_id), DONE, timeout=120)
    assert again["status"] == "succeeded", again


# --- worker 被 SIGKILL ---


def test_killing_the_worker_mid_clone_leaves_a_temp_dir_that_the_retry_cleans(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    assert remote is not None
    stack.remote.control(delay_seconds=300)
    job_id = _register_job(client)
    temp = _wait_cloning(stack, job_id)

    stack.kill("worker-test")

    leftover = stack.managed_dir / ".tmp" / temp
    assert leftover.exists()  # SIGKILL 沒有任何清理機會：暫存目錄留在掛載上
    stack.remote.control()  # 恢復正常的遠端
    stack.start("worker-test")
    stack.wait_online()
    job = wait_for_status(client, job_id, DONE, timeout=150)

    assert job["status"] == "succeeded", job
    assert job["attempt"] == 2  # 第一次因 worker 被殺而中斷（租約過期），第二次完成
    assert not leftover.exists()  # 新的 attempt 清掉了上一個 attempt 的暫存目錄
    assert _temp_children(stack) == []
    assert _repository_count(stack) == 1
    assert git(_clone_path(stack), "rev-parse", "HEAD") == remote.head()


def test_killing_the_worker_mid_fetch_leaves_a_clone_that_the_next_fetch_can_update(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    assert remote is not None
    repository_id = _register(client, stack)
    head = git(_clone_path(stack), "rev-parse", "HEAD")
    # 約 3 MB 的新 pack、限速 400 KB/s：fetch 在「下載中」被殺（SIGKILL 沒有任何清理機會）
    remote.commit({"new.bin": os.urandom(3_000_000)}, message="big")
    stack.remote.control(bytes_per_second=400_000)
    job_id = _fetch_job(client, repository_id)
    wait_until(lambda: " fetch " in _container_commands(stack), "git fetch to run")
    time.sleep(2)  # 讓下載確實開始（物件已開始寫入 objects/pack）

    stack.kill("worker-test")

    clone = _clone_path(stack)
    assert git(clone, "rev-parse", "HEAD") == head  # 被殺的 fetch 沒有改動 clone 的既有狀態
    stack.remote.control()
    stack.start("worker-test")
    stack.wait_online()
    job = wait_for_status(client, job_id, DONE, timeout=150)

    assert job["status"] == "succeeded", job  # 殘留的 tmp_pack 等不影響下一次 fetch
    assert job["attempt"] == 2
    assert git(clone, "rev-parse", "origin/main") == remote.head()
    assert not list(clone.rglob("*.lock"))
    assert git(clone, "fsck", "--full", "--no-dangling") == ""


def test_the_worker_start_sweeps_old_unreferenced_temp_dirs_and_nothing_else(
    stack: Stack,
) -> None:
    stack.stop("worker-test")
    root = stack.managed_dir / ".tmp"
    old_orphan, young, stranger = root / ("a" * 16), root / ("b" * 16), root / "not-ours"
    for path in (old_orphan, young, stranger):
        (path / ".git" / "objects").mkdir(parents=True)
        (path / ".git" / "objects" / "x.pack").write_bytes(b"x")
    long_ago = time.time() - 10_000
    os.utime(old_orphan, (long_ago, long_ago))
    os.utime(stranger, (long_ago, long_ago))

    stack.start("worker-test")
    stack.wait_online()
    wait_until(lambda: not old_orphan.exists(), "the orphan sweep", timeout=60)

    assert young.exists()  # 年輕的：可能是進行中的 clone
    assert stranger.exists()  # 名稱不是本系統產生的格式
    assert (stranger / ".git" / "objects" / "x.pack").exists()


# --- 重複提交、失敗不留半套 ---


def test_duplicate_submissions_make_one_job_and_one_clone(stack: Stack, client: TestClient) -> None:
    stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    stack.remote.control(delay_seconds=1)  # 讓第一個 job 還在跑時其他請求到達

    def submit(index: int) -> int:
        spelling = ["octocat/Hello-World", "OCTOCAT/hello-world"][index % 2]
        return _register_job(client, spelling)

    with ThreadPoolExecutor(max_workers=5) as pool:
        ids = list(pool.map(submit, range(5)))

    assert len(set(ids)) == 1
    job = wait_for_status(client, ids[0], DONE, timeout=120)
    assert job["status"] == "succeeded", job
    assert stack.scalar("SELECT count(*) FROM jobs") == 1
    assert _repository_count(stack) == 1
    # 只 clone 一次：一次 clone 只有一次 ref 探索（git 的協定 v2 另有 ls-refs 與 fetch 兩個 POST）
    discoveries = [r for r in stack.remote.requests() if "/info/refs" in r["path"]]
    assert len(discoveries) == 1


def test_clone_failures_leave_no_row_directory_or_temp_dir(
    stack: Stack, client: TestClient
) -> None:
    # (1) GitHub API 說存在，Git 遠端沒有（404）
    stack.remote.add_repo("octocat", "NoGit", bare=False)
    first = wait_for_status(client, _register_job(client, "octocat/NoGit"), DONE, timeout=120)
    # (2) 網路在 clone 途中中斷（伺服器切斷連線）
    stack.remote.add_repo("octocat", "Cut", {"c.txt": "c\n"})
    stack.remote.control(abort=True)
    second = wait_for_status(client, _register_job(client, "octocat/Cut"), DONE, timeout=120)

    for job in (first, second):
        assert job["status"] == "failed", job
        assert job["error"]["code"] == "github_clone_failed"
        assert job["error"]["message"] and "fatal" not in job["error"]["message"]
    assert _repository_count(stack) == 0
    assert _temp_children(stack) == []
    assert not (stack.managed_dir / "github").exists()


def test_a_missing_or_invalid_token_fails_with_a_permanent_error(
    stack: Stack, client: TestClient
) -> None:
    stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    _save_token(stack)
    stack.remote.control(accepted_token="github_pat_TESTONLY_not_the_saved_one")  # noqa: S106

    job = wait_for_status(client, _register_job(client), DONE, timeout=120)

    assert job["status"] == "failed"
    assert job["error"]["code"] == "github_token_invalid"
    assert job["attempt"] == 1  # 永久性：不重試
    assert _repository_count(stack) == 0


# --- 主機端同時讀取 ---


def test_host_reads_during_worker_fetches_never_see_a_broken_repository(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    assert remote is not None
    repository_id = _register(client, stack)
    clone = _clone_path(stack)
    failures: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        commands = [
            ["for-each-ref", "--format=%(refname) %(objectname)"],
            ["log", "--all", "--oneline", "-n", "5"],
            ["rev-parse", "--verify", "origin/main^{commit}"],
            ["cat-file", "--batch-all-objects", "--batch-check"],
        ]
        while not stop.is_set():
            for args in commands:
                done = subprocess.run(  # noqa: S603
                    ["git", "-C", str(clone), *args],  # noqa: S607
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                if done.returncode != 0:
                    failures.append(f"{args[0]}: {done.stderr.decode(errors='replace')[:200]}")

    thread = threading.Thread(target=reader, daemon=True)
    stack.remote.control(delay_seconds=1)  # 每個請求慢一點，讓 fetch 與主機讀取確實重疊
    thread.start()
    try:
        for index in range(3):
            remote.commit({f"file{index}.txt": f"{index}\n"}, message=f"commit {index}")
            job = wait_for_status(client, _fetch_job(client, repository_id), DONE, timeout=120)
            assert job["status"] == "succeeded", job
            assert len(job["result"]["updated_refs"]) == 1
    finally:
        stop.set()
        thread.join(timeout=60)

    assert failures == []
    assert git(clone, "rev-parse", "origin/main") == remote.head()
    assert git(clone, "fsck", "--full", "--no-dangling") == ""  # 雙方寫讀後 repo 完好
    assert not list(clone.rglob("*.lock"))  # 沒有殘留的鎖檔


# --- 移除互動 ---


def test_removal_is_refused_during_a_container_fetch_and_works_after(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    assert remote is not None
    repository_id = _register(client, stack)
    remote.commit({"x.txt": "x\n"})
    stack.remote.control(delay_seconds=300)
    job_id = _fetch_job(client, repository_id)
    wait_until(lambda: " fetch " in _container_commands(stack), "git fetch to run")

    during = client.delete(f"/api/v1/repositories/{repository_id}")
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    wait_for_status(client, job_id, DONE, timeout=60)
    after = client.delete(f"/api/v1/repositories/{repository_id}")

    assert during.status_code == 409
    assert during.json()["error"]["code"] == "repository_busy"
    assert after.status_code == 200
    assert after.json()["removed_clone"] is True
    assert not _clone_path(stack).exists()


def test_the_registered_clone_can_be_removed_from_the_host_after_container_writes(
    stack: Stack, client: TestClient
) -> None:
    """容器寫入的 `.git/objects/pack/*` 在 Windows 為唯讀屬性：D33 移除仍要成功（切片 1 回歸）。"""
    stack.remote.add_repo("octocat", "Hello-World", dict(REPO_FILES))
    repository_id = _register(client, stack)

    response = client.delete(f"/api/v1/repositories/{repository_id}")

    assert response.status_code == 200
    assert response.json()["removed_clone"] is True
    assert not _clone_path(stack).exists()
