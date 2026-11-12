#!/bin/sh
# 第二階段切片 1 spike：NTFS 大小寫語意在容器掛載上的行為（只讀）。
# 用法：case-probe.sh <含 Hello.txt 與 Sub/Inner.txt 的目錄>
d="$1"
echo "--- ls"
ls -la "$d"
for name in Hello.txt hello.txt HELLO.TXT sub/Inner.txt Sub/inner.TXT; do
  if [ -e "$d/$name" ]; then echo "exists: $name => $(cat "$d/$name")"; else echo "missing: $name"; fi
done
python - "$d" <<'PY'
import os
import sys
from pathlib import Path

d = Path(sys.argv[1])
print("listdir:", sorted(os.listdir(d)))
for name in ("hello.txt", "HELLO.TXT", "sub/inner.txt"):
    try:
        print(f"resolve_strict({name}):", (d / name).resolve(strict=True))
    except OSError as e:
        print(f"resolve_strict({name}) error:", type(e).__name__, e)
PY
