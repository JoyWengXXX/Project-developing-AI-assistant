<#
第二階段切片 1 spike：建立測試 fixture（可重跑；每次先清掉 .test-repos\spike 再重建）。
用途：為「Windows 路徑↔Linux 容器掛載」實測提供假 repo。不含秘密；只寫入專案內的 .test-repos\ 與 .test-managed\。
建立內容（.test-repos\spike\）：
  ascii-repo\            一般 repo（3 個 commit、一個 tag）
  repo 空白 中文\        目錄與檔案名稱含空白與 CJK 的 repo
  case-repo\             Git 物件內有只差大小寫的路徑（a.txt/A.txt、Dir/f.txt、dir/f.txt）；沒有檢出工作目錄
  casedir\               一般目錄（非 repo），用於 NTFS 大小寫語意測試
  many-files-repo\       20,000 個小檔（pack 物件，fast-import 建立）
  many-loose-repo\       與上同內容，但物件為 loose（unpack-objects）
  links\                 junction（有效、指向掛載外、壞掉）與符號連結（若權限允許；否則記錄無法建立）
  roots 與 .test-managed\spike\outside-target：junction 的目標
#>
param([switch]$SkipMany)

. "$PSScriptRoot\common.ps1"

function Invoke-Git { & git.exe @args 2>&1 | ForEach-Object { "$_" } | Out-Null; if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') 失敗（exit $LASTEXITCODE）" } }

Remove-TestTree $SpikeRepos
Remove-TestTree $SpikeManaged
New-Item -ItemType Directory -Force $SpikeRepos, $SpikeManaged | Out-Null

function Write-Utf8([string]$Path, [string]$Text) {
    New-Item -ItemType Directory -Force (Split-Path $Path) | Out-Null
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

# --- 1. ascii-repo ---
$ascii = Join-Path $SpikeRepos 'ascii-repo'
New-Item -ItemType Directory $ascii | Out-Null
Invoke-Git -C $ascii init -q -b main
Invoke-Git -C $ascii config core.autocrlf false
Write-Utf8 "$ascii\README.md" "# ascii repo`n"
Write-Utf8 "$ascii\src\a.txt" "alpha`n"
Invoke-Git -C $ascii add -A; Invoke-Git -C $ascii commit -q -m 'c1'
Write-Utf8 "$ascii\src\b.txt" "bravo`n"
Invoke-Git -C $ascii add -A; Invoke-Git -C $ascii commit -q -m 'c2'
Write-Utf8 "$ascii\src\a.txt" "alpha v2`n"
Invoke-Git -C $ascii add -A; Invoke-Git -C $ascii commit -q -m 'c3'
Invoke-Git -C $ascii tag v1

# --- 2. 空白與 CJK ---
$cjk = Join-Path $SpikeRepos 'repo 空白 中文'
New-Item -ItemType Directory $cjk | Out-Null
Invoke-Git -C $cjk init -q -b main
Invoke-Git -C $cjk config core.autocrlf false
Invoke-Git -C $cjk config core.quotepath false
Write-Utf8 "$cjk\文件 a.txt" "內容 一`n"
Write-Utf8 "$cjk\子 目錄\檔案.txt" "內容 二 with space`n"
Write-Utf8 "$cjk\dir with space\x y.txt" "x`n"
Invoke-Git -C $cjk add -A; Invoke-Git -C $cjk commit -q -m 'cjk'

# --- 3. 只差大小寫的路徑（直接寫 Git 物件，不檢出）---
$case = Join-Path $SpikeRepos 'case-repo'
New-Item -ItemType Directory $case | Out-Null
Invoke-Git -C $case init -q -b main
function HashObj([string]$Repo, [string]$Text) {
    $tmp = Join-Path $Repo 'blob.tmp'
    [IO.File]::WriteAllText($tmp, $Text, [Text.UTF8Encoding]::new($false))
    $h = (& git -C $Repo hash-object -w --no-filters $tmp).Trim()
    Remove-Item -LiteralPath $tmp
    return $h
}
$ha = HashObj $case "lower a`n"; $hA = HashObj $case "UPPER A`n"
$hf1 = HashObj $case "Dir f`n"; $hf2 = HashObj $case "dir f`n"
function MkTree([string]$Repo, [string[]]$Lines) {
    $tmp = Join-Path $Repo 'tree.tmp'
    [IO.File]::WriteAllText($tmp, (($Lines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
    $t = (& cmd /c "git -C `"$Repo`" mktree < `"$tmp`"").Trim()
    Remove-Item -LiteralPath $tmp
    return $t
}
$tDir = MkTree $case @("100644 blob $hf1`tf.txt")
$tdir = MkTree $case @("100644 blob $hf2`tf.txt")
$root = MkTree $case @("100644 blob $hA`tA.txt", "040000 tree $tDir`tDir", "100644 blob $ha`ta.txt", "040000 tree $tdir`tdir")
$commit = (& git -C $case commit-tree $root -m 'case').Trim()
Invoke-Git -C $case update-ref refs/heads/main $commit

# --- 4. casedir（NTFS 大小寫語意）---
Write-Utf8 "$SpikeRepos\casedir\Hello.txt" "hello`n"
Write-Utf8 "$SpikeRepos\casedir\Sub\Inner.txt" "inner`n"

# --- 5. 20,000 個小檔 ---
if (-not $SkipMany) {
    $many = Join-Path $SpikeRepos 'many-files-repo'
    New-Item -ItemType Directory $many | Out-Null
    Invoke-Git -C $many init -q -b main
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    $psi.Arguments = "-C `"$many`" fast-import --quiet"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $proc = [Diagnostics.Process]::Start($psi)
    $stream = $proc.StandardInput.BaseStream
    $enc = [Text.UTF8Encoding]::new($false)
    function W([string]$t) { $b = $enc.GetBytes($t); $stream.Write($b, 0, $b.Length) }
    $msg = "many files`n"
    W "commit refs/heads/main`ncommitter spike <spike@example.invalid> 1767225600 +0000`ndata $($enc.GetByteCount($msg))`n$msg"
    for ($i = 0; $i -lt 20000; $i++) {
        $d = '{0:D3}' -f ($i % 200)
        $content = "file $i in dir $d`n" + ('x' * (80 + ($i % 120))) + "`n"
        W "M 100644 inline pkg$d/file$i.txt`ndata $($enc.GetByteCount($content))`n$content`n"
    }
    $stream.Close(); $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) { throw "fast-import 失敗（$($proc.ExitCode)）" }

    # loose 版本：把 pack 內容解開到另一個 repo
    $loose = Join-Path $SpikeRepos 'many-loose-repo'
    New-Item -ItemType Directory $loose | Out-Null
    Invoke-Git -C $loose init -q -b main
    $packTmp = Join-Path $SpikeManaged 'many.pack'
    & cmd /c "git -C `"$many`" pack-objects --all --stdout < NUL > `"$packTmp`"" 2>&1 | Out-Null
    & cmd /c "git -C `"$loose`" unpack-objects -q < `"$packTmp`"" 2>&1 | Out-Null
    Remove-Item -LiteralPath $packTmp
    $sha = (& git -C $many rev-parse main).Trim()
    Invoke-Git -C $loose update-ref refs/heads/main $sha
}

# --- 6. junction 與符號連結 ---
$links = Join-Path $SpikeRepos 'links'
New-Item -ItemType Directory $links | Out-Null
$outside = Join-Path $SpikeManaged 'outside-target'
New-Item -ItemType Directory $outside | Out-Null
Write-Utf8 "$outside\secret.txt" "outside the repository root`n"
New-Item -ItemType Junction -Path "$links\junction-to-ascii" -Target $ascii | Out-Null
New-Item -ItemType Junction -Path "$links\junction-outside" -Target $outside | Out-Null
$gone = Join-Path $SpikeManaged 'will-be-removed'
New-Item -ItemType Directory $gone | Out-Null
New-Item -ItemType Junction -Path "$links\junction-broken" -Target $gone | Out-Null
[IO.Directory]::Delete($gone)

$symlinkStatus = @()
# mklink 可接受相對目標（New-Item 會以目前目錄解析）；沒有權限（需系統管理員或開發人員模式）時只記錄
foreach ($spec in @(
        @{ Name = 'symlink-to-ascii'; Target = $ascii; Dir = $true },
        @{ Name = 'symlink-relative'; Target = '..\ascii-repo'; Dir = $true },
        @{ Name = 'symlink-to-file'; Target = "$ascii\README.md"; Dir = $false },
        @{ Name = 'symlink-broken'; Target = "$SpikeManaged\no-such-target"; Dir = $true })) {
    $flag = if ($spec.Dir) { '/D' } else { '' }
    $out = & cmd /c "mklink $flag `"$links\$($spec.Name)`" `"$($spec.Target)`"" 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -eq 0) { $symlinkStatus += "$($spec.Name): 已建立" }
    else { $symlinkStatus += "$($spec.Name): 無法建立（$($out -join ' ')）" }
}

Write-Host '--- fixture 摘要'
Get-ChildItem -LiteralPath $SpikeRepos -Force | ForEach-Object { '{0,-24} {1}' -f $_.Name, $_.Attributes }
Get-ChildItem -LiteralPath $links -Force | ForEach-Object { '{0,-24} {1} -> {2}' -f $_.Name, $_.Attributes, ($_.Target -join ',') }
$symlinkStatus | ForEach-Object { Write-Host $_ }
foreach ($r in 'ascii-repo', 'repo 空白 中文', 'case-repo', 'many-files-repo', 'many-loose-repo') {
    $p = Join-Path $SpikeRepos $r
    if (Test-Path $p) { '{0,-20} HEAD={1} files={2}' -f $r, (& git -C $p rev-parse HEAD), (& git -C $p ls-tree -r HEAD --name-only | Measure-Object).Count }
}
