<#
第二階段切片 1 spike：實測 spec 9.6 的 (6)——讀取與 clone 的耗時對照（只記錄條件與數字，不外推、不宣稱達標）。
A. 20,000 個小檔 repo：唯讀掛載上的 ls-tree＋cat-file --batch，對照同一 repo 複製到容器本機磁碟（overlay2 可寫層）。
B. 公開 repo（octocat/Hello-World、dotnet-architecture/eShopOnWeb，無 token）在容器內 git clone --no-checkout：
   讀寫掛載（加固／不加固）對容器本機磁碟，各 3 次；並含暫存→最終位置的 rename。
需要網路（B）。只在 .test-managed\spike\perf 內寫入。
注意：Docker Desktop 的掛載是 9p（drvfs）經 Windows 檔案系統，結果受主機磁碟、防毒軟體與快取影響；數字只代表這一次的條件。
#>
param([int]$Repeat = 3, [int]$LooseRepeat = 1, [switch]$SkipClone, [switch]$SkipRead, [switch]$RealRepoOnly)
. "$PSScriptRoot\common.ps1"
$roMount = "type=bind,source=$(ToDockerPath $ReposRoot),target=/mnt/repos,readonly"
$rwMount = "type=bind,source=$(ToDockerPath $ManagedRoot),target=/mnt/managed"
$perf = Join-Path $SpikeManaged 'perf'
Remove-TestTree $perf
New-Item -ItemType Directory -Force $perf | Out-Null

Write-Host "條件：主機 $((Get-CimInstance Win32_ComputerSystem).Model)；CPU 邏輯核心 $((Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors)；記憶體 $([math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)) GB；磁碟 D: $((Get-PSDrive D).Used/1GB -as [int]) GB 已用"
Write-Host "Docker：$((Invoke-Native 'docker' @('info','--format','{{.OperatingSystem}} / 核心 {{.KernelVersion}} / CPUs={{.NCPU}} / 記憶體={{.MemTotal}}')).Output)"

