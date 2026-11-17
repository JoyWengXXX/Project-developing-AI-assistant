"""Synthetic scale measurement of retrieval search SQL.

Set PERF_DATABASE_URL to a throwaway database named repopilot_perf (migrations applied), then run
`uv run python scripts/eval/perf_search.py <output.json>`. The script TRUNCATEs repositories.
"""

# ruff: noqa: E501, S101, S311

import json
import math
import os
import platform
import random
import statistics
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from repopilot.search_query import keyword_terms
from repopilot.services.retrieval_search import _keyword_candidates, _semantic_candidates

URL = os.environ["PERF_DATABASE_URL"]
SNAPSHOTS_LARGE = 20
CHUNKS_PER_LARGE = 4000
VECTORS_PER_CHUNK = 6
CHUNK_BYTES = 7300
POOL = 1000
OUT = sys.argv[1]

engine = create_engine(URL)
assert engine.url.database == "repopilot_perf"
rng = random.Random(7)


def unit_vector() -> list[float]:
    v = [rng.gauss(0, 1) for _ in range(768)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


report: dict = {
    "platform": platform.platform(),
    "python": platform.python_version(),
    "chunk_bytes": CHUNK_BYTES,
    "vectors_per_chunk": VECTORS_PER_CHUNK,
    "tiers": [],
}

with engine.begin() as c:
    c.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))
    c.execute(text("DROP TABLE IF EXISTS vec_pool"))
    c.execute(text("CREATE TABLE vec_pool (i int primary key, v vector(768))"))
    for i in range(POOL):
        c.execute(
            text("INSERT INTO vec_pool VALUES (:i, CAST(:v AS vector))"),
            {"i": i, "v": "[" + ",".join(f"{x:.6f}" for x in unit_vector()) + "]"},
        )
    c.execute(
        text("INSERT INTO repositories (name, canonical_local_path) VALUES ('perf', '/perf')")
    )


def add_snapshot(n_chunks: int, label: str) -> tuple[int, int]:
    t0 = time.perf_counter()
    with engine.begin() as c:
        sha = f"{abs(hash(label)) % 10**12:012d}" + "a" * 28
        snap = c.execute(
            text(
                "INSERT INTO snapshots (repository_id, commit_sha, import_profile_hash, status, "
                "file_count) VALUES (1, :sha, 'p', 'completed', :n) RETURNING id"
            ),
            {"sha": sha, "n": n_chunks},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO snapshot_files (snapshot_id, path, content, byte_size, status) "
                "SELECT :s, 'src/d' || (g % 100) || '/f' || g || '.py', body, octet_length(body), "
                "'imported' FROM (SELECT g, "
                "  CASE WHEN g % 100 = 0 THEN 'rareneedle ' ELSE '' END || "
                "  repeat(md5(g::text || CAST(:s AS text)) || ' commonword ', :reps) AS body "
                "  FROM generate_series(1, :n) g) t"
            ),
            {"s": snap, "reps": CHUNK_BYTES // 45, "n": n_chunks},
        )
        cs = c.execute(
            text(
                "INSERT INTO retrieval_chunk_sets (snapshot_id, profile_hash, max_bytes, chunk_count) "
                "VALUES (:s, repeat('b', 64), 4096, :n) RETURNING id"
            ),
            {"s": snap, "n": n_chunks},
        ).scalar_one()
        idx = c.execute(
            text(
                "INSERT INTO retrieval_indexes (snapshot_id, chunk_set_id, profile_hash, profile, "
                "status, segment_count, completed_at) VALUES (:s, :cs, repeat('c', 64), '{}'::jsonb, "
                "'completed', :sc, now()) RETURNING id"
            ),
            {"s": snap, "cs": cs, "sc": n_chunks * VECTORS_PER_CHUNK},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO retrieval_chunks (snapshot_id, chunk_set_id, snapshot_file_id, ordinal, "
                "start_offset, end_offset, start_line, end_line, split_kind, content_hash) "
                "SELECT :s, :cs, f.id, 0, 0, f.byte_size, 1, 160, 'text', md5(f.id::text) "
                "FROM snapshot_files f WHERE f.snapshot_id = :s"
            ),
            {"s": snap, "cs": cs},
        )
        c.execute(
            text(
                "INSERT INTO retrieval_vectors (index_id, chunk_set_id, chunk_id, ordinal, "
                "start_offset, end_offset, token_count, embedding) "
                "SELECT :idx, :cs, k.id, o, o * 1000, o * 1000 + 999, 350, p.v "
                "FROM retrieval_chunks k CROSS JOIN generate_series(0, :vpc - 1) o "
                "JOIN vec_pool p ON p.i = (k.id * 7 + o) % :pool "
                "WHERE k.snapshot_id = :s"
            ),
            {"idx": idx, "cs": cs, "vpc": VECTORS_PER_CHUNK, "pool": POOL, "s": snap},
        )
    return idx, round(time.perf_counter() - t0, 1)


