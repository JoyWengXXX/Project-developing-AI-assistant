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
