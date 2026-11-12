<#
第二階段切片 1 spike：實測 spec 9.6 的 (1)～(5)——Windows 主機路徑以唯讀方式掛進 Linux 容器並讓 Git 讀取。
前置：docker compose --profile jobs build worker；.\New-SpikeFixtures.ps1
只讀為主：寫入測試只針對 .test-repos\ 與 .test-managed\ 內的 fixture。
對使用者真實 repo（本專案）只做 rev-parse／ls-tree／cat-file，且以唯讀掛載；不寫入、不跑 status／gc／fetch。
輸出為逐項觀察；通過／失敗的判定由閱讀者依輸出（與 docs/roadmap.md 的紀錄）決定。
#>
. "$PSScriptRoot\common.ps1"
$src = ToDockerPath $ReposRoot
$roMount = "type=bind,source=$src,target=/mnt/repos,readonly"
$pyHost = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

Write-Host "===== 環境"
Show-Result 'docker version' (Invoke-Native 'docker' @('version', '--format', 'server={{.Server.Version}} client={{.Client.Version}}'))
Show-Result 'compose version' (Invoke-Native 'docker' @('compose', 'version'))
Show-Result 'image' (Invoke-Native 'docker' @('image', 'inspect', $WorkerImage, '--format', '{{.Id}} {{.Os}}/{{.Architecture}}'))
Show-Result 'host git' (Invoke-Native 'git' @('--version'))

