"""無網路的本機 embedding 執行器。"""

import argparse
import hashlib
import importlib
import logging
import os
import re
import signal
import socket
import stat
from pathlib import Path
from types import FrameType

from repopilot.embedding.local_runtime import EmbeddingRuntimeError, LocalEmbeddingRuntime
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_server import handle_request

logger = logging.getLogger("repopilot.embedding.isolated_runtime")


_HOST_DRIVER_LIBRARY = re.compile(
    r"^(?:libcuda\.so|libcudadebugger\.so|libdxcore\.so|libnvidia-[^/]*\.so)"
)


def runtime_digest(bundle: Path, libraries: Path) -> str:
    """涵蓋執行器封裝及映像內的系統共享函式庫。

    NVIDIA container runtime 在啟動容器時會把主機的 GPU 驅動函式庫（`libcuda`、`libnvidia-*`、
    `libdxcore` 等）掛進系統函式庫目錄；它們屬於主機而非映像，版本隨主機驅動變動，所以不納入——
    否則同一個映像在 CPU 與 GPU 模式、或驅動更新後會得到不同的識別，已建立的索引會被視為不相容。
    驅動函式庫的信任邊界與 Docker 本身相同（見 README）。
    """
    if not (bundle / "llama-server").is_file() or not libraries.is_dir():
        raise EmbeddingRuntimeError("The embedding runtime files are unavailable.")
    bundled = sorted(path for path in bundle.rglob("*") if path.is_file())
    system = sorted(
        path
        for path in libraries.glob("*.so*")
        if path.is_file() and not _HOST_DRIVER_LIBRARY.match(path.name)
    )
    if not bundled or not system:
        raise EmbeddingRuntimeError("The embedding runtime files are unavailable.")
    digest = hashlib.sha256()
    for label, root, paths in (("bundle", bundle, bundled), ("system", libraries, system)):
        for path in paths:
            name = f"{label}/{path.relative_to(root).as_posix()}".encode()
            digest.update(len(name).to_bytes(4, "big"))
            digest.update(name)
            file_digest = hashlib.sha256()
            with path.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    file_digest.update(block)
            digest.update(file_digest.digest())
    return digest.hexdigest()


def require_network_isolation() -> None:
    try:
        interfaces = {name for _, name in socket.if_nameindex()}
    except OSError:
        raise EmbeddingRuntimeError("Could not verify network isolation.") from None
    if interfaces != {"lo"}:
        raise EmbeddingRuntimeError("The embedding runtime requires an isolated network namespace.")


def _serve(path: Path, profile: EmbeddingProfile, runtime: LocalEmbeddingRuntime) -> None:
    if os.name != "posix" or not path.is_absolute():
        raise EmbeddingRuntimeError("The isolated embedding socket requires Linux.")
    get_uid = getattr(os, "geteuid", None)
    unix_family = getattr(socket, "AF_UNIX", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if get_uid is None or unix_family is None or no_follow is None:
        raise EmbeddingRuntimeError("The isolated embedding socket is unavailable.")
    directory = path.parent.lstat()
    uid = get_uid()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != uid or directory.st_mode & 0o077:
        raise EmbeddingRuntimeError("The isolated embedding directory is not private.")
    fcntl = importlib.import_module("fcntl")
    descriptor = os.open(path.parent / ".embedding.lock", os.O_CREAT | os.O_RDWR | no_follow, 0o600)
    with os.fdopen(descriptor, "r+b") as lockfile:
        metadata = os.fstat(lockfile.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != uid
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise EmbeddingRuntimeError("The isolated embedding lock is not private.")
        try:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EmbeddingRuntimeError(
                "The isolated embedding runtime is already running."
            ) from None
        try:
            old = path.lstat()
        except FileNotFoundError:
            old = None
        if old is not None:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != uid:
                raise EmbeddingRuntimeError("The existing embedding socket is not safe to replace.")
            path.unlink()
        with socket.socket(unix_family, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(16)
            try:
                while True:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(30)
                        try:
                            handle_request(
                                connection,
                                profile=profile,
                                count_tokens=runtime.count_tokens,
                                embed=runtime.embed,
                            )
                        except OSError:
                            logger.warning("embedding_socket_connection_failed")
            finally:
                if path.exists():
                    path.unlink()


def _terminate(_: int, __: FrameType | None) -> None:
    raise SystemExit(0)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Run an isolated local embedding runtime.")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/opt/llama"))
    parser.add_argument("--library-dir", type=Path, default=Path("/usr/lib/x86_64-linux-gnu"))
    # 只影響速度：不納入 EmbeddingProfile（執行環境雜湊相同則設定相容）。GPU 的向量與 CPU 有
    # 微小數值差異（量測：餘弦相似度 ≥ 0.999998），不是逐位元相同
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--gpu-layers", type=int, default=0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _terminate)
    try:
        require_network_isolation()
        digest = runtime_digest(args.runtime_dir, args.library_dir)
        logger.info("runtime_bundle_sha256=%s", digest)
        logger.info("embedding_compute threads=%d gpu_layers=%d", args.threads, args.gpu_layers)
        profile = EmbeddingProfile(
            model_sha256=args.model_sha256,
            runtime_bundle_sha256=digest,
        )
        with LocalEmbeddingRuntime(
            executable=args.runtime_dir / "llama-server",
            model=args.model,
            model_sha256=args.model_sha256,
            threads=args.threads,
            gpu_layers=args.gpu_layers,
        ) as runtime:
            _serve(args.socket, profile, runtime)
    except EmbeddingRuntimeError as exc:
        logger.error("isolated_embedding_start_failed: %s", exc)
        return 1
    except (OSError, ValueError):
        logger.error("isolated_embedding_configuration_or_io_failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
