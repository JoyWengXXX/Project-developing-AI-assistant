#!/bin/sh
# 第二階段切片 1 spike：非 root 使用者對掛載內容的身分、權限與寫入行為（唯讀掛載為主）。
# 用法：mount-facts.sh <掛載目標目錄> <repo 目錄>
mnt="$1"
repo="$2"
echo "id: $(id)"
echo "mount line: $(grep " $mnt " /proc/self/mountinfo | head -1)"
for p in "$mnt" "$repo" "$repo/.git" "$repo/.git/HEAD" "$repo/README.md"; do
  [ -e "$p" ] && stat -c '%u:%g %a %A %F %n' "$p" || echo "missing: $p"
done
echo "--- read test"
head -c 40 "$repo/README.md" 2>&1
echo "--- write tests (預期在 ro 掛載失敗)"
touch "$mnt/spike-write-test" 2>&1; echo "touch exit=$?"
echo x > "$repo/spike-write-test" 2>&1; echo "redirect exit=$?"
mkdir "$mnt/spike-write-dir" 2>&1; echo "mkdir exit=$?"
rm "$repo/README.md" 2>&1; echo "rm exit=$?"
( : >> "$repo/README.md" ) 2>&1; echo "append exit=$?"
mv "$repo/README.md" "$repo/README.moved" 2>&1; echo "mv exit=$?"
