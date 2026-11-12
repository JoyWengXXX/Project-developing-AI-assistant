<#
第二階段切片 1 spike：清理——只刪除本 spike 建立的測試內容（.test-repos\ 與 .test-managed\ 之下）。
先移除 junction／symlink（不跟隨到目標），再清除唯讀屬性後遞迴刪除。不碰專案其他目錄、不碰任何 Docker 資源。
#>
. "$PSScriptRoot\common.ps1"
foreach ($root in $ReposRoot, $ManagedRoot) {
    if (Test-Path -LiteralPath $root) {
        Get-ChildItem -LiteralPath $root -Force | ForEach-Object { Remove-TestTree $_.FullName }
        Remove-Item -LiteralPath $root -Force
    }
}
Write-Host "已清理：.test-repos 存在=$(Test-Path $ReposRoot)；.test-managed 存在=$(Test-Path $ManagedRoot)"
