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


def test_network_isolation_refuses_non_loopback_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("socket.if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    with pytest.raises(EmbeddingRuntimeError):
        require_network_isolation()
