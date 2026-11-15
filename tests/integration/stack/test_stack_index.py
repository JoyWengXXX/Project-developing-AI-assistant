"""真實堆疊的結構索引（切片 6）：容器內的 worker 匯入並建立索引，API 從資料庫讀取。

與 `test_stack_import.py` 同一組容器；這裡只驗證索引階段在真實容器（Linux、Python 3.12 映像）裡
端到端可用，細節行為由 `tests/integration/jobs/test_index_jobs.py` 與
`tests/integration/test_snapshot_indexing.py` 驗證。
"""

import pytest
from fastapi.testclient import TestClient

from tests.integration.stack.conftest import Stack, register, submit, wait_for_status

pytestmark = pytest.mark.stack

FILES = {
    "README.md": "# Demo\n",
    "app/models.py": "class Base: ...\nclass User(Base):\n    async def name(self): ...\n",
    "app/中文.py": "def 函式(): ...\n",
    "app/broken.py": "def oops(:\n",
    "src/Shop.cs": "namespace Shop;\npublic class Order : Base\n{\n    public void Ship() { }\n}\n",
}


def test_the_worker_container_indexes_the_snapshot_and_the_api_serves_the_structure(
    stack: Stack, client: TestClient
) -> None:
    repository_id = register(client, stack.new_repo("indexed", FILES))

    accepted = submit(client, repository_id, revision="HEAD")
    job = wait_for_status(client, accepted["job"]["id"], {"succeeded", "failed"}, timeout=120)

    assert job["status"] == "succeeded", job
    snapshot_id = job["snapshot_id"]
    result = job["result"]
    assert (result["indexed_files"], result["parse_error_files"]) == (4, 1)
    # models：module、Base、User、name；中文：module、函式；Shop.cs：namespace、Order、Ship
    assert result["symbols"] == 9
    index = client.get(f"/api/v1/snapshots/{snapshot_id}/index").json()
    assert index["status"] == "completed"
    assert index["analyzers"]["python"]["python_major_minor"] == "3.12"
    # 容器內的 tree-sitter 套件與 grammar ABI（切片 1 spike 驗證過的 15）
    assert index["analyzers"]["csharp"]["abi_version"] == 15
    assert index["counts"]["files_by_language"] == {"python": 3, "csharp": 1}
    assert index["counts"]["files_by_parse_status"] == {"parsed": 3, "failed": 1}
    problems = client.get(
        f"/api/v1/snapshots/{snapshot_id}/index/files", params={"has_problems": True}
    ).json()
    assert [f["path"] for f in problems["items"]] == ["app/broken.py"]
    symbols = client.get(f"/api/v1/snapshots/{snapshot_id}/symbols", params={"limit": 200}).json()
    names = {s["qualified_name"] for s in symbols["items"]}
    assert {"app.models.User.name", "app.中文.函式", "Shop.Order.Ship"} <= names
    # API（主機）與 worker（容器）算出同一個索引設定雜湊：不同的話提交時 worker 會 profile_mismatch
    again = submit(client, repository_id, revision="HEAD")
    assert again["reused"] is True  # 快照與目前設定的索引都 completed


def test_an_index_job_runs_in_the_container_without_reading_the_repository(
    stack: Stack, client: TestClient
) -> None:
    repository_id = register(client, stack.new_repo("index-only", FILES))
    sync = client.post(f"/api/v1/repositories/{repository_id}/imports", json={})
    assert sync.status_code == 201
    snapshot_id = sync.json()["id"]

    accepted = client.post(f"/api/v1/snapshots/{snapshot_id}/index-jobs")

    assert accepted.status_code == 202
    job = wait_for_status(
        client, accepted.json()["job"]["id"], {"succeeded", "failed"}, timeout=120
    )
    assert job["status"] == "succeeded", job
    assert job["kind"] == "snapshot_index"
    assert client.get(f"/api/v1/snapshots/{snapshot_id}/index").json()["status"] == "completed"


def test_worker_indexes_script_files_from_git_and_api_searches_them(
    stack: Stack, client: TestClient
) -> None:
    repository_id = register(
        client,
        stack.new_repo(
            "scripts",
            {
                "src/auth.ts": 'import { hash as h } from "crypto";\n'
                "export type UserId = string;\n",
                "src/Card.tsx": "export const Card = () => <div>Hello</div>;\n",
                "src/view.jsx": "export function View() { return <div>OK</div>; }\n",
                "src/broken.js": "function okay() {}\nfunction broken( {\n",
            },
        ),
    )
    accepted = submit(client, repository_id, revision="HEAD")
    job = wait_for_status(client, accepted["job"]["id"], {"succeeded", "failed"}, timeout=120)
    assert job["status"] == "succeeded", job
    snapshot_id = job["snapshot_id"]

    index = client.get(f"/api/v1/snapshots/{snapshot_id}/index").json()
    assert index["counts"]["files_by_language"] == {"typescript": 2, "javascript": 2}
    problems = client.get(
        f"/api/v1/snapshots/{snapshot_id}/index/files", params={"has_problems": True}
    ).json()
    assert [(item["path"], item["parse_status"]) for item in problems["items"]] == [
        ("src/broken.js", "parsed_with_errors")
    ]
    results = client.get(
        f"/api/v1/snapshots/{snapshot_id}/search/symbols",
        params={"q": "UserId", "language": "typescript", "kind": "type_alias"},
    ).json()
    assert [(item["name"], item["start_line"]) for item in results["items"]] == [("UserId", 2)]


def test_worker_indexes_java_from_git_and_api_searches_it(stack: Stack, client: TestClient) -> None:
    repository_id = register(
        client,
        stack.new_repo(
            "java",
            {
                "src/Service.java": "package demo;\n"
                "import static java.util.Collections.emptyList;\n"
                "class Service { void run() {} }\n",
                "src/other.py": "def helper(): pass\n",
            },
        ),
    )
    accepted = submit(client, repository_id, revision="HEAD")
    job = wait_for_status(client, accepted["job"]["id"], {"succeeded", "failed"}, timeout=120)
    assert job["status"] == "succeeded", job
    snapshot_id = job["snapshot_id"]

    index = client.get(f"/api/v1/snapshots/{snapshot_id}/index").json()
    assert index["counts"]["files_by_language"] == {"java": 1, "python": 1}
    results = client.get(
        f"/api/v1/snapshots/{snapshot_id}/search/symbols",
        params={"q": "Service", "language": "java", "kind": "class"},
    ).json()
    item = results["items"][0]
    assert (item["name"], item["start_line"]) == ("Service", 3)
    detail = client.get(f"/api/v1/snapshots/{snapshot_id}/symbols/{item['symbol_id']}").json()
    assert [(ref["kind"], ref["target"]) for ref in detail["imports"]] == [
        ("java_static_import", "java.util.Collections.emptyList")
    ]
