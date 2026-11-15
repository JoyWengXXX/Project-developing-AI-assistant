"""探測隔離推論 socket 是否能處理識別請求。"""

import socket
from pathlib import Path

from repopilot.embedding.socket_protocol import receive_message, send_message


def socket_ready(path: Path) -> bool:
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(path))
            send_message(connection, {"operation": "identity"})
            response = receive_message(connection)
            return response.get("ok") is True
    except (OSError, ValueError):
        return False


def main() -> int:
    return 0 if socket_ready(Path("/run/embed/runtime.sock")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
