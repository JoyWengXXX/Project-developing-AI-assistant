"""隔離推論環境的身分與網路前置條件。"""

from pathlib import Path

import pytest

from repopilot.embedding.isolated_runtime import require_network_isolation, runtime_digest
from repopilot.embedding.local_runtime import EmbeddingRuntimeError


def test_runtime_digest_includes_executable_and_system_libraries(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    libraries = tmp_path / "libs"
    bundle.mkdir()
    libraries.mkdir()
    executable = bundle / "llama-server"
    library = libraries / "libgomp.so.1"
    executable.write_bytes(b"fake executable")
    library.write_bytes(b"fake library")
    original = runtime_digest(bundle, libraries)
    assert runtime_digest(bundle, libraries) == original
    library.write_bytes(b"changed library")
    assert runtime_digest(bundle, libraries) != original
    library.write_bytes(b"fake library")
    executable.write_bytes(b"changed executable")
    assert runtime_digest(bundle, libraries) != original


def test_runtime_digest_ignores_gpu_driver_libraries_injected_by_the_host(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    libraries = tmp_path / "libs"
    bundle.mkdir()
    libraries.mkdir()
    (bundle / "llama-server").write_bytes(b"fake executable")
    (libraries / "libgomp.so.1").write_bytes(b"fake library")
    without_gpu = runtime_digest(bundle, libraries)

    # NVIDIA container runtime 啟動時掛進來的主機驅動函式庫（版本隨主機驅動而變）
    for name in (
        "libcuda.so",
        "libcuda.so.1",
        "libcudadebugger.so.1",
        "libdxcore.so",
        "libnvidia-ml.so.1",
        "libnvidia-gpucomp.so.590.62",
    ):
        (libraries / name).write_bytes(b"driver 590.62")
    assert runtime_digest(bundle, libraries) == without_gpu
    (libraries / "libnvidia-ml.so.1").write_bytes(b"driver 595.00")
    assert runtime_digest(bundle, libraries) == without_gpu

    # 名稱相近但不是驅動函式庫的檔案仍然納入
    (libraries / "libcudart.so.12").write_bytes(b"not injected")
    assert runtime_digest(bundle, libraries) != without_gpu


def test_runtime_digest_still_requires_a_non_driver_system_library(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    libraries = tmp_path / "libs"
    bundle.mkdir()
    libraries.mkdir()
    (bundle / "llama-server").write_bytes(b"fake executable")
    (libraries / "libcuda.so.1").write_bytes(b"driver only")

    with pytest.raises(EmbeddingRuntimeError):
        runtime_digest(bundle, libraries)


def test_network_isolation_refuses_non_loopback_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("socket.if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    with pytest.raises(EmbeddingRuntimeError):
        require_network_isolation()
