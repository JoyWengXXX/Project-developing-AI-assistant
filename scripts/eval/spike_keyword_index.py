"""H5 spike: does a pg_trgm index make retrieval keyword search fast enough, and is it equivalent?

Compares the current query (`_keyword_candidates`: scan every chunk, substr + lower + strpos) with
an alternative that stores a lowercased copy of each chunk in `retrieval_chunks.content_lower`
(GIN, gin_trgm_ops) and filters with LIKE. Text is real source code from this working tree,
replicated with a unique prefix per copy, so trigram frequencies are realistic but replicas share
their trigram sets (index size is therefore a lower bound for truly diverse text).

Set SPIKE_DATABASE_URL to a throwaway database named repopilot_perf (migrations applied) and run
`uv run python scripts/eval/spike_keyword_index.py <output.json>`. It TRUNCATEs repositories and
ALTERs retrieval_chunks, so never point it at a shared database.
"""

# ruff: noqa: E501, S101, S607, S608, B023

import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from repopilot.search_query import keyword_terms
from repopilot.services.retrieval_search import _keyword_candidates

URL = os.environ["SPIKE_DATABASE_URL"]
OUT = sys.argv[1]
TIERS = [(408, "real-repo sized"), (20_000, "20k chunks"), (80_000, "80k chunks")]
PIECE_CHARS = 8000

# term sets: (label, query). Terms come from keyword_terms(): \w+, lowercased, de-duplicated.
QUERIES = [
    ("rare word", "advisory"),
    ("rare identifier with underscore", "retrieval_chunk_set"),
    ("common word", "self"),
    ("two common words", "import return"),
    ("2-char term (no trigram)", "id"),
    ("mixed rare+2-char", "advisory id"),
    ("CJK term", "資料庫"),
    ("upper case input", "FERNET"),
    ("no match", "zzzqqqxxx"),
    ("natural language", "how are github tokens encrypted before saving"),
]

engine = create_engine(URL)
assert engine.url.database == "repopilot_perf"


def pieces() -> list[str]:
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    result: list[str] = []
    files = subprocess.check_output(
        ["git", "ls-files", "src", "tests", "README.md"], cwd=root, text=True
    ).split()
    files += (
        [str(p.relative_to(root)) for p in (root / "docs").rglob("*.md")]
        if (root / "docs").exists()
        else []
    )
    for name in files:
        path = root / name
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        current = ""
        for line in body.splitlines(keepends=True):
            if current and len(current) + len(line) > PIECE_CHARS:
                result.append(current)
                current = ""
            current += line
        if current.strip():
            result.append(current)
    return result


def build(n_chunks: int, template_count: int) -> int:
    with engine.begin() as c:
        c.execute(text("TRUNCATE repositories RESTART IDENTITY CASCADE"))
        c.execute(
            text("INSERT INTO repositories (name, canonical_local_path) VALUES ('spike', '/spike')")
        )
        snap = c.execute(
            text(
                "INSERT INTO snapshots (repository_id, commit_sha, import_profile_hash, status, file_count) "
                "VALUES (1, repeat('a', 40), 'p', 'completed', :n) RETURNING id"
            ),
            {"n": n_chunks},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO snapshot_files (snapshot_id, path, content, byte_size, status) "
                "SELECT :s, 'f/' || x.g || '/' || t.id || '.txt', b.body, octet_length(b.body), 'imported' "
                "FROM (SELECT g, ((g - 1) % :tc) + 1 AS tid FROM generate_series(1, :n) g) x "
                "JOIN spike_templates t ON t.id = x.tid "
                "CROSS JOIN LATERAL (SELECT '# replica ' || x.g || E'\\n' || t.body AS body) b",
            ),
            {"s": snap, "n": n_chunks, "tc": template_count},
        )
        cs = c.execute(
            text(
                "INSERT INTO retrieval_chunk_sets (snapshot_id, profile_hash, max_bytes, chunk_count) "
                "VALUES (:s, repeat('b', 64), 32768, :n) RETURNING id"
            ),
            {"s": snap, "n": n_chunks},
        ).scalar_one()
        idx = c.execute(
            text(
                "INSERT INTO retrieval_indexes (snapshot_id, chunk_set_id, profile_hash, profile, status, segment_count, completed_at) "
                "VALUES (:s, :cs, repeat('c', 64), '{}'::jsonb, 'completed', 0, now()) RETURNING id"
            ),
            {"s": snap, "cs": cs},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO retrieval_chunks (snapshot_id, chunk_set_id, snapshot_file_id, ordinal, start_offset, end_offset, "
                "start_line, end_line, split_kind, content_hash) "
                "SELECT :s, :cs, f.id, 0, 0, char_length(f.content), 1, 100, 'text', md5(f.id::text) "
                "FROM snapshot_files f WHERE f.snapshot_id = :s"
            ),
            {"s": snap, "cs": cs},
        )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("ANALYZE"))
    return int(idx)


def size_mib(relation: str) -> float:
    with engine.connect() as c:
        return round(
            float(c.scalar(text("SELECT pg_total_relation_size(:r)"), {"r": relation})) / 2**20, 1
        )


