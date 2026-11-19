"""本機模型設定在啟動外部行程前驗證。"""

from pathlib import Path

import pytest

from repopilot.embedding.local_runtime import LocalEmbeddingRuntime


@pytest.mark.parametrize(
    ("digest", "timeout"),
    [
        ("bad", 30.0),
        ("0" * 64, 0.0),
        ("0" * 64, float("inf")),
    ],
)
def test_rejects_invalid_runtime_configuration(tmp_path: Path, digest: str, timeout: float) -> None:
    with pytest.raises(ValueError):
        LocalEmbeddingRuntime(
            executable=tmp_path / "runtime",
            model=tmp_path / "model",
            model_sha256=digest,
            timeout_seconds=timeout,
        )


def test_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalEmbeddingRuntime(
            executable=Path("runtime"), model=tmp_path / "model", model_sha256="0" * 64
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"threads": 0},
        {"threads": 257},
        {"threads": True},
        {"threads": 2.0},
        {"gpu_layers": -1},
        {"gpu_layers": 1000},
        {"gpu_layers": False},
        {"gpu_layers": "99"},
    ],
)
def test_rejects_invalid_compute_settings(tmp_path: Path, overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LocalEmbeddingRuntime(
            executable=tmp_path / "runtime",
            model=tmp_path / "model",
            model_sha256="0" * 64,
            **overrides,  # type: ignore[arg-type]
        )


def _flags(runtime: LocalEmbeddingRuntime) -> dict[str, str]:
    args = runtime.command()
    return {flag: args[index + 1] for index, flag in enumerate(args[:-1]) if flag.startswith("--")}


def test_the_default_command_keeps_the_previous_cpu_behaviour(tmp_path: Path) -> None:
    runtime = LocalEmbeddingRuntime(
        executable=tmp_path / "runtime", model=tmp_path / "model", model_sha256="0" * 64
    )

    flags = _flags(runtime)

    assert flags["--threads"] == flags["--threads-batch"] == "2"
    assert flags["--n-gpu-layers"] == "0"
    assert flags["--host"] == "127.0.0.1" and "--offline" in runtime.command()
    assert flags["--parallel"] == "1" and flags["--ctx-size"] == "512"


def test_threads_and_gpu_layers_only_change_those_flags(tmp_path: Path) -> None:
    base = LocalEmbeddingRuntime(
        executable=tmp_path / "runtime", model=tmp_path / "model", model_sha256="0" * 64
    )
    tuned = LocalEmbeddingRuntime(
        executable=tmp_path / "runtime",
        model=tmp_path / "model",
        model_sha256="0" * 64,
        threads=8,
        gpu_layers=99,
    )

    changed = {flag for flag, value in _flags(tuned).items() if _flags(base)[flag] != value}

    assert changed == {"--threads", "--threads-batch", "--n-gpu-layers"}
    assert _flags(tuned)["--threads"] == _flags(tuned)["--threads-batch"] == "8"
    assert _flags(tuned)["--n-gpu-layers"] == "99"
