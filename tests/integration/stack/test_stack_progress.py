"""bytes_received 實測（切片 5，D44）：容器內 clone 時，暫存目錄大小在 Windows 掛載上是否即時可見。

假遠端以 `bytes_per_second` 限速回應 pack，讓 clone 持續十餘秒；測試每 0.25 秒輪詢
`GET /jobs/{id}`，記錄 `(phase, bytes_received)`。結論（2026-11-12 實測：**即時可見**，約每個
量測間隔前進一次）與數字記錄於 docs（architecture D44、roadmap）。
"""

import os
import time

from fastapi.testclient import TestClient

from tests.integration.stack.conftest import Stack
from tests.integration.stack.test_stack_github import DONE, _register_job


def test_bytes_received_grows_while_the_container_clones_over_the_mount(
    stack: Stack, client: TestClient
) -> None:
    remote = stack.remote.add_repo("octocat", "Big", {"README.md": "big\n"})
    assert remote is not None
    # 兩個 4 MB 的隨機檔（不可壓縮）：pack 約 8 MB，限速後 clone 約 11～15 秒
    remote.commit({"blob1.bin": os.urandom(4_000_000), "blob2.bin": os.urandom(4_000_000)})
    stack.remote.control(bytes_per_second=700_000)

    job_id = _register_job(client, "octocat/Big")
    samples: list[tuple[str, int | None]] = []
    started = time.monotonic()
    while True:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        samples.append((job["phase"], job["progress"]["bytes_received"]))
        if job["status"] in DONE:
            break
        assert time.monotonic() - started < 120, "the clone did not finish"
        time.sleep(0.25)

    assert job["status"] == "succeeded", job
    cloning = [bytes_ for phase, bytes_ in samples if phase == "cloning"]
    positive = [b for b in cloning if b]
    distinct = sorted(set(positive))
    # 進行中就看得到：多個不同的正值，只增不減，不超過實際大小太多（pack 約 8 MB）
    assert len(distinct) >= 4, f"bytes_received did not advance while cloning: {distinct}"
    assert positive == sorted(positive), "bytes_received must never decrease"
    assert distinct[-1] < 9_000_000
    # 不是百分比：沒有總數；完成後保留最後一次量測值
    assert job["progress"]["total_files"] is None
    assert job["progress"]["bytes_received"] is not None
