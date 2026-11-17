"""Run fixed real-repo queries against an isolated API: file-level Hit@5, Recall@5 and MRR.

Usage: python scripts/eval/run_real_eval.py <queries.json> <result.json> <snapshot_id>
Set EVAL_API to override the default http://127.0.0.1:8000/api/v1.
"""

# ruff: noqa: S310, SIM115

import json
import os
import statistics
import sys
import time
import urllib.request

API = os.environ.get("EVAL_API", "http://127.0.0.1:8000/api/v1")
spec = json.load(open(sys.argv[1], encoding="utf-8"))
out_path = sys.argv[2]
snapshot_id = int(sys.argv[3])


def search(query: str, mode: str) -> tuple[list[dict], float]:
    body = json.dumps(
        {"snapshot_ids": [snapshot_id], "query": query, "mode": mode, "limit": 50}
    ).encode()
    req = urllib.request.Request(
        f"{API}/retrieval/search", data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    return data["items"], time.perf_counter() - t0


rows = []
for q in spec["queries"]:
    relevant = set(q["relevant"])
    for mode in ("keyword", "semantic", "hybrid"):
        items, seconds = search(q["query"], mode)
        files: list[str] = []
        for item in items:
            if item["path"] not in files:
                files.append(item["path"])
        top5 = files[:5]
        found = [f for f in top5 if f in relevant]
        rank = next((i + 1 for i, f in enumerate(files) if f in relevant), None)
        rows.append(
            {
                "id": q["id"],
                "query": q["query"],
                "mode": mode,
                "top5_files": top5,
                "hit_at_5": 1.0 if found else 0.0,
                "recall_at_5": len(found) / len(relevant),
                "reciprocal_rank": (1.0 / rank) if rank else 0.0,
                "seconds": round(seconds, 3),
            }
        )

summary = {}
for mode in ("keyword", "semantic", "hybrid"):
    m = [r for r in rows if r["mode"] == mode]
    summary[mode] = {
        "hit_at_5": round(statistics.mean(r["hit_at_5"] for r in m), 3),
        "recall_at_5": round(statistics.mean(r["recall_at_5"] for r in m), 3),
        "mrr": round(statistics.mean(r["reciprocal_rank"] for r in m), 3),
        "median_seconds": round(statistics.median(r["seconds"] for r in m), 3),
        "queries": len(m),
    }
json.dump(
    {"dataset": spec["dataset_version"], "summary": summary, "rows": rows},
    open(out_path, "w", encoding="utf-8"),
    indent=1,
    ensure_ascii=False,
)
print(json.dumps(summary, indent=1))
