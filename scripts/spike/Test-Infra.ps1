<#
第二階段切片 1 spike：實測 spec 9.6 的 (8)(9)——依賴煙霧測試（拋棄式容器）與開發／測試堆疊隔離。
(8) 在 --rm 容器（worker 基底映像、加固設定）以 venv 安裝 tree-sitter、tree-sitter-c-sharp、celery[redis]（pip 與 uv pip），
    import 並解析一小段 C#；從 worker 容器以 redis-py PING 與 Celery broker 連線檢查（容器間網路，主機不開埠）。不改專案依賴。
(9) 以 profile 啟動／停止 redis、redis-test、worker、worker-test，驗證 db、db-test 容器 ID 與啟動時間不變、網路隔離。
安全：不使用 docker compose down、docker volume rm、docker system prune；只對本 spike 的服務執行 rm -sf。
#>
. "$PSScriptRoot\common.ps1"
$proj = 'project-developing-ai-assistant'
Set-Location $ProjectRoot
Sync-ContainerScripts

function Get-State([string]$n) {
    (Invoke-Native 'docker' @('inspect', "$proj-$n-1", '--format', '{{.Id}} started={{.State.StartedAt}} status={{.State.Status}}')).Output
}
function Show-DbState([string]$When) {
    Write-Host "[$When] db      : $(Get-State 'db')"
    Write-Host "[$When] db-test : $(Get-State 'db-test')"
    Write-Host "[$When] volume postgres_dev_data CreatedAt: $((Invoke-Native 'docker' @('volume','inspect',"${proj}_postgres_dev_data",'--format','{{.CreatedAt}}')).Output)"
}

Write-Host '===== (9) 起點：db／db-test 狀態與設定雜湊'
Show-DbState '前'
foreach ($n in 'db', 'db-test') {
    $labels = (Invoke-Native 'docker' @('inspect', "$proj-$n-1", '--format', '{{json .Config.Labels}}')).Output | ConvertFrom-Json
    $label = $labels.'com.docker.compose.config-hash'
    $now = ((Invoke-Native 'docker' @('compose', 'config', '--hash', $n)).Output -split '\s+')[1]
    Write-Host "$n config-hash 容器標籤=$label | 目前 compose 檔=$now | 相同=$($label -eq $now)"
}
Show-Result 'compose --dry-run up -d --wait db db-test（預演，不執行；確認不會 Recreate）' (Invoke-Native 'docker' @('compose', '--dry-run', 'up', '-d', '--wait', 'db', 'db-test'))

Write-Host "`n===== redis（profile jobs）"
$sw = [Diagnostics.Stopwatch]::StartNew()
Show-Result 'compose --profile jobs up -d --wait redis' (Invoke-Native 'docker' @('compose', '--profile', 'jobs', 'up', '-d', '--wait', 'redis'))
Write-Host "耗時 $([int]$sw.Elapsed.TotalSeconds) 秒"
$redis = "$proj-redis-1"
Show-Result 'redis 狀態' (Invoke-Native 'docker' @('inspect', $redis, '--format', 'status={{.State.Status}} health={{.State.Health.Status}} ports={{json .NetworkSettings.Ports}} networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}} mounts={{range .Mounts}}{{.Type}}:{{.Name}}->{{.Destination}} {{end}}'))
Show-Result 'docker port redis（應為空）' (Invoke-Native 'docker' @('port', $redis))
Write-Host "主機 127.0.0.1:6379 可連線：$(Test-NetConnection -ComputerName 127.0.0.1 -Port 6379 -InformationLevel Quiet -WarningAction SilentlyContinue)（若主機上另有程式占用 6379 則此值不代表本 redis）"
Show-Result 'redis-cli config' (Invoke-Native 'docker' @('exec', $redis, 'redis-cli', 'config', 'get', 'maxmemory-policy'))
Show-Result 'redis-cli config appendonly' (Invoke-Native 'docker' @('exec', $redis, 'redis-cli', 'config', 'get', 'appendonly'))
Show-Result 'redis-cli ping' (Invoke-Native 'docker' @('exec', $redis, 'redis-cli', 'ping'))

Write-Host "`n===== (8) 拋棄式容器煙霧測試（網路 ${proj}_jobs；venv 在容器 /tmp）"
$sw.Restart()
$r = Invoke-Worker -Network "${proj}_jobs" -Env 'PIP_NO_INPUT=1' -TmpfsOptions 'rw,exec,nosuid,nodev,size=1g' -Script 'infra-smoke.sh' -ScriptArgs 'redis://redis:6379/0'
Show-Result 'infra-smoke.sh（加固＋tmpfs /tmp 加上 exec）' $r
Write-Host "耗時 $([int]$sw.Elapsed.TotalSeconds) 秒"
Write-Host '--- 對照：加固且 /tmp 為預設 tmpfs（noexec）時，在 /tmp 建立的 venv 能否執行'
Show-Result 'default tmpfs /tmp 是否 noexec' (Invoke-Worker -Command 'grep -m1 /tmp /proc/self/mountinfo; python -m venv /tmp/v1 && /tmp/v1/bin/python --version 2>&1')

