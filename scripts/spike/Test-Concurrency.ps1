<#
第二階段切片 1 spike：實測 spec 9.6 的 (7)——跨作業系統並行（Windows Git 讀取／fetch 與容器內 Linux Git 對同一個受管 clone）。
使用真正的 compose 服務 worker-test（profile jobs-test；加固設定），不對任何遠端網路寫入：fetch 的來源是 .test-managed 內的本機 bare repo。
場景：
  A. 主機持續讀取（for-each-ref／log／rev-list／cat-file --batch-all-objects）同時，容器 fetch；中途容器 pack-refs --all
  B. 主機 fetch 與容器 fetch 同時對同一個 clone 執行
  C. 容器持續寫入時，主機刪除該目錄
只在 .test-managed\spike\conc 內寫入與刪除；結束時停止並移除 worker-test（不使用 compose down）。
#>
param([int]$Iterations = 30, [int]$ContendedIterations = 15)
. "$PSScriptRoot\common.ps1"
$conc = Join-Path $SpikeManaged 'conc'
Remove-TestTree $conc
New-Item -ItemType Directory -Force $conc | Out-Null
Sync-ContainerScripts
$origin = Join-Path $conc 'origin.git'
$work = Join-Path $conc 'work'
$clone = Join-Path $conc 'clone'
$cOrigin = 'file:///mnt/managed/spike/conc/origin.git'
$cClone = '/mnt/managed/spike/conc/clone'

function Invoke-GitHost { param([string[]]$GitArgs) Invoke-Native 'git' $GitArgs }

Write-Host '===== 準備：host 端建立 bare origin 與 work repo，worker-test 以 compose 啟動'
$null = Invoke-GitHost @('init', '-q', '--bare', '-b', 'main', $origin)
$null = Invoke-GitHost @('init', '-q', '-b', 'main', $work)
[IO.File]::WriteAllText("$work\f0.txt", "0`n")
$null = Invoke-GitHost @('-C', $work, 'add', '-A'); $null = Invoke-GitHost @('-C', $work, 'commit', '-q', '-m', 'c0')
$null = Invoke-GitHost @('-C', $work, 'push', '-q', $origin, 'main')
Show-Result 'compose up worker-test' (Invoke-Native 'docker' @('compose', '--profile', 'jobs-test', 'up', '-d', '--wait', 'worker-test'))
$cid = (Invoke-Native 'docker' @('compose', '--profile', 'jobs-test', 'ps', '-q', 'worker-test')).Output.Trim()
Write-Host "worker-test container id: $cid"
function Exec([string[]]$A, [string[]]$Env = @('GIT_CONFIG_GLOBAL=/dev/null')) {
    $dargs = @('exec'); foreach ($e in $Env) { $dargs += @('-e', $e) }; $dargs += $cid; $dargs += $A
    Invoke-Native 'docker' $dargs
}
Show-Result 'container: 身分與唯讀根檔案系統' (Exec @('sh', '-c', 'id; touch /x 2>&1 | head -1; grep -e NoNewPrivs -e CapEff /proc/self/status'))
Show-Result 'container: clone（file://，--no-checkout）' (Exec @('git', 'clone', '--no-checkout', '--quiet', '--template=', $cOrigin, $cClone))

function Add-HostCommit([int]$i) {
    [IO.File]::WriteAllText("$work\f$i.txt", "$i`n" + ('y' * 200) + "`n")
    $null = Invoke-GitHost @('-C', $work, 'add', '-A'); $null = Invoke-GitHost @('-C', $work, 'commit', '-q', '-m', "c$i")
    $null = Invoke-GitHost @('-C', $work, 'push', '-q', $origin, 'main')
}

