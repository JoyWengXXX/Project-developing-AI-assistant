#!/bin/sh
# 第二階段切片 1 spike：從容器內測試某個主機名稱的名稱解析與 6379 的 Redis PING（Python socket，不需額外套件）。
# 用法：ping.sh <主機名稱> [埠，預設 6379]
python - "$1" "${2:-6379}" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
try:
    addr = socket.gethostbyname(host)
except OSError as e:
    print("resolve failed:", type(e).__name__, e)
    raise SystemExit(0)
try:
    s = socket.create_connection((host, port), timeout=3)
    s.sendall(b"PING\r\n")
    print("resolved", addr, "reply:", s.recv(16))
    s.close()
except OSError as e:
    print("resolved", addr, "connect failed:", type(e).__name__, e)
PY