def like_pattern(term: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", term) + "%"


def keyword_like(index_ids: list[int], terms: list[str], limit: int = 100) -> list[int]:
    score = " + ".join(
        f"(CASE WHEN strpos(c.content_lower, :t{i}) > 0 THEN 1 ELSE 0 END)"
        for i in range(len(terms))
    )
    params: dict[str, object] = {f"t{i}": term for i, term in enumerate(terms)}
    params["patterns"] = [like_pattern(t) for t in terms]
    params["ids"] = index_ids
    params["limit"] = limit
    sql = f"""
        SELECT c.id
        FROM retrieval_chunks c
        JOIN retrieval_indexes i ON i.chunk_set_id = c.chunk_set_id
        JOIN snapshots s ON s.id = c.snapshot_id
        JOIN snapshot_files f ON f.id = c.snapshot_file_id
        WHERE i.id = ANY(:ids) AND i.status = 'completed' AND f.status = 'imported'
          AND c.content_lower LIKE ANY(CAST(:patterns AS text[]))
          AND ({score}) > 0
        ORDER BY ({score}) DESC, s.repository_id, s.id, f.path COLLATE "C", c.start_offset, c.id
        LIMIT :limit
    """
    with Session(engine) as session:
        session.execute(text("SET statement_timeout = 300000"))
        return [row[0] for row in session.execute(text(sql), params)]


def keyword_current(index_ids: list[int], terms: list[str]) -> list[int]:
    with Session(engine) as session:
        session.execute(text("SET statement_timeout = 300000"))
        return [m.chunk_id for m in _keyword_candidates(session, index_ids, terms, 100)]


def timed(fn, repeats: int = 3):  # type: ignore[no-untyped-def]
    times, rows = [], []
    for _ in range(repeats):
        t0 = time.perf_counter()
        rows = fn()
        times.append(time.perf_counter() - t0)
    return round(statistics.median(times), 3), rows


def explain_uses_index(index_ids: list[int], terms: list[str]) -> bool:
    patterns = [like_pattern(t) for t in terms]
    with engine.connect() as c:
        plan = c.execute(
            text(
                "EXPLAIN SELECT c.id FROM retrieval_chunks c WHERE c.content_lower LIKE ANY(CAST(:p AS text[]))"
            ),
            {"p": patterns},
        ).all()
    return any("ix_spike_content_lower_trgm" in row[0] for row in plan)


report: dict[str, object] = {
    "platform": platform.platform(),
    "python": platform.python_version(),
    "tiers": [],
}
with engine.begin() as c:
    c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    c.execute(text("DROP TABLE IF EXISTS spike_templates"))
    c.execute(text("CREATE TABLE spike_templates (id int PRIMARY KEY, body text NOT NULL)"))
    templates = pieces()
    for i, body in enumerate(templates, 1):
        c.execute(text("INSERT INTO spike_templates VALUES (:i, :b)"), {"i": i, "b": body})
report["template_pieces"] = len(templates)
report["template_chars"] = sum(len(t) for t in templates)

for n_chunks, label in TIERS:
    with engine.begin() as c:
        c.execute(text("ALTER TABLE retrieval_chunks DROP COLUMN IF EXISTS content_lower"))
    index_id = build(n_chunks, len(templates))
    tier: dict[str, object] = {
        "label": label,
        "chunks": n_chunks,
        "files_mib": size_mib("snapshot_files"),
        "chunks_mib_before": size_mib("retrieval_chunks"),
    }
    t0 = time.perf_counter()
    with engine.begin() as c:
        c.execute(text("ALTER TABLE retrieval_chunks ADD COLUMN content_lower text"))
        c.execute(
            text(
                "UPDATE retrieval_chunks c SET content_lower = lower(substr(f.content, c.start_offset + 1, c.end_offset - c.start_offset)) "
                "FROM snapshot_files f WHERE f.id = c.snapshot_file_id"
            )
        )
    tier["backfill_s"] = round(time.perf_counter() - t0, 1)
    t0 = time.perf_counter()
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE INDEX ix_spike_content_lower_trgm ON retrieval_chunks USING gin (content_lower gin_trgm_ops)"
            )
        )
    tier["index_build_s"] = round(time.perf_counter() - t0, 1)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("ANALYZE retrieval_chunks"))
    with engine.connect() as c:
        tier["content_lower_mib_total_chunks_table"] = size_mib("retrieval_chunks")
        tier["gin_index_mib"] = round(
            c.scalar(text("SELECT pg_relation_size('ix_spike_content_lower_trgm')")) / 2**20, 1
        )
    results = []
    for qlabel, query in QUERIES:
        terms = keyword_terms(query)
        t_cur, rows_cur = timed(lambda: keyword_current([index_id], terms))
        t_new, rows_new = timed(lambda: keyword_like([index_id], terms))
        results.append(
            {
                "query": qlabel,
                "terms": terms,
                "current_median_s": t_cur,
                "like_trgm_median_s": t_new,
                "rows": len(rows_cur),
                "identical_top100": rows_cur == rows_new,
                "plan_uses_trgm_index": explain_uses_index([index_id], terms),
            }
        )
        print(label, results[-1], flush=True)
    tier["queries"] = results
    report["tiers"].append(tier)  # type: ignore[attr-defined]
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, ensure_ascii=False)

print("done")