Write-Host "`n===== (1a) Compose bind mount 來源路徑寫法（docker compose run，ro）"
$formsDir = Join-Path $ManagedRoot '_compose-forms'
New-Item -ItemType Directory -Force $formsDir | Out-Null
$rootsDir = Join-Path $ReposRoot 'spike-roots'
$weirdSrc = Join-Path $rootsDir '根 目錄 with space'
New-Item -ItemType Directory -Force $weirdSrc | Out-Null
[IO.File]::WriteAllText((Join-Path $weirdSrc 'marker.txt'), "marker`n")
$fwd = ToDockerPath $ReposRoot
$bs = $ReposRoot
$bs2 = $ReposRoot.Replace('\', '\\')
$lower = $fwd.Substring(0, 1).ToLower() + $fwd.Substring(1)
$driveLetter = $ReposRoot.Substring(0, 1).ToLower()
$wslStyle = '/' + $driveLetter + $fwd.Substring(2)
$forms = [ordered]@{
    'long-yaml-plain-forward'          = "      - type: bind`n        source: $fwd`n        target: /mnt/repos`n        read_only: true"
    'long-yaml-doublequoted-2backslash' = "      - type: bind`n        source: `"$bs2`"`n        target: /mnt/repos`n        read_only: true"
    'long-yaml-singlequoted-1backslash' = "      - type: bind`n        source: '$bs'`n        target: /mnt/repos`n        read_only: true"
    'long-yaml-lowercase-drive'        = "      - type: bind`n        source: $lower`n        target: /mnt/repos`n        read_only: true"
    'long-yaml-wsl-style-/d/...'       = "      - type: bind`n        source: $wslStyle`n        target: /mnt/repos`n        read_only: true"
    'long-relative-./.test-repos'      = "      - type: bind`n        source: ./../../.test-repos`n        target: /mnt/repos`n        read_only: true"
    'short-forward-ro'                 = "      - '${fwd}:/mnt/repos:ro'"
    'short-backslash-ro'               = "      - '${bs}:/mnt/repos:ro'"
    'long-space-and-CJK-source'        = "      - type: bind`n        source: '$(ToDockerPath $weirdSrc)'`n        target: /mnt/repos`n        read_only: true"
}
foreach ($name in $forms.Keys) {
    $safe = ($name -replace '[^A-Za-z0-9._-]', '_')
    $file = Join-Path $formsDir "$safe.yml"
    $yaml = "services:`n  probe:`n    image: $WorkerImage`n    entrypoint: [sh, -c]`n    command: ['ls /mnt/repos | head -5; touch /mnt/repos/w 2>&1; echo touch_exit=`$`$?']`n    volumes:`n$($forms[$name])`n"
    [IO.File]::WriteAllText($file, $yaml, [Text.UTF8Encoding]::new($false))
    $r = Invoke-Native 'docker' @('compose', '-f', $file, '-p', 'spikeforms', 'run', '--rm', '--no-deps', '-T', 'probe')
    $first = ($r.Output -split "`n" | Select-Object -First 6) -join ' | '
    Write-Host ("[{0}] exit={1} :: {2}" -f $name, $r.ExitCode, $first)
}
# 環境變數插值（docker-compose.yml 的 ${WORKER_REPOSITORIES_HOST_DIR} 寫法）
$envYaml = Join-Path $formsDir 'env-interpolation.yml'
[IO.File]::WriteAllText($envYaml, "services:`n  probe:`n    image: $WorkerImage`n    entrypoint: [sh, -c]`n    command: ['ls /mnt/repos | head -3']`n    volumes:`n      - type: bind`n        source: `${SRC}`n        target: /mnt/repos`n        read_only: true`n", [Text.UTF8Encoding]::new($false))
foreach ($v in @($fwd, $bs)) {
    $env:SRC = $v
    $r = Invoke-Native 'docker' @('compose', '-f', $envYaml, '-p', 'spikeforms', 'run', '--rm', '--no-deps', '-T', 'probe')
    Write-Host ("[env SRC={0}] exit={1} :: {2}" -f $v, $r.ExitCode, (($r.Output -split "`n") -join ' | '))
}
Remove-Item Env:SRC
Invoke-Native 'docker' @('network', 'rm', 'spikeforms_default') | Out-Null

Write-Host "`n===== (1b)(2) 唯讀、身分與權限（fixture：ascii-repo；非 root、加固設定）"
Show-Result 'mount-facts' (Invoke-Worker -Mount @($roMount) -Script 'mount-facts.sh' -ScriptArgs '/mnt/repos', '/mnt/repos/spike/ascii-repo')
Show-Result 'mount-facts (root, 無加固)' (Invoke-Worker -Mount @($roMount) -User '0:0' -NoHardening -Command 'id; touch /mnt/repos/x 2>&1; echo touch_exit=$?')

Write-Host "`n===== (3) Git 讀取：fixture（與主機端雜湊比對）"
$null = Compare-GitRead -Title 'ascii-repo' -HostRepo "$SpikeRepos\ascii-repo" -ContainerRepo '/mnt/repos/spike/ascii-repo' -Mount @($roMount)
$null = Compare-GitRead -Title 'repo 空白 中文' -HostRepo "$SpikeRepos\repo 空白 中文" -ContainerRepo '/mnt/repos/spike/repo 空白 中文' -Mount @($roMount)
$null = Compare-GitRead -Title 'case-repo' -HostRepo "$SpikeRepos\case-repo" -ContainerRepo '/mnt/repos/spike/case-repo' -Mount @($roMount)

Write-Host "`n===== (3) Git 讀取：使用者真實 repo（本專案；D:\ProgramingProjects 整個唯讀掛載到 /mnt/repos）"
$userRoot = Split-Path $ProjectRoot -Parent
$userMount = "type=bind,source=$(ToDockerPath $userRoot),target=/mnt/repos,readonly"
$projName = Split-Path $ProjectRoot -Leaf
$null = Compare-GitRead -Title "real repo $projName" -HostRepo $ProjectRoot -ContainerRepo "/mnt/repos/$projName" -Mount @($userMount)
Show-Result 'real repo: 寫入應失敗（只對 /mnt/repos 根目錄嘗試 mkdir，不碰 repo 內檔案）' (Invoke-Worker -Mount @($userMount) -Command 'mkdir /mnt/repos/spike-should-fail 2>&1; echo mkdir_exit=$?')

Write-Host "`n===== (3) dubious ownership（fixture：ascii-repo）"
$ascii = '/mnt/repos/spike/ascii-repo'
$cases = @(
    @{ T = '預設（系統層 /etc/gitconfig 的 safe.directory /mnt/repos/*）'; Env = @(); Opt = @() },
    @{ T = '不讀系統層（GIT_CONFIG_NOSYSTEM=1），無其他設定'; Env = @('GIT_CONFIG_NOSYSTEM=1'); Opt = @() },
    @{ T = '不讀系統層 + 命令列 -c safe.directory=<該 repo>'; Env = @('GIT_CONFIG_NOSYSTEM=1'); Opt = @('-c', "safe.directory=$ascii") },
    @{ T = '不讀系統層 + 命令列 -c safe.directory=/mnt/repos/*'; Env = @('GIT_CONFIG_NOSYSTEM=1'); Opt = @('-c', 'safe.directory=/mnt/repos/*') },
    @{ T = '不讀系統層 + 環境變數 GIT_CONFIG_COUNT 形式（D32 的 token 傳遞方式）'; Env = @('GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_COUNT=1', 'GIT_CONFIG_KEY_0=safe.directory', "GIT_CONFIG_VALUE_0=$ascii"); Opt = @() },
    @{ T = '不讀系統層 + GIT_CONFIG_GLOBAL=/dev/null（D32 網路指令的作法）'; Env = @('GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_GLOBAL=/dev/null'); Opt = @() },
    @{ T = '系統層保留 + GIT_CONFIG_GLOBAL=/dev/null（D32 網路指令實際情況）'; Env = @('GIT_CONFIG_GLOBAL=/dev/null'); Opt = @() },
    @{ T = '以 GIT_CONFIG_SYSTEM 指向無 safe.directory 的空檔，再用 -c safe.directory=*'; Env = @('GIT_CONFIG_SYSTEM=/dev/null'); Opt = @('-c', 'safe.directory=*') }
)
foreach ($c in $cases) {
    $optText = ($c.Opt | ForEach-Object { if ($_ -eq '-c') { '-c' } else { "'$_'" } }) -join ' '
    $r = Invoke-Worker -Mount @($roMount) -Env $c.Env -Command ("git $optText -C $ascii rev-parse HEAD 2>&1 | head -3")
    Write-Host ("[{0}] => {1}" -f $c.T, (($r.Output -split "`n" | Select-Object -First 2) -join ' | '))
}
$r = Invoke-Worker -Mount @($roMount) -Command 'git -C /mnt/repos/spike/links/junction-to-ascii rev-parse HEAD 2>&1 | head -2'
Write-Host "[在 junction 路徑上的 git] => $($r.Output)"
$r = Invoke-Worker -Mount @($roMount) -Command 'git -C /mnt/repos/spike/ascii-repo/src rev-parse --show-toplevel 2>&1'
Write-Host "[子目錄 --show-toplevel] => $($r.Output)"
$r = Invoke-Worker -Mount @($roMount) -Command 'git -C /mnt/repos/spike rev-parse --show-toplevel 2>&1'
Write-Host "[非 repo 目錄 --show-toplevel] => $($r.Output)"

Write-Host "`n===== (4) junction／symlink：容器內看到什麼"
$probe = @('/mnt/repos/spike/links/junction-to-ascii', '/mnt/repos/spike/links/junction-outside', '/mnt/repos/spike/links/junction-broken', '/mnt/repos/spike/ascii-repo', '/mnt/repos/spike/links/symlink-to-ascii')
Show-Result 'ls -l links' (Invoke-Worker -Mount @($roMount) -Command 'ls -l /mnt/repos/spike/links; stat -c %F:%N /mnt/repos/spike/links/* 2>&1')
Show-Result 'links-probe（容器端 resolve／is_symlink／readlink）' (Invoke-Worker -Mount @($roMount) -Script 'links-probe.sh' -ScriptArgs $probe)
Show-Result '容器內 /mnt/host 是否存在' (Invoke-Worker -Mount @($roMount) -Command 'ls -d /mnt/host 2>&1; ls -d /mnt/host/d 2>&1')
Write-Host '--- 主機端 Path.resolve(strict=True)（Windows Python）'
foreach ($p in @("$SpikeRepos\links\junction-to-ascii", "$SpikeRepos\links\junction-outside", "$SpikeRepos\links\junction-broken", "$SpikeRepos\ascii-repo")) {
    $hostPy = "import sys; from pathlib import Path; p=Path(sys.argv[1]); print('is_symlink=',p.is_symlink(),'| is_junction=',p.is_junction() if hasattr(p,'is_junction') else 'n/a','|', end=' ')`ntry: print('resolved=',p.resolve(strict=True))`nexcept Exception as e: print(type(e).__name__, e)"
    $tmpPy = Join-Path $ManagedRoot '_host-resolve.py'
    [IO.File]::WriteAllText($tmpPy, $hostPy)
    $r = Invoke-Native $pyHost @($tmpPy, $p)
    Write-Host ("  {0}`n    {1}" -f $p, $r.Output)
}
Write-Host '--- 符號連結（主機端）：fixture 狀態'
Get-ChildItem "$SpikeRepos\links" -Force | Where-Object { $_.Name -like 'symlink-*' } | ForEach-Object { $_.Name }
if (-not (Get-ChildItem "$SpikeRepos\links" -Force | Where-Object { $_.Name -like 'symlink-*' })) { Write-Host '主機端無法建立 Windows 符號連結（需系統管理員或開發人員模式）→ 以「容器內在讀寫掛載建立 symlink」另行測試，見 Test-ManagedRw.ps1' }

Write-Host "`n===== (5) 大小寫語意"
Show-Result 'casedir（NTFS 不分大小寫）' (Invoke-Worker -Mount @($roMount) -Script 'case-probe.sh' -ScriptArgs '/mnt/repos/spike/casedir')
$null = Compare-GitRead -Title 'case-repo（Git 物件內 a.txt 與 A.txt 並存）' -HostRepo "$SpikeRepos\case-repo" -ContainerRepo '/mnt/repos/spike/case-repo' -Mount @($roMount)
$r = Invoke-Worker -Mount @($roMount) -Command 'git -C /mnt/repos/spike/case-repo ls-tree -r --name-only HEAD'
Write-Host "容器內 ls-tree 名稱：$(($r.Output -split "`n") -join ', ')"
Write-Host '主機 Python 對小寫路徑的 resolve（NTFS 還原實際大小寫，D18）：'
$tmpPy = Join-Path $ManagedRoot '_host-resolve2.py'
[IO.File]::WriteAllText($tmpPy, "import sys; from pathlib import Path; print(Path(sys.argv[1]).resolve(strict=True))")
Write-Host ('  ' + (Invoke-Native $pyHost @($tmpPy, "$SpikeRepos\CASEDIR\hello.TXT")).Output)
Write-Host "`n完成"
