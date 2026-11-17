"""Embedding throughput vs llama-server thread count, through the real LocalEmbeddingRuntime path.

Runs inside the (network-less) embedding image. Texts are real source-code pieces (sent in on stdin
as JSON); the production code path is used (tokenize + embed, one text per call, --parallel 1).
Only the value after --threads / --threads-batch is replaced, by wrapping subprocess.Popen.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from repopilot.embedding.local_runtime import LocalEmbeddingRuntime

MODEL = Path("/model/model.gguf")
SHA = os.environ["MODEL_SHA"]
threads_list = [int(x) for x in sys.argv[1].split(",")]
texts: list[str] = json.load(sys.stdin)
WARMUP = 3
real_popen = subprocess.Popen


def run(threads: int) -> dict[str, object]:
    def patched(args: list[str], **kwargs: object) -> "subprocess.Popen[str]":
        args = list(args)
        for flag in ("--threads", "--threads-batch"):
            args[args.index(flag) + 1] = str(threads)
        gpu_layers = os.environ.get("GPU_LAYERS")
        if gpu_layers is not None:
            args[args.index("--n-gpu-layers") + 1] = gpu_layers
        return real_popen(args, **kwargs)  # type: ignore[no-any-return,call-overload]

    setattr(subprocess, "Popen", patched)  # noqa: B010 - 只在本腳本內暫時替換
    try:
        with LocalEmbeddingRuntime(
            executable=Path("/opt/llama/llama-server"), model=MODEL, model_sha256=SHA
        ) as runtime:
            usable = [t for t in texts if runtime.count_tokens(t) <= 512]
            for text in usable[:WARMUP]:
                runtime.embed(text)
            per_call: list[float] = []
            tokens = 0
            vectors = []
            t_all = time.perf_counter()
            for text in usable[WARMUP:]:
                t0 = time.perf_counter()
                vector = runtime.embed(text)
                per_call.append(time.perf_counter() - t0)
                tokens += runtime.count_tokens(text)
                vectors.append(vector)
            total = time.perf_counter() - t_all
    finally:
        setattr(subprocess, "Popen", real_popen)  # noqa: B010
    dump = os.environ.get("DUMP")
    if dump:
        Path(dump).write_text(json.dumps(vectors), encoding="utf-8")
    return {
        "threads": threads,
        "texts": len(per_call),
        "total_s": round(total, 1),
        "vectors_per_s": round(len(per_call) / total, 3),
        "tokens_per_s": round(tokens / total, 1),
        "median_call_s": round(statistics.median(per_call), 3),
        "p95_call_s": round(sorted(per_call)[int(len(per_call) * 0.95) - 1], 3),
        "first_vector_head": [round(v, 5) for v in vectors[0][:4]],
        "digest": sum(round(v, 4) for vec in vectors[:5] for v in vec[:16]),
    }


for count in threads_list:
    print(json.dumps(run(count)), flush=True)
