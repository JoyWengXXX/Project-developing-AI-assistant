<#
第二階段切片 1 spike：實測 spec 9.6 的 (6)(寫入部分)——受管目錄讀寫掛載（.test-managed\ → /mnt/managed）。
內容：非 root＋加固設定下的建立、rename（暫存→最終）、符號連結、大小寫、git init／commit／repack／gc，
以及主機端看到的擁有者／權限／唯讀屬性，和 Windows 端刪除容器建立的 .git 物件（D32 _clear_readonly_and_retry 的情境）。
只在 .test-managed\spike\rw 內寫入與刪除。
#>
. "$PSScriptRoot\common.ps1"
$rw = Join-Path $SpikeManaged 'rw'
Remove-TestTree $rw
New-Item -ItemType Directory -Force $SpikeManaged | Out-Null
$rwMount = "type=bind,source=$(ToDockerPath $ManagedRoot),target=/mnt/managed"

Write-Host "===== 容器內（uid 10001、read_only 根檔案系統、cap_drop ALL、no-new-privileges）"
$r = Invoke-Worker -Mount @($rwMount) -Script 'managed-rw.sh' -ScriptArgs '/mnt/managed/spike/rw'
Show-Result 'managed-rw.sh' $r

Write-Host "`n===== 主機端檢視（Windows）"
Get-ChildItem -LiteralPath $rw -Force | ForEach-Object {
    '{0,-24} {1,-34} link={2} target={3}' -f $_.Name, $_.Attributes, $_.LinkType, ($_.Target -join ',')
}
$f1 = Join-Path $rw 'file1.txt'
Write-Host "file1.txt 擁有者（Get-Acl）：$((Get-Acl -LiteralPath $f1).Owner)"
Write-Host "file1.txt 內容：$((Get-Content -LiteralPath $f1 -Raw).Trim())"
Show-Result 'icacls file1.txt' (Invoke-Native 'icacls' @($f1))
$ro = Join-Path $rw 'readonly-0444.txt'
Write-Host "readonly-0444.txt 屬性：$((Get-Item -LiteralPath $ro -Force).Attributes) IsReadOnly=$((Get-Item -LiteralPath $ro -Force).IsReadOnly)"
$packs = Get-ChildItem -LiteralPath "$rw\git-rw\.git\objects\pack" -Force -ErrorAction SilentlyContinue
$packs | ForEach-Object { Write-Host ("pack 檔 {0} 屬性={1} 大小={2}" -f $_.Name, $_.Attributes, $_.Length) }
Write-Host '主機端 Windows Git 讀容器建立的 repo：'
Show-Result 'host git log/fsck' (Invoke-Native 'git' @('-C', "$rw\git-rw", 'log', '--oneline'))
Show-Result 'host git fsck' (Invoke-Native 'git' @('-C', "$rw\git-rw", 'fsck'))
Show-Result 'host git for-each-ref' (Invoke-Native 'git' @('-C', "$rw\git-rw", 'for-each-ref'))
Write-Host '主機端對容器建立的符號連結：'
foreach ($n in 'symlink-rel', 'symlink-abs-broken') {
    $p = Join-Path $rw $n
    $item = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    if ($item) { Write-Host ("  {0}: Attributes={1} LinkType={2} Target={3} PSIsContainer={4}" -f $n, $item.Attributes, $item.LinkType, ($item.Target -join ','), $item.PSIsContainer) } else { Write-Host "  ${n}: 主機端看不到" }
}
Write-Host '大小寫：主機端 a.txt／A.txt 數量與內容：'
Get-ChildItem -LiteralPath $rw -Force | Where-Object { $_.Name -ieq 'a.txt' } | ForEach-Object { "  $($_.Name) => $((Get-Content -LiteralPath $_.FullName -Raw).Trim())" }

Write-Host "`n===== 主機建立、容器讀取（CJK 檔名）"
[IO.File]::WriteAllText((Join-Path $rw '主機建立 文件.txt'), "fromhost`n")
Show-Result 'container finds host-created file by content' (Invoke-Worker -Mount @($rwMount) -Command 'grep -r fromhost /mnt/managed/spike/rw')

Write-Host "`n===== 主機端刪除容器建立的 git repo（D32 _clear_readonly_and_retry 的情境）"
$g = "$rw\git-rw"
Write-Host '(a) Remove-Item -Recurse（不加 -Force）'
try { Remove-Item -LiteralPath $g -Recurse -ErrorAction Stop; Write-Host '  成功' } catch { Write-Host "  失敗：$($_.Exception.Message)" }
if (Test-Path -LiteralPath $g) {
    Write-Host '(b) [IO.Directory]::Delete(path, true)'
    try { [IO.Directory]::Delete($g, $true); Write-Host '  成功' } catch { Write-Host "  失敗：$($_.Exception.Message)" }
}
if (Test-Path -LiteralPath $g) {
    Write-Host '(c) 清除唯讀屬性（attrib -R /S /D）後 rmdir'
    & attrib -R "$g\*" /S /D | Out-Null
    & cmd /c rmdir /S /Q "$g" 2>&1 | Out-Null
    Write-Host "  結果：$(if (Test-Path -LiteralPath $g) { '仍存在' } else { '已刪除' })"
}
Write-Host "git-rw 是否還在：$(Test-Path -LiteralPath $g)"

Write-Host '(d) 以專案實際的 managed_repos._clear_readonly_and_retry（shutil.rmtree onexc）刪除 git-rw2（Windows Python）'
$py = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$rmPy = Join-Path $ManagedRoot '_host-rmtree.py'
[IO.File]::WriteAllText($rmPy, "import shutil, sys`nfrom repopilot.services.managed_repos import _clear_readonly_and_retry`nshutil.rmtree(sys.argv[1], onexc=_clear_readonly_and_retry)`nprint('rmtree ok')")
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
Show-Result 'rmtree git-rw2' (Invoke-Native $py @($rmPy, "$rw\git-rw2"))
Remove-Item Env:PYTHONPATH
Write-Host "git-rw2 是否還在：$(Test-Path -LiteralPath "$rw\git-rw2")"

Write-Host "`n===== 主機端刪除其餘項目（含容器建立的符號連結）"
foreach ($n in 'symlink-rel', 'symlink-abs-broken') {
    $p = Join-Path $rw $n
    try { Remove-LinkSafe $p; Write-Host "  Remove-LinkSafe $n => $(if (Test-Path -LiteralPath $p) { '仍存在' } else { '已刪除' })" } catch { Write-Host "  $n 刪除失敗：$($_.Exception.Message)" }
}
Remove-TestTree $rw
Write-Host "rw 目錄是否已清除：$(-not (Test-Path -LiteralPath $rw))"