if (-not $SkipRead -and -not $RealRepoOnly) {
    Write-Host "`n===== A. 讀取：20,000 小檔 repo（唯讀掛載 vs 容器本機磁碟）"
    foreach ($repo in 'many-files-repo', 'many-loose-repo') {
        # loose 物件在掛載上非常慢（實測數分鐘），預設只量 1 次，避免整個腳本跑上數十分鐘
        $n = if ($repo -eq 'many-loose-repo') { $LooseRepeat } else { $Repeat }
        $r = Invoke-Worker -Mount @($roMount) -NoHardening -Script 'perf-read.sh' -ScriptArgs "/mnt/repos/spike/$repo", $n
        Show-Result "$repo（user 10001，掛載 ro，每組 $n 次）" $r
    }
    Write-Host '--- 主機端（Windows Git，同一 repo、同一時間點）做對照'
    foreach ($repo in 'many-files-repo', 'many-loose-repo') {
        $p = "$SpikeRepos\$repo"
        $blobs = & git -C $p ls-tree -r HEAD | ForEach-Object { ($_ -split '[ \t]')[2] }
        $tmpShas = Join-Path $perf 'shas.txt'
        [IO.File]::WriteAllText($tmpShas, (($blobs -join "`n") + "`n"))
        for ($i = 1; $i -le $Repeat; $i++) {
            $t1 = Measure-Command { & git -C $p ls-tree -r -l -z HEAD | Out-Null }
            $t2 = Measure-Command { & cmd /c "git -C `"$p`" cat-file --batch < `"$tmpShas`" > NUL" }
            Write-Host ("  host {0} run{1}: ls-tree_ms={2} cat-file-batch_ms={3}" -f $repo, $i, [int]$t1.TotalMilliseconds, [int]$t2.TotalMilliseconds)
        }
    }
}

if ($RealRepoOnly) {
    Write-Host "`n===== A2. 使用者真實 repo（本專案；唯讀掛載；只讀 HEAD 的 tree 與 blob，不寫入、不執行 status／gc／fetch）"
    $projName = Split-Path $ProjectRoot -Leaf
    $userMount = "type=bind,source=$(ToDockerPath (Split-Path $ProjectRoot -Parent)),target=/mnt/repos,readonly"
    Show-Result 'host git count-objects -v（唯讀）' (Invoke-Native 'git' @('-C', $ProjectRoot, 'count-objects', '-v'))
    Show-Result "real repo 掛載上讀取 ×$Repeat" (Invoke-Worker -Mount @($userMount) -NoHardening -Script 'perf-read.sh' -ScriptArgs "/mnt/repos/$projName", $Repeat, 'nocopy')
    $blobs = & git -C $ProjectRoot ls-tree -r HEAD | ForEach-Object { ($_ -split '[ \t]')[2] }
    $tmpShas = Join-Path $perf 'shas.txt'
    [IO.File]::WriteAllText($tmpShas, (($blobs -join "`n") + "`n"))
    for ($i = 1; $i -le $Repeat; $i++) {
        $t1 = Measure-Command { & git -C $ProjectRoot ls-tree -r -l -z HEAD | Out-Null }
        $t2 = Measure-Command { & cmd /c "git -C `"$ProjectRoot`" cat-file --batch < `"$tmpShas`" > NUL" }
        Write-Host ("  host real repo run{0}: ls-tree_ms={1} cat-file-batch_ms={2}" -f $i, [int]$t1.TotalMilliseconds, [int]$t2.TotalMilliseconds)
    }
}

if (-not $SkipClone -and -not $RealRepoOnly) {
    Write-Host "`n===== B. clone --no-checkout 耗時（每項 $Repeat 次；含 rename）"
    $targets = @(
        @{ Label = 'hello'; Url = 'https://github.com/octocat/Hello-World.git' },
        @{ Label = 'eshop'; Url = 'https://github.com/dotnet-architecture/eShopOnWeb.git' }
    )
    foreach ($t in $targets) {
        Write-Host "--- $($t.Label)：讀寫掛載（加固：read_only 根檔案系統＋cap_drop ALL＋no-new-privileges）"
        $keep = if ($t.Label -eq 'hello') { 'keep' } else { '' }
        $r = Invoke-Worker -Mount @($rwMount) -Script 'clone-time.sh' -ScriptArgs $t.Url, '/mnt/managed/spike/perf/hardened', $t.Label, $Repeat, $keep
        Show-Result "$($t.Label) mount hardened" $r
        Write-Host "--- $($t.Label)：讀寫掛載（不加固，只用來分離「加固」與「掛載」兩個因素）"
        $r = Invoke-Worker -Mount @($rwMount) -NoHardening -Script 'clone-time.sh' -ScriptArgs $t.Url, '/mnt/managed/spike/perf/plain', $t.Label, $Repeat
        Show-Result "$($t.Label) mount not hardened" $r
        Write-Host "--- $($t.Label)：容器本機磁碟（overlay2 可寫層，不加固）"
        $r = Invoke-Worker -NoHardening -Script 'clone-time.sh' -ScriptArgs $t.Url, '/home/worker/local-clones', $t.Label, $Repeat
        Show-Result "$($t.Label) container-local" $r
    }
    Write-Host "`n--- 主機端檢查容器 clone 到掛載的 repo（hello 的最後一次保留）"
    $kept = Get-ChildItem -LiteralPath "$perf\hardened" -Directory -Force | Where-Object { $_.Name -like 'hello-*' } | Select-Object -First 1
    if ($kept) {
        Show-Result "host git fsck $($kept.Name)" (Invoke-Native 'git' @('-C', $kept.FullName, 'fsck'))
        Show-Result 'host git for-each-ref' (Invoke-Native 'git' @('-C', $kept.FullName, 'for-each-ref'))
        Show-Result 'host git rev-parse --show-toplevel' (Invoke-Native 'git' @('-C', $kept.FullName, 'rev-parse', '--show-toplevel'))
        Show-Result 'host git config --get core.ignorecase / core.filemode' (Invoke-Native 'git' @('-C', $kept.FullName, 'config', '--local', '--list'))
    }
    else { Write-Host '找不到保留的 hello clone（clone 可能失敗，見上方輸出）' }
}
Remove-TestTree $perf
Write-Host "`n完成（perf 目錄已清除：$(-not (Test-Path -LiteralPath $perf))）"