Write-Host "`n===== (9) 測試堆疊：redis-test、worker-test、worker（profile jobs-test／jobs）"
Show-Result 'compose --profile jobs-test up -d --wait redis-test worker-test' (Invoke-Native 'docker' @('compose', '--profile', 'jobs-test', 'up', '-d', '--wait', 'redis-test', 'worker-test'))
Show-Result 'compose --profile jobs up -d --wait worker' (Invoke-Native 'docker' @('compose', '--profile', 'jobs', 'up', '-d', '--wait', 'worker'))
Show-Result 'compose ps（含所有 profile）' (Invoke-Native 'docker' @('compose', '--profile', 'jobs', '--profile', 'jobs-test', 'ps', '-a', '--format', '{{.Name}} {{.Service}} {{.State}} {{.Status}}'))
foreach ($n in 'redis', 'redis-test', 'worker', 'worker-test') {
    Show-Result "inspect $n" (Invoke-Native 'docker' @('inspect', "$proj-$n-1", '--format', 'user={{.Config.User}} readonlyRootfs={{.HostConfig.ReadonlyRootfs}} capDrop={{.HostConfig.CapDrop}} secOpt={{.HostConfig.SecurityOpt}} tmpfs={{.HostConfig.Tmpfs}} networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}} mounts={{range .Mounts}}[{{.Type}} {{.Source}} -> {{.Destination}} rw={{.RW}}] {{end}}'))
}
$wt = "$proj-worker-test-1"
$w = "$proj-worker-1"
Write-Host '--- 名稱解析與連線（worker-test 應只看得到 redis-test；worker 應只看得到 redis）'
foreach ($pair in @(@($wt, 'redis-test'), @($wt, 'redis'), @($wt, 'db-test'), @($wt, 'db'), @($w, 'redis'), @($w, 'redis-test'), @($w, 'db'))) {
    $o = Invoke-Native 'docker' @('exec', $pair[0], 'sh', '/mnt/managed/_container-scripts/ping.sh', $pair[1])
    Write-Host ("  {0} -> {1}: {2}" -f ($pair[0] -replace "^$proj-", ''), $pair[1], ($o.Output -replace "`n", ' '))
}
Write-Host '--- redis-test 設定'
Show-Result 'redis-test 設定' (Invoke-Native 'docker' @('exec', "$proj-redis-test-1", 'redis-cli', 'config', 'get', 'save'))
Show-Result 'redis-test appendonly' (Invoke-Native 'docker' @('exec', "$proj-redis-test-1", 'redis-cli', 'config', 'get', 'appendonly'))
Show-Result 'redis-test maxmemory-policy' (Invoke-Native 'docker' @('exec', "$proj-redis-test-1", 'redis-cli', 'config', 'get', 'maxmemory-policy'))
Show-Result '在 worker 容器內驗證加固（NoNewPrivs／CapEff／唯讀根）' (Invoke-Native 'docker' @('exec', $w, 'sh', '-c', 'id; grep -e NoNewPrivs -e CapEff /proc/self/status; touch /x 2>&1 | head -1; touch /tmp/ok && echo tmp-writable'))

Write-Host '--- 停止並移除 redis-test、worker-test、worker（只這三個；其餘不動），然後檢查 db／db-test／redis'
Show-Result 'compose --profile jobs-test rm -sf redis-test worker-test' (Invoke-Native 'docker' @('compose', '--profile', 'jobs-test', 'rm', '-sf', 'redis-test', 'worker-test'))
Show-Result 'compose --profile jobs rm -sf worker' (Invoke-Native 'docker' @('compose', '--profile', 'jobs', 'rm', '-sf', 'worker'))
Show-DbState '後（redis-test／worker 停止後）'
Write-Host "redis 仍在執行：$((Invoke-Native 'docker' @('inspect', $redis, '--format', '{{.State.Status}}')).Output)"
Show-Result 'compose --profile jobs rm -sf redis' (Invoke-Native 'docker' @('compose', '--profile', 'jobs', 'rm', '-sf', 'redis'))
foreach ($net in "${proj}_jobs", "${proj}_jobs-test") { Show-Result "docker network rm $net（本 spike 建立的網路）" (Invoke-Native 'docker' @('network', 'rm', $net)) }
Show-DbState '最終'
Write-Host "main 專案其餘容器：$((Invoke-Native 'docker' @('ps', '-a', '--filter', "label=com.docker.compose.project=$proj", '--format', '{{.Names}}={{.Status}}')).Output -replace "`n", '; ')"
