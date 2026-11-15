"""在 Linux 測試真正的 Unix socket 與檔案權限。"""

import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from repopilot.embedding.local_runtime import EmbeddingRuntimeError
from repopilot.embedding.profile import EmbeddingProfile
from repopilot.embedding.socket_client import SocketEmbeddingClient
from repopilot.embedding.socket_server import handle_request


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"), "Unix socket required")
class TestEmbeddingUnixSocket(unittest.TestCase):
    def test_round_trip_and_rejection_of_public_socket(self) -> None:
        profile = EmbeddingProfile(model_sha256="a" * 64, runtime_bundle_sha256="b" * 64)
        with tempfile.TemporaryDirectory() as folder:
            os.chmod(folder, 0o700)
            path = Path(folder) / "runtime.sock"
            unix_family = getattr(socket, "AF_UNIX", None)
            assert unix_family is not None
            with socket.socket(unix_family, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                os.chmod(path, 0o600)
                server.listen()
                server.settimeout(15)

                def serve() -> None:
                    for _ in range(3):
                        with server.accept()[0] as connection:
                            handle_request(
                                connection,
                                profile=profile,
                                count_tokens=lambda text: len(text) + 1,
                                embed=lambda text: [1.0] + [0.0] * 767,
                            )

                thread = threading.Thread(target=serve)
                thread.start()
                client = SocketEmbeddingClient(socket_path=path, profile=profile)
                try:
                    client.verify_identity()
                    self.assertEqual(len(client.embed("fake source")), 768)
                finally:
                    thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
                os.chmod(path, 0o666)  # noqa: S103 - 驗證拒絕不安全權限
                with self.assertRaises(EmbeddingRuntimeError):
                    client.verify_identity()