def sizes() -> dict:
    with engine.connect() as c:
        row = c.execute(
            text(
                "SELECT pg_total_relation_size('retrieval_vectors'), "
                "pg_total_relation_size('retrieval_chunks'), "
                "pg_total_relation_size('snapshot_files'), "
                "(SELECT count(*) FROM retrieval_vectors), (SELECT count(*) FROM retrieval_chunks)"
            )
        ).one()
    return {
        "vectors_mib": round(row[0] / 2**20, 1),
        "chunks_mib": round(row[1] / 2**20, 1),
        "files_mib": round(row[2] / 2**20, 1),
        "vector_rows": row[3],
        "chunk_rows": row[4],
    }


def measure(index_ids: list[int], repeats: int = 5) -> dict:
    qvec = unit_vector()
    out: dict = {}
    cases = {
        "keyword_rare": lambda s: _keyword_candidates(
            s, index_ids, keyword_terms("rareneedle"), 100
        ),
        "keyword_common": lambda s: _keyword_candidates(
            s, index_ids, keyword_terms("commonword rareneedle"), 100
        ),
        "semantic": lambda s: _semantic_candidates(s, index_ids, qvec, 100),
    }
    for name, fn in cases.items():
        times = []
        for _ in range(repeats):
            with Session(engine) as s:
                s.execute(text("SET statement_timeout = 120000"))
                t0 = time.perf_counter()
                rows = fn(s)
                times.append(time.perf_counter() - t0)
        out[name] = {
            "first_s": round(times[0], 3),
            "median_s": round(statistics.median(times), 3),
            "max_s": round(max(times), 3),
            "rows": len(rows),
        }
    return out


# Tier 0: real-repo sized snapshot (~408 chunks)
idx0, secs = add_snapshot(408, "real-sized")
with engine.begin() as c:
    c.execute(text("ANALYZE"))
report["tiers"].append(
    {
        "name": "1 snapshot, 408 chunks (real-repo sized)",
        "insert_s": secs,
        **sizes(),
        "latency": measure([idx0]),
    }
)
print(json.dumps(report["tiers"][-1], indent=1), flush=True)

large = []
for i in range(SNAPSHOTS_LARGE):
    idx, secs = add_snapshot(CHUNKS_PER_LARGE, f"large-{i}")
    large.append(idx)
    print(f"built large snapshot {i + 1}/{SNAPSHOTS_LARGE} in {secs}s", flush=True)
    if i + 1 in (1, 5, 20):
        with engine.begin() as c:
            c.execute(text("ANALYZE"))
        tier = {
            "name": f"{i + 1} snapshot(s) x {CHUNKS_PER_LARGE} chunks searched (DB holds all built so far)",
            "insert_s_last": secs,
            **sizes(),
            "latency": measure(large[: i + 1]),
        }
        report["tiers"].append(tier)
        print(json.dumps(tier, indent=1), flush=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=1)
print("done")
