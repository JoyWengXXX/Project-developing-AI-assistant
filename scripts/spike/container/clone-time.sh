#!/bin/sh
# 第二階段切片 1 spike：容器內以 D32 的參數形式 git clone --no-checkout（公開 repo、無 token），量測耗時，並做「暫存目錄 → 最終位置」rename。
# 用法：clone-time.sh <url> <目的地父目錄> <標籤> [重複次數，預設 3] [keep]
#   keep：最後一次 clone 保留（供主機端 fsck／刪除測試）；否則每次結束都刪除（rm -rf，記錄是否成功）
# 只在 <目的地父目錄> 內寫入。
url="$1"; parent="$2"; label="$3"; n="${4:-3}"; keep="$5"
export GIT_CONFIG_GLOBAL=/dev/null GIT_ALLOW_PROTOCOL=https GIT_TERMINAL_PROMPT=0
now() { date +%s%N; }
ms() { echo $(( ($2 - $1) / 1000000 )); }
mkdir -p "$parent" || exit 1
i=1
while [ "$i" -le "$n" ]; do
  tmp="$parent/.tmp-$label-$i"; final="$parent/$label-$i"
  rm -rf "$tmp" "$final"
  s=$(now)
  git -c credential.helper= -c core.hooksPath=/dev/null -c gc.autoDetach=false -c maintenance.autoDetach=false \
    clone --no-checkout --quiet --template= -- "$url" "$tmp" 2>&1 | tail -3
  e=$(now); clonems=$(ms $s $e)
  s=$(now); mv "$tmp" "$final"; mvcode=$?; e=$(now); mvms=$(ms $s $e)
  size=$(du -sk "$final" 2>/dev/null | cut -f1)
  files=$(find "$final" -type f 2>/dev/null | wc -l)
  head=$(git -c safe.directory='*' -C "$final" rev-parse HEAD 2>&1)
  echo "$label run$i clone_ms=$clonems rename_ms=$mvms rename_exit=$mvcode size_kb=$size files=$files head=$head"
  if [ "$i" -eq "$n" ] && [ "$keep" = "keep" ]; then :; else
    s=$(now); rm -rf "$final"; rmcode=$?; e=$(now)
    echo "$label run$i cleanup rm -rf exit=$rmcode ms=$(ms $s $e) 殘留=$([ -e "$final" ] && echo yes || echo no)"
  fi
  i=$((i+1))
done
