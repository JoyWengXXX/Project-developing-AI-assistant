"""測試用的本機 Git smart HTTP 伺服器：以 `git http-backend`（CGI）提供 bare repo。

用途：驗證 token 經 `http.<base>/.extraheader` 真的送到伺服器（記錄每個請求的 Authorization）、
可模擬「需要認證的私有 repo」（缺少或不符的標頭回 401）與「很慢的伺服器」（逾時測試）。
只綁定 127.0.0.1；沒有外部連線。
"""

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class GitHttpServer:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        # 每個請求的 (path, Authorization 標頭或 None)
        self.requests: list[tuple[str, str | None]] = []
        # 非 None 時，Authorization 標頭必須完全相符，否則回 401
        self.required_authorization: str | None = None
        # 每個請求先等待的秒數（可被 stop 中斷），用來模擬很慢的伺服器
        self.delay_seconds = 0.0
        self._stopping = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return  # 不輸出存取日誌

            def do_GET(self) -> None:
                self._serve()

            def do_POST(self) -> None:
                self._serve()

            def _serve(self) -> None:
                authorization = self.headers.get("Authorization")
                outer.requests.append((self.path, authorization))
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if outer.delay_seconds and outer._stopping.wait(outer.delay_seconds):
                    return
                required = outer.required_authorization
                if required is not None and authorization != required:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="test"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                parts = urlsplit(self.path)
                env = {
                    **os.environ,
                    "GIT_PROJECT_ROOT": str(outer.project_root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": parts.path,
                    "QUERY_STRING": parts.query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "REMOTE_ADDR": "127.0.0.1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                }
                if encoding := self.headers.get("Content-Encoding"):
                    env["HTTP_CONTENT_ENCODING"] = encoding
                if protocol := self.headers.get("Git-Protocol"):
                    env["GIT_PROTOCOL"] = protocol
                completed = subprocess.run(
                    ["git", "http-backend"],  # noqa: S607
                    input=body,
                    capture_output=True,
                    env=env,
                    timeout=30,
                    check=False,
                )
                self._reply_from_cgi(completed.stdout)

            def _reply_from_cgi(self, output: bytes) -> None:
                header_blob, _, payload = output.partition(b"\r\n\r\n")
                status = 200
                headers: list[tuple[str, str]] = []
                for line in header_blob.decode("latin-1").split("\r\n"):
                    name, _, value = line.partition(":")
                    if name.lower() == "status":
                        status = int(value.strip().split()[0])
                    elif name:
                        headers.append((name, value.strip()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler
