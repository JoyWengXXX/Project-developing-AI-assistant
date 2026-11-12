# 第二階段切片 1 spike 的共用函式（由其他腳本 dot-source）。
# 用途：集中專案路徑、固定映像標籤與 docker run 輔助函式。不含秘密。
# 只寫入專案內的 .test-repos\ 與 .test-managed\（皆被 .gitignore 排除）。

Set-StrictMode -Version 2
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)

$script:ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script:ReposRoot = Join-Path $ProjectRoot '.test-repos'
$script:ManagedRoot = Join-Path $ProjectRoot '.test-managed'
$script:SpikeRepos = Join-Path $ReposRoot 'spike'
$script:SpikeManaged = Join-Path $ManagedRoot 'spike'
$script:ContainerScripts = Join-Path $ManagedRoot '_container-scripts'
$script:ContainerScriptsSource = Join-Path $PSScriptRoot 'container'
$script:WorkerImage = 'repopilot-worker:stage02-spike'

function Sync-ContainerScripts {
    # 把 scripts\spike\container\*.sh 以 LF 換行複製到 .test-managed\_container-scripts（工作目錄的 CRLF 會讓 sh 失敗）
    New-Item -ItemType Directory -Force $ContainerScripts | Out-Null
    Get-ChildItem -LiteralPath $ContainerScriptsSource -Filter *.sh | ForEach-Object {
        $text = [IO.File]::ReadAllText($_.FullName).Replace("`r`n", "`n")
        [IO.File]::WriteAllText((Join-Path $ContainerScripts $_.Name), $text, [Text.UTF8Encoding]::new($false))
    }
}

# 與主機 git 輸出比對時用的固定作者資訊與日期，讓 fixture 的 commit SHA 可重現
$env:GIT_AUTHOR_NAME = 'spike'
$env:GIT_AUTHOR_EMAIL = 'spike@example.invalid'
$env:GIT_COMMITTER_NAME = 'spike'
$env:GIT_COMMITTER_EMAIL = 'spike@example.invalid'
$env:GIT_AUTHOR_DATE = '2026-01-01T00:00:00Z'
$env:GIT_COMMITTER_DATE = '2026-01-01T00:00:00Z'