# ---- 場景 A ----
Write-Host "`n===== 場景 A：host 持續讀取 + 容器 fetch（$Iterations 次；第 10、20 次前容器 pack-refs --all）"
$stopFile = Join-Path $conc 'stop-reader'
$readerLog = Join-Path $conc 'reader.log'
$reader = Start-Job -ArgumentList $clone, $stopFile, $readerLog -ScriptBlock {
    param($clone, $stopFile, $log)
    $runs = 0; $fails = 0
    $cmds = @(@('for-each-ref'), @('log', '--all', '--oneline'), @('rev-list', '--all', '--count'), @('cat-file', '--batch-check', '--batch-all-objects'), @('rev-parse', 'HEAD', 'refs/remotes/origin/main'))
    while (-not (Test-Path -LiteralPath $stopFile)) {
        foreach ($a in $cmds) {
            $out = & git -C $clone @a 2>&1 | ForEach-Object { "$_" }
            $runs++
            if ($LASTEXITCODE -ne 0) { $fails++; Add-Content -LiteralPath $log -Value ("FAIL git {0} :: {1}" -f ($a -join ' '), ($out -join ' ')) }
        }
    }
    "runs=$runs fails=$fails"
}
$fetchFail = 0; $fetchOut = @()
for ($i = 1; $i -le $Iterations; $i++) {
    Add-HostCommit $i
    if ($i -eq 10 -or $i -eq 20) { $pr = Exec @('git', '-C', $cClone, 'pack-refs', '--all'); Write-Host "  [i=$i] 容器 pack-refs --all exit=$($pr.ExitCode) $($pr.Output)" }
    $f = Exec @('git', '-C', $cClone, 'fetch', '--quiet', 'origin')
    if ($f.ExitCode -ne 0) { $fetchFail++; $fetchOut += "i=$i exit=$($f.ExitCode) :: $($f.Output)" }
}
New-Item -ItemType File $stopFile | Out-Null
$null = Wait-Job $reader -Timeout 60
Write-Host "host reader：$(Receive-Job $reader)"
Remove-Job $reader -Force
Write-Host "容器 fetch 失敗次數：$fetchFail / $Iterations"
$fetchOut | ForEach-Object { Write-Host "  $_" }
if (Test-Path $readerLog) { Write-Host '主機讀取失敗紀錄（前 5 行）：'; Get-Content $readerLog -Encoding UTF8 | Select-Object -First 5 | ForEach-Object { "  $_" } } else { Write-Host '主機讀取：無失敗紀錄' }
$hostRefs = (Invoke-GitHost @('-C', $clone, 'for-each-ref', '--format=%(refname) %(objectname)')).Output
$contRefs = (Exec @('git', '-C', $cClone, 'for-each-ref', '--format=%(refname) %(objectname)')).Output
Write-Host "for-each-ref 主機與容器一致：$($hostRefs -eq $contRefs)；origin/main=$((Invoke-GitHost @('-C', $clone, 'rev-parse', 'refs/remotes/origin/main')).Output) 預期=$((Invoke-GitHost @('-C', $work, 'rev-parse', 'HEAD')).Output)"
Write-Host "packed-refs 存在：$(Test-Path (Join-Path $clone '.git\packed-refs'))"
Show-Result '主機 git fsck --full' (Invoke-GitHost @('-C', $clone, 'fsck', '--full'))
Show-Result '容器 git fsck --full' (Exec @('git', '-C', $cClone, 'fsck', '--full'))
Show-Result '容器 git count-objects -v' (Exec @('git', '-C', $cClone, 'count-objects', '-v'))
Get-ChildItem -LiteralPath $clone -Recurse -Force -Filter '*.lock' -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "殘留鎖檔：$($_.FullName)" }

