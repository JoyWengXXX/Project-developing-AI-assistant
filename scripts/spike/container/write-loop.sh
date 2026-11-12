#!/bin/sh
# 第二階段切片 1 spike：在讀寫掛載上持續寫入大量小檔（供「主機端在容器寫入時刪除目錄」的測試）。
# 用法：write-loop.sh <專用目錄> <檔案數>
d="$1"; n="${2:-3000}"
i=0
while [ "$i" -lt 50 ]; do mkdir -p "$d/d$i" || { echo "mkdir failed at $i"; exit 3; }; i=$((i+1)); done
i=0; errs=0
while [ "$i" -lt "$n" ]; do
  f="$d/d$((i % 50))/f$i"
  if ! { echo "$i" > "$f"; } 2>/dev/null; then
    errs=$((errs+1))
    [ "$errs" -le 3 ] && echo "write failed at i=$i: $({ echo x > "$f"; } 2>&1)"
  fi
  i=$((i+1))
done
echo "write-loop finished: attempted=$n errors=$errs"
