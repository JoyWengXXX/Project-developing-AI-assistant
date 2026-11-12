"""測試堆疊用的假 GitHub：同一個程序同時提供 GitHub REST API 與 Git smart HTTP。

只在 `worker-test` 容器內執行（見 docker-compose.yml 的 `command`），綁定 127.0.0.1，所以 worker 的
`GITHUB_API_BASE_URL`／`GITHUB_CLONE_BASE_URL` 可以是 `http://127.0.0.1:<埠>`——這正好通過
`Settings` 對「http 只允許本機迴圈位址」的驗證，不需要為了測試放寬任何正式設定。

與測試（Windows 主機）共用一個 bind mount 目錄（`FAKE_REMOTE_DIR`，預設 `/mnt/remote`）：
- `git/<owner>/<name>.git`：bare repo（測試在主機端用 git 建立與推送）。
- `api.json`：`{"owner/name": {"full_name": ..., "private": bool, "default_branch": ...}}`
  （鍵為小寫）。
- `control.json`：每個請求都重新讀取，測試用來操控行為：`delay_seconds`（Git 請求先等再回應，
  製造慢速遠端）、`bytes_per_second`（限制 Git 回應速度，讓 clone 持續數秒以觀察進度）、`abort`
  （Git 請求直接切斷連線）、`required_authorization`（Git 請求必須帶這個 Authorization 標頭，
  否則 401）、`accepted_token`（API 請求帶了別的 Bearer token 回 401）、`api_status`（所有 API
  請求回此狀態碼）。
- `requests.log`：每個請求一行 JSON（路徑與 Authorization 標頭），供測試確認 token 真的送達。

只用標準函式庫；不是產品程式碼，不會進入映像（以唯讀 bind mount 掛入）。
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("FAKE_REMOTE_DIR", "/mnt/remote"))
PORT = int(os.environ.get("FAKE_REMOTE_PORT", "18765"))
_LOG_LOCK = threading.Lock()


def _control() -> dict[str, Any]:
    try:
        loaded = json.loads((ROOT / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _log(path: str, authorization: str | None) -> None:
    line = json.dumps({"path": path, "authorization": authorization, "t": time.time()})
    with _LOG_LOCK, (ROOT / "requests.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def _serve(self) -> None:
        # 客戶端（被取消或逾時的 git）已經離開時寫入會失敗：忽略
        with contextlib.suppress(BrokenPipeError, ConnectionError):
            self._handle()

    def _handle(self) -> None:
        authorization = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _log(self.path, authorization)
        control = _control()
        if self.path.startswith("/repos/"):
            self._api(authorization, control)
        else:
            delay = control.get("delay_seconds")  # 只延遲 Git 請求（API 逾時是另一個情境）
            if isinstance(delay, int | float) and delay > 0:
                time.sleep(float(delay))
            self._git(authorization, control, body)

    def _send(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _api(self, authorization: str | None, control: dict[str, Any]) -> None:
        forced = control.get("api_status")
        if isinstance(forced, int):
            self._send(forced, b'{"message": "forced"}')
            return
        accepted = control.get("accepted_token")
        if (
            authorization is not None
            and isinstance(accepted, str)
            and authorization != f"Bearer {accepted}"
        ):
            self._send(401, b'{"message": "bad credentials"}')
            return
        try:
            registry = json.loads((ROOT / "api.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            registry = {}
        key = urlsplit(self.path).path.removeprefix("/repos/").lower()
        entry = registry.get(key)
        if entry is None or (entry.get("private") and authorization is None):
            self._send(404, b'{"message": "Not Found"}')
            return
        owner, _, name = entry["full_name"].partition("/")
        payload = {
            "id": 1,
            "name": name,
            "full_name": entry["full_name"],
            "private": bool(entry.get("private")),
            "owner": {"login": owner},
            "default_branch": entry.get("default_branch", "main"),
        }
        self._send(200, json.dumps(payload).encode())

    def _git(self, authorization: str | None, control: dict[str, Any], body: bytes) -> None:
        if control.get("abort"):
            self.connection.close()  # 不回任何東西：模擬網路中斷
            return
        required = control.get("required_authorization")
        if isinstance(required, str) and authorization != required:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="fake"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        parts = urlsplit(self.path)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/tmp",  # noqa: S108 - 容器內的 tmpfs
            "GIT_CONFIG_GLOBAL": os.devnull,
            # bind mount 上的檔案擁有者是 root，worker 是 10001：只在這個假伺服器放寬
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
            "GIT_PROJECT_ROOT": str(ROOT / "git"),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": parts.path,
            "QUERY_STRING": parts.query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": "127.0.0.1",
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
            timeout=120,
            check=False,
        )
        header_blob, _, payload = completed.stdout.partition(b"\r\n\r\n")
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
        rate = control.get("bytes_per_second")
        if isinstance(rate, int | float) and rate > 0:
            chunk = max(1024, int(rate // 10))
            for start in range(0, len(payload), chunk):
                self.wfile.write(payload[start : start + chunk])
                self.wfile.flush()
                time.sleep(chunk / float(rate))
        else:
            self.wfile.write(payload)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    (ROOT / "server.ready").write_text(str(time.time()), encoding="utf-8")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