# ---- 場景 B ----
Write-Host "`n===== 場景 B：主機 fetch 與容器 fetch 同時對同一個 clone（$ContendedIterations 次）"
$hostFail = 0; $contFail = 0; $msgs = @()
for ($k = 1; $k -le $ContendedIterations; $k++) {
    Add-HostCommit (1000 + $k)
    $job = Start-Job -ArgumentList $cid -ScriptBlock {
        param($cid)
        $o = & docker exec -e GIT_CONFIG_GLOBAL=/dev/null $cid git -C /mnt/managed/spike/conc/clone fetch --quiet origin 2>&1 | ForEach-Object { "$_" }
        "exit=$LASTEXITCODE :: $($o -join ' ')"
    }
    $h = Invoke-GitHost @('-C', $clone, 'fetch', '--quiet', $origin, '+refs/heads/*:refs/remotes/origin/*')
    $null = Wait-Job $job -Timeout 120
    $c = Receive-Job $job; Remove-Job $job -Force
    if ($h.ExitCode -ne 0) { $hostFail++; $msgs += "k=$k host exit=$($h.ExitCode) :: $($h.Output)" }
    if ($c -notmatch '^exit=0') { $contFail++; $msgs += "k=$k container $c" }
}
Write-Host "同時 fetch：主機失敗 $hostFail / $ContendedIterations，容器失敗 $contFail / $ContendedIterations"
$msgs | Select-Object -First 8 | ForEach-Object { Write-Host "  $_" }
Show-Result '主機 git fsck --full（場景 B 後）' (Invoke-GitHost @('-C', $clone, 'fsck', '--full'))
Show-Result '容器 git fsck --full（場景 B 後）' (Exec @('git', '-C', $cClone, 'fsck', '--full'))
Write-Host "origin/main 是否為最新：$((Invoke-GitHost @('-C', $clone, 'rev-parse', 'refs/remotes/origin/main')).Output -eq (Invoke-GitHost @('-C', $work, 'rev-parse', 'HEAD')).Output)"
Get-ChildItem -LiteralPath $clone -Recurse -Force -Filter '*.lock' -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "殘留鎖檔：$($_.FullName)" }

# ---- 場景 C ----
Write-Host "`n===== 場景 C：容器持續寫入時，主機刪除該目錄（只刪 .test-managed\spike\conc\delme）"
$delme = Join-Path $conc 'delme'
# worker-test 沒有掛載 /spike；腳本副本在受管目錄的 _container-scripts（同步時已轉成 LF）
$writerJob = Start-Job -ArgumentList $cid -ScriptBlock {
    param($cid)
    $o = & docker exec $cid sh /mnt/managed/_container-scripts/write-loop.sh /mnt/managed/spike/conc/delme 6000 2>&1 | ForEach-Object { "$_" }
    "exit=$LASTEXITCODE :: $($o -join ' | ')"
}
Start-Sleep -Milliseconds 1500
$sw = [Diagnostics.Stopwatch]::StartNew()
Write-Host "寫入中時 delme 是否存在：$(Test-Path -LiteralPath $delme)；目前檔案數約 $((Get-ChildItem -LiteralPath $delme -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object).Count)"
$py = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$rmPy = Join-Path $ManagedRoot '_host-rmtree.py'
[IO.File]::WriteAllText($rmPy, "import shutil, sys`nfrom repopilot.services.managed_repos import _clear_readonly_and_retry`ntry:`n    shutil.rmtree(sys.argv[1], onexc=_clear_readonly_and_retry)`n    print('rmtree ok')`nexcept OSError as e:`n    print('rmtree OSError', type(e).__name__, e)")
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
$del = Invoke-Native $py @($rmPy, $delme)
Remove-Item Env:PYTHONPATH
Write-Host "主機刪除（專案的 shutil.rmtree＋_clear_readonly_and_retry）：$($del.Output)；耗時 $([int]$sw.ElapsedMilliseconds) ms"
$null = Wait-Job $writerJob -Timeout 180
Write-Host "容器寫入程序結果：$(Receive-Job $writerJob)"
Remove-Job $writerJob -Force
$leftFiles = (Get-ChildItem -LiteralPath $delme -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Host "容器寫完後 delme 是否存在：$(Test-Path -LiteralPath $delme)；殘留檔案數：$leftFiles"
$del2 = Invoke-Native $py @($rmPy, $delme)
Write-Host "容器停止寫入後再刪一次：$($del2.Output)；是否存在：$(Test-Path -LiteralPath $delme)"

# ---- 清理 ----
Write-Host "`n===== 清理"
Show-Result 'compose rm -sf worker-test（只停止並移除此服務）' (Invoke-Native 'docker' @('compose', '--profile', 'jobs-test', 'rm', '-sf', 'worker-test'))
Show-Result 'docker network rm jobs-test（本 spike 建立的空網路）' (Invoke-Native 'docker' @('network', 'rm', 'project-developing-ai-assistant_jobs-test'))
Remove-TestTree $conc
Write-Host "conc 是否已清除：$(-not (Test-Path -LiteralPath $conc))"
