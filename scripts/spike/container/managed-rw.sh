#!/bin/sh
# 第二階段切片 1 spike：讀寫掛載（受管目錄）上的建立、rename、git 寫入與符號連結。
# 用法：managed-rw.sh <讀寫掛載內的專用工作目錄>（例如 /mnt/managed/spike/rw）
# 只在傳入的工作目錄內寫入。主機端的後續檢查（擁有者、唯讀屬性、刪除）由 Test-ManagedRw.ps1 執行。
base="$1"
export GIT_AUTHOR_NAME=spike GIT_AUTHOR_EMAIL=spike@example.invalid
export GIT_COMMITTER_NAME=spike GIT_COMMITTER_EMAIL=spike@example.invalid
echo "id: $(id)"
mkdir -p "$base" 2>&1 || { echo "mkdir base failed"; exit 1; }
stat -c 'base owner=%u:%g mode=%a %A' "$base"

echo "--- 建立檔案與 stat"
echo hello > "$base/file1.txt"; echo "create exit=$?"
stat -c 'file1 owner=%u:%g mode=%a %A' "$base/file1.txt"
echo ro > "$base/readonly-0444.txt"; chmod 0444 "$base/readonly-0444.txt"; echo "chmod 0444 exit=$?"
stat -c 'readonly-0444 mode=%a %A' "$base/readonly-0444.txt"

echo "--- rename：暫存目錄 → 最終位置（D32 的 clone 流程）"
mkdir -p "$base/tmp-clone/sub" && echo data > "$base/tmp-clone/sub/f.txt"
mv "$base/tmp-clone" "$base/final-clone"; echo "mv dir exit=$?"
[ -d "$base/final-clone/sub" ] && [ ! -e "$base/tmp-clone" ] && echo "rename 結果：來源消失、目的存在" || echo "rename 結果異常"
mkdir -p "$base/tmp2" && echo y > "$base/tmp2/g.txt"
mv -T "$base/tmp2" "$base/final-clone" 2>&1; echo "mv -T onto existing non-empty dir exit=$? (預期非 0)"
[ -f "$base/tmp2/g.txt" ] && echo "tmp2 仍在（未被覆蓋）"
python - "$base" <<'PY'
import os, sys
base = sys.argv[1]
os.makedirs(f"{base}/atomic-src", exist_ok=True)
open(f"{base}/atomic-src/a", "w").write("x")
try:
    os.rename(f"{base}/atomic-src", f"{base}/atomic-dst")
    print("os.rename dir: ok")
except OSError as e:
    print("os.rename dir error:", e)
open(f"{base}/replace-src", "w").write("new")
open(f"{base}/replace-dst", "w").write("old")
try:
    os.replace(f"{base}/replace-src", f"{base}/replace-dst")
    print("os.replace over existing file: ok ->", open(f"{base}/replace-dst").read())
except OSError as e:
    print("os.replace error:", e)
PY

echo "--- 大小寫：建立 a.txt 與 A.txt"
echo lower > "$base/a.txt"; echo upper > "$base/A.txt"
ls "$base" | grep -ci '^a.txt$' | sed 's/^/名稱為 a.txt（不分大小寫）的項目數：/'
echo "a.txt => $(cat "$base/a.txt")"

echo "--- 在讀寫掛載建立符號連結"
ln -s final-clone "$base/symlink-rel" 2>&1; echo "ln -s relative exit=$?"
ln -s /nonexistent/abs "$base/symlink-abs-broken" 2>&1; echo "ln -s abs exit=$?"
ls -l "$base" | grep -- '->' || echo "(無符號連結列出)"
[ -L "$base/symlink-rel" ] && echo "symlink-rel 在容器內 is link" || echo "symlink-rel 在容器內不是 link"

echo "--- git 寫入（init、commit、repack 產生 0444 物件檔與 pack）"
g="$base/git-rw"
git init -q -b main "$g" 2>&1; echo "git init exit=$?"
echo c1 > "$g/f.txt"; git -C "$g" add f.txt 2>&1; git -C "$g" commit -q -m c1 2>&1; echo "commit exit=$?"
echo c2 >> "$g/f.txt"; git -C "$g" commit -q -am c2 2>&1
git -C "$g" repack -a -d -q 2>&1; echo "repack exit=$?"
git -C "$g" fsck 2>&1 | head -3; echo "fsck exit=$?"
ls -l "$g/.git/objects/pack" | head -5
stat -c 'pack mode=%a %A %n' "$g"/.git/objects/pack/*.pack
git -C "$g" gc --quiet 2>&1; echo "gc exit=$?"
# 第二個一模一樣的 repo，供主機端以專案的 _clear_readonly_and_retry 刪除
g2="$base/git-rw2"
git init -q -b main "$g2" && echo c1 > "$g2/f.txt" && git -C "$g2" add f.txt && git -C "$g2" commit -q -m c1 && git -C "$g2" repack -a -d -q
echo "git-rw2 created exit=$?"