function ToDockerPath([string]$Path) {
    # Compose／docker 的 Windows 來源路徑，使用正斜線形式（D:/…）
    return $Path.Replace('\', '/')
}

function Invoke-Native {
    # 執行外部指令並回傳 @{ ExitCode; Output(string) }；stderr 併入輸出，不因 stderr 拋例外
    param([string]$File, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $File @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
        }
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
    return [pscustomobject]@{ ExitCode = $code; Output = ($lines -join "`n") }
}

function Invoke-Worker {
    # 以拋棄式容器執行 sh 指令（worker 映像、預設套用 spec 9.5 的加固）。
    # Mount：`--mount` 參數字串陣列；Script：容器內 /spike 下的腳本檔名（唯讀掛載 scripts\spike\container）
    param(
        [string[]]$Mount = @(),
        [string]$Command,
        [string]$Script,
        [string[]]$ScriptArgs = @(),
        [string]$User = '10001:10001',
        [switch]$NoHardening,
        [string[]]$Env = @(),
        [string]$Network,
        [string]$TmpfsOptions = '',
        [string]$Image = $WorkerImage
    )
    $dockerArgs = @('run', '--rm', '--user', $User)
    Sync-ContainerScripts
    if (-not $NoHardening) {
        # Docker 的 --tmpfs 預設為 noexec；需要在 /tmp 執行程式（例如 venv）時以 TmpfsOptions 指定 exec 與大小
        $tmp = if ($TmpfsOptions) { "/tmp:$TmpfsOptions" } else { '/tmp' }
        $dockerArgs += @('--read-only', '--tmpfs', $tmp, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true')
    }
    if ($Network) { $dockerArgs += @('--network', $Network) }
    foreach ($e in $Env) { $dockerArgs += @('-e', $e) }
    foreach ($m in $Mount) { $dockerArgs += @('--mount', $m) }
    $dockerArgs += @('--mount', "type=bind,source=$(ToDockerPath $ContainerScripts),target=/spike,readonly")
    $dockerArgs += @('--entrypoint', 'sh', $Image)
    if ($Script) { $dockerArgs += @("/spike/$Script") + $ScriptArgs } else { $dockerArgs += @('-c', $Command) }
    return Invoke-Native 'docker' $dockerArgs
}

function Get-HostGitStreamHash {
    # 在主機執行 git，回傳 stdout 的 SHA-256（小寫 hex）；可從 stdin 餵文字。以 MemoryStream 收 stdout，避免與 stdin 寫入互相卡死
    param([string]$Repo, [string[]]$GitArgs, [string]$StdinText)
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    $quoted = $GitArgs | ForEach-Object { if ($_ -match '[\s"]') { '"' + $_.Replace('"', '\"') + '"' } else { $_ } }
    $psi.Arguments = "-C `"$Repo`" " + ($quoted -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = ($PSBoundParameters.ContainsKey('StdinText'))
    $p = [Diagnostics.Process]::Start($psi)
    $ms = New-Object IO.MemoryStream
    $copy = $p.StandardOutput.BaseStream.CopyToAsync($ms)
    $errTask = $p.StandardError.ReadToEndAsync()
    if ($psi.RedirectStandardInput) {
        $b = [Text.UTF8Encoding]::new($false).GetBytes($StdinText)
        $p.StandardInput.BaseStream.Write($b, 0, $b.Length)
        $p.StandardInput.Close()
    }
    $copy.Wait(); $p.WaitForExit()
    if ($p.ExitCode -ne 0) { return "ERROR(exit $($p.ExitCode)): $($errTask.Result)" }
    $sha = [Security.Cryptography.SHA256]::Create()
    return (($sha.ComputeHash($ms.ToArray()) | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-HostGitRead {
    # 與 container\git-read.sh 輸出同鍵的主機端結果（Windows Git）
    param([string]$Repo)
    $lsLines = & git -C $Repo ls-tree -r HEAD | ForEach-Object { "$_" }
    $blobs = @($lsLines | Where-Object { $_ -match '^\d+ blob ' } | ForEach-Object { ($_ -split '[ \t]')[2] })
    $stdin = if ($blobs.Count) { ($blobs -join "`n") + "`n" } else { '' }
    [ordered]@{
        head              = (& git -C $Repo rev-parse HEAD).Trim()
        lstree_sha256     = Get-HostGitStreamHash $Repo @('ls-tree', '-r', '-l', '-z', 'HEAD')
        lstree_count      = @($lsLines).Count
        blob_count        = $blobs.Count
        batchcheck_sha256 = Get-HostGitStreamHash $Repo @('cat-file', '--batch-check') -StdinText $stdin
        batch_sha256      = Get-HostGitStreamHash $Repo @('cat-file', '--batch') -StdinText $stdin
    }
}

function ConvertFrom-KeyValueLines([string]$Text) {
    $h = [ordered]@{}
    foreach ($line in ($Text -split "`n")) {
        if ($line -match '^(?<k>[a-z_0-9]+)=(?<v>.*)$') { $h[$Matches.k] = $Matches.v.Trim() }
    }
    return $h
}

function Compare-GitRead {
    # 在容器內以 git-read.sh 讀 ContainerRepo，與主機端對 HostRepo 的結果逐鍵比對；回傳是否全部一致
    param([string]$Title, [string]$HostRepo, [string]$ContainerRepo, [string[]]$Mount, [string[]]$Env = @(), [string[]]$GitOptions = @())
    $hostRead = Get-HostGitRead $HostRepo
    $r = Invoke-Worker -Mount $Mount -Env $Env -Script 'git-read.sh' -ScriptArgs (@($ContainerRepo) + $GitOptions)
    $cont = ConvertFrom-KeyValueLines $r.Output
    $allOk = $true
    Write-Host "----- $Title (container exit=$($r.ExitCode)); toplevel=$($cont['toplevel'])"
    foreach ($k in $hostRead.Keys) {
        $same = ("$($hostRead[$k])" -eq "$($cont[$k])")
        if (-not $same) { $allOk = $false }
        Write-Host ('  {0,-18} {1} host={2} container={3}' -f $k, $(if ($same) { 'SAME' } else { 'DIFF' }), $hostRead[$k], $cont[$k])
    }
    if (-not $allOk -and $r.Output) { Write-Host $r.Output }
    return $allOk
}

function Show-Result {
    param([string]$Title, $Result)
    Write-Host "----- $Title (exit=$($Result.ExitCode))"
    if ($Result.Output) { Write-Host $Result.Output }
}

function Remove-LinkSafe {
    # 刪除 junction／symlink 本身而不跟隨到目標（Remove-Item -Recurse 在 Windows PowerShell 5.1 可能刪到目標內容）
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName) } else { [IO.File]::Delete($item.FullName) }
    }
}

function Remove-TestTree {
    # 只刪除 .test-repos\ 或 .test-managed\ 之下的路徑；先移除所有 reparse point，再清除唯讀屬性後遞迴刪除
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = (Resolve-Path -LiteralPath $Path).Path
    $allowed = @($ReposRoot, $ManagedRoot) | ForEach-Object { $_.TrimEnd('\') + '\' }
    if (-not ($allowed | Where-Object { $full.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) })) {
        throw "拒絕刪除測試目錄以外的路徑：$full"
    }
    $self = Get-Item -LiteralPath $full -Force
    if (-not $self.PSIsContainer) { Remove-Item -LiteralPath $full -Force; return }
    if ($self.Attributes -band [IO.FileAttributes]::ReparsePoint) { Remove-LinkSafe $full; return }
    Get-ChildItem -LiteralPath $full -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
        ForEach-Object { Remove-LinkSafe $_.FullName }
    # 長路徑與唯讀檔（git pack）：以 cmd 刪除較穩，先清屬性
    & attrib -R "$full\*" /S /D 2>&1 | Out-Null
    & cmd /c rmdir /S /Q "$full" 2>&1 | Out-Null
    if (Test-Path -LiteralPath $full) { Write-Warning "未能完全刪除：$full" }
}
