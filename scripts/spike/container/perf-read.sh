#!/bin/sh
# 第二階段切片 1 spike：對同一個 repo 比較「掛載上讀取」與「容器本機磁碟讀取」的耗時（只讀；本機副本寫在容器自己的 /home/worker/local）。
# 用法：perf-read.sh <掛載上的 repo 路徑> [重複次數，預設 3] [nocopy：只量掛載上的讀取，不複製到本機]
# 輸出每次的毫秒數與讀取的位元組數；不外推、不比較其他環境。
repo="$1"
n="${2:-3}"
now() { date +%s%N; }
ms() { echo $(( ($2 - $1) / 1000000 )); }

measure() { # $1=label $2=repo
  git -C "$2" ls-tree -r HEAD | awk '$2=="blob"{print $3}' > /tmp/shas.txt
  s=$(now); git -C "$2" ls-tree -r -l -z HEAD > /tmp/ls.bin; e=$(now); lsms=$(ms $s $e)
  s=$(now); bytes=$(git -C "$2" cat-file --batch < /tmp/shas.txt | wc -c); e=$(now); catms=$(ms $s $e)
  echo "$1 ls-tree_ms=$lsms cat-file-batch_ms=$catms batch_bytes=$bytes blobs=$(wc -l < /tmp/shas.txt)"
}

i=1
while [ "$i" -le "$n" ]; do measure "mounted run$i" "$repo"; i=$((i+1)); done
[ "$3" = "nocopy" ] && exit 0

mkdir -p /home/worker/local
s=$(now); cp -R "$repo" /home/worker/local/repo 2>&1 | tail -2; e=$(now)
echo "copy mounted->local_ms=$(ms $s $e)（cp -R，含 .git 全部檔案）"
i=1
while [ "$i" -le "$n" ]; do measure "local run$i" /home/worker/local/repo; i=$((i+1)); done
