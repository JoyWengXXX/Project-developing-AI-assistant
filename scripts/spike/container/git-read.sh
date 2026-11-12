#!/bin/sh
# 第二階段切片 1 spike：容器內的唯讀 Git 讀取，輸出可與主機端比對的雜湊。
# 用法：git-read.sh <repo 目錄> [額外的 git 全域參數，例如 -c safe.directory=...]
# 只讀：rev-parse、ls-tree、cat-file；不執行會寫入 .git 的指令（status、gc、fetch 等）。
repo="$1"
shift

echo "toplevel=$(git "$@" -C "$repo" rev-parse --show-toplevel 2>&1)"
echo "head=$(git "$@" -C "$repo" rev-parse HEAD 2>&1)"
echo "lstree_sha256=$(git "$@" -C "$repo" ls-tree -r -l -z HEAD | sha256sum | cut -d' ' -f1)"
echo "lstree_count=$(git "$@" -C "$repo" ls-tree -r HEAD | wc -l)"
git "$@" -C "$repo" ls-tree -r HEAD | awk '$2=="blob"{print $3}' > /tmp/shas.txt
echo "blob_count=$(wc -l < /tmp/shas.txt)"
echo "batchcheck_sha256=$(git "$@" -C "$repo" cat-file --batch-check < /tmp/shas.txt | sha256sum | cut -d' ' -f1)"
echo "batch_sha256=$(git "$@" -C "$repo" cat-file --batch < /tmp/shas.txt | sha256sum | cut -d' ' -f1)"
