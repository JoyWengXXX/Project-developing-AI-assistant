#!/bin/sh
# 第二階段切片 1 spike：以 Python 在容器內檢查路徑（resolve(strict=True)、is_symlink、類型、stat）。
# 用法：links-probe.sh <路徑>...
# 只讀；輸出每個路徑的實際型別，供與主機端 Path.resolve() 比對。
python - "$@" <<'PY'
import os
import stat
import sys
from pathlib import Path

for raw in sys.argv[1:]:
    p = Path(raw)
    row = {"path": raw}
    try:
        lst = os.lstat(raw)
        row["lstat_mode"] = stat.filemode(lst.st_mode)
        row["is_symlink"] = p.is_symlink()
        row["lstat_dev_ino"] = f"{lst.st_dev}:{lst.st_ino}"
    except OSError as e:
        row["lstat_error"] = f"{type(e).__name__}: {e}"
    try:
        row["is_dir"] = p.is_dir()
        row["exists"] = p.exists()
    except OSError as e:
        row["exists_error"] = f"{type(e).__name__}: {e}"
    try:
        row["resolve_strict"] = str(p.resolve(strict=True))
    except Exception as e:
        row["resolve_strict_error"] = f"{type(e).__name__}: {e}"
    try:
        row["listdir"] = sorted(os.listdir(raw))[:5]
    except OSError as e:
        row["listdir_error"] = f"{type(e).__name__}: {e}"
    try:
        row["readlink"] = os.readlink(raw)
    except OSError as e:
        row["readlink_error"] = f"{type(e).__name__}: {e}"
    print(row)
PY
