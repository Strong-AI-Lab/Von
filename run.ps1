<#
    Von Launcher / Process Manager
    Actions:
        start        Start server in background (default)
        foreground   Run server in foreground (like old behavior)
        stop         Gracefully stop via /admin/shutdown (fallback force)
        status       Report running/stopped + health
        restart      Stop then start
        logs         Show/tail current log (use -Tail/-Follow)
        check        Quick health check (exit codes: 0/healthy,2/unhealthy,3/not running)
        backup       Run manual backup
        restore-backup Dry-run/apply restore from a backup artefact or receipt
        help         Show help

    Flags:
        -Port <int>              Server port (default 5001 on macOS, 5000 elsewhere; -AgentTest defaults to 5010)
        -AgentTest               Isolated coding-agent test instance mode
        -IsolatedTestInstance    Alias for -AgentTest
        -NoBrowser               Do not auto-open browser on start
        -ForceBrowser            Force open browser even if already opened this session
        -ChromeBeta              Use Chrome Beta only (for MCP DevTools debugging)
        -Tail <n>                When action=logs, number of lines (default 100)
        -Follow                  When action=logs, stream updates
        -LogRetention <n>        Keep last n log files (default 20)
        -AdminToken <token>      Explicit admin token (else auto-generate & persist)

    TODO: Full multi-instance worker isolation for modes that require per-instance workers.
    TODO: Optional size-based log rollover.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)] [string]$Action = 'start',
    [int]$Port = 5000,
    [switch]$AgentTest,
    [switch]$IsolatedTestInstance,
    [switch]$NoBrowser,
    [switch]$ForceBrowser,
    [switch]$ChromeBeta,
    [int]$Tail = 100,
    [switch]$Follow,
    [int]$LogRetention = 20,
    [string]$AdminToken,
    [switch]$SkipHealth,
    [int]$HealthTimeoutSec = 960,
    [int]$HealthGraceSec = 45,
    [string[]]$ReadyLogPatterns = @('Running with Waitress', 'Press CTRL+C to quit', 'Flask app running'),
    [switch]$DisableLogReady,
    [switch]$HealthDebug,
    [switch]$ShowRelationCoverage,
    [switch]$StatusRunMaintenance,
    # On-demand backups
    [switch]$BackupDryRun,
    [string]$BackupTag = 'manual',
    [string]$BackupOutDir,
    # Restore backups
    [string]$RestoreBackupPath,
    [string]$RestoreTargetDbName,
    [switch]$RestoreApply,
    [switch]$RestoreDropTarget,
    # Auto-update (continuous self-updating runner)
    [int]$UpdateIntervalMinutes = 60,
    [string]$UpdateBranch = 'main',
    [switch]$UpdateNoRestartIfRunning,
    # Disable automatic migration of local backups to W: drive
    [switch]$NoBackupMigrate,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$ExtraArgs
)

$Action = $Action.ToLower()
if ($Action -in @('--help', '-h', '/?')) { $Action = 'help' }

$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$RunDir = Join-Path $Root '.run'
$LogsDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
$script:VonStartFailed = $false

# Ensure logging helper is available before any code path that may emit log lines.
if (-not (Get-Command Write-LauncherLog -ErrorAction SilentlyContinue)) {
    function Write-LauncherLog {
        param([Parameter(Mandatory = $true)][string]$Msg)
        $ts = (Get-Date).ToString('HH:mm:ss')
        Write-Host ("[{0}] {1}" -f $ts, $Msg)
    }
}

function Apply-LauncherSwitchCompatibility {
    param(
        [string]$RawInvocationLine = $MyInvocation.Line,
        [string[]]$RemainingArgs = $ExtraArgs
    )

    if ((-not $script:ForceBrowser) -and (($RemainingArgs -contains '--ForceBrowser') -or ($RawInvocationLine -match '(^|\s)--ForceBrowser(?:\s|$)'))) {
        $script:ForceBrowser = $true
    }
    if ((-not $script:NoBrowser) -and (($RemainingArgs -contains '--NoBrowser') -or ($RawInvocationLine -match '(^|\s)--NoBrowser(?:\s|$)'))) {
        $script:NoBrowser = $true
    }
    if ((-not $script:ChromeBeta) -and (($RemainingArgs -contains '--ChromeBeta') -or ($RawInvocationLine -match '(^|\s)--ChromeBeta(?:\s|$)'))) {
        $script:ChromeBeta = $true
    }
    if ((-not $script:AgentTest) -and (($RemainingArgs -contains '--AgentTest') -or ($RawInvocationLine -match '(^|\s)--AgentTest(?:\s|$)'))) {
        $script:AgentTest = $true
    }
    if ((-not $script:IsolatedTestInstance) -and (($RemainingArgs -contains '--IsolatedTestInstance') -or ($RawInvocationLine -match '(^|\s)--IsolatedTestInstance(?:\s|$)'))) {
        $script:IsolatedTestInstance = $true
    }
}

function Test-AgentTestInstance {
    return [bool]$script:AgentTestInstance
}

function Test-MacLauncherHost {
    return [bool](Get-Variable -Name IsMacOS -ValueOnly -ErrorAction SilentlyContinue)
}

function Get-StandardLauncherDefaultPort {
    if (Test-MacLauncherHost) { return 5001 }
    return 5000
}

function Apply-AgentTestLauncherDefaults {
    param([bool]$PortWasExplicitlyBound = $false)

    $script:StandardDefaultPort = Get-StandardLauncherDefaultPort
    $script:AgentTestDefaultPort = 5010
    $script:AgentTestInstance = [bool]($script:AgentTest -or $script:IsolatedTestInstance)
    $script:AgentTestPortDefaulted = $false
    if (-not (Test-AgentTestInstance)) {
        if (-not $PortWasExplicitlyBound) {
            $script:Port = $script:StandardDefaultPort
        }
        [Environment]::SetEnvironmentVariable('VON_AGENT_TEST_INSTANCE', $null, 'Process')
        return
    }

    if (-not $PortWasExplicitlyBound) {
        $script:Port = $script:AgentTestDefaultPort
        $script:AgentTestPortDefaulted = $true
    }
    if (-not $script:ForceBrowser) {
        $script:NoBrowser = $true
    }
    [Environment]::SetEnvironmentVariable('VON_AGENT_TEST_INSTANCE', '1', 'Process')
}

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { return "''" }
    return "'" + ($Value -replace "'", "''") + "'"
}

Apply-LauncherSwitchCompatibility
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$($PSBoundParameters.ContainsKey('Port'))

function Set-EnvFromDotEnv {
    param([string]$EnvPath)
    if (-not $EnvPath) { return }
    if (-not (Test-Path $EnvPath)) { return }

    $applied = 0
    Get-Content $EnvPath -ErrorAction SilentlyContinue | ForEach-Object {
        $line = $_.Trim()
        if (-not $line) { return }
        if ($line.StartsWith('#')) { return }

        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $key = $Matches[1]
            $raw = $Matches[2]
            if (-not $key) { return }
            $value = $raw.Trim()
            if ($value.Length -ge 2) {
                $first = $value.Substring(0, 1)
                $last = $value.Substring($value.Length - 1, 1)
                if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }

            [Environment]::SetEnvironmentVariable($key, $value, "Process")
            $applied++
        }
    }

    if ($applied -gt 0) {
        Write-LauncherLog "Loaded $applied .env override(s) from $EnvPath"
    }
}

Set-EnvFromDotEnv -EnvPath (Join-Path $Root '.env')
if (Test-AgentTestInstance) {
    [Environment]::SetEnvironmentVariable('VON_AGENT_TEST_INSTANCE', '1', 'Process')
}
else {
    [Environment]::SetEnvironmentVariable('VON_AGENT_TEST_INSTANCE', $null, 'Process')
}

function Test-TruthySetting {
    param([object]$Value)
    if ($null -eq $Value) { return $false }
    return $Value.ToString().Trim().ToLowerInvariant() -match '^(1|true|yes|y|on)$'
}

function Get-NormalisedAbsolutePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    try {
        $full = [System.IO.Path]::GetFullPath($expanded)
    }
    catch {
        $full = $expanded
    }
    return ($full -replace '/', '\').TrimEnd('\')
}

function Test-IsPathInsideRoot {
    param(
        [Parameter(Mandatory = $true)][string]$CandidatePath,
        [Parameter(Mandatory = $true)][string]$RootPath
    )
    $candidate = Get-NormalisedAbsolutePath -Path $CandidatePath
    $rootPathNormalised = Get-NormalisedAbsolutePath -Path $RootPath
    if ($candidate.Equals($rootPathNormalised, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidate.StartsWith($rootPathNormalised + '\', [StringComparison]::OrdinalIgnoreCase)
}

$AllowRepoBackupOutput = Test-TruthySetting $env:VON_ALLOW_BACKUP_IN_REPO

function Test-BackupOutputPathAllowed {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Context,
        [Parameter(Mandatory = $true)][bool]$ApplyMode
    )
    if (-not (Test-IsPathInsideRoot -CandidatePath $Path -RootPath $Root)) {
        return $true
    }

    $resolved = Get-NormalisedAbsolutePath -Path $Path
    if ($AllowRepoBackupOutput) {
        Write-LauncherLog "[$Context] WARN: backup output resolves inside repo root ($resolved), but continuing due to VON_ALLOW_BACKUP_IN_REPO=1."
        return $true
    }

    if ($ApplyMode) {
        Write-LauncherLog "[$Context] ERROR: refusing backup apply mode with output under repo root ($resolved). Set VON_BACKUP_ROOT/-BackupOutDir outside repo, or override with VON_ALLOW_BACKUP_IN_REPO=1."
        return $false
    }

    Write-LauncherLog "[$Context] WARN: backup output is under repo root ($resolved). Dry-run allowed, apply mode remains blocked unless VON_ALLOW_BACKUP_IN_REPO=1."
    return $true
}

# Backup root resolution:
# - Prefer explicit VON_BACKUP_ROOT if already present in environment.
# - Else prefer W:\von_backups if W: exists and is writable.
# - Else prefer a non-repo local path (%LOCALAPPDATA%\Von\backups).
# - Else fall back to repo-local 'backups' as a last resort.
$BackupRoot = $null
$script:DefaultNonRepoBackupRoot = $null

function Test-WDriveAvailable {
    <#
        Returns $true only when W: is present and reachable.
        This is more defensive than Test-Path alone (network drives can throw).
    #>
    try {
        $drive = Get-PSDrive -Name 'W' -ErrorAction Stop
        if (-not $drive) { return $false }
        try {
            return [bool](Test-Path -LiteralPath 'W:\' -ErrorAction Stop)
        }
        catch {
            return $false
        }
    }
    catch {
        return $false
    }
}

function Resolve-BackupOutDir {
    param(
        [Parameter(Mandatory = $true)][string]$OutDir,
        [Parameter(Mandatory = $true)][string]$FallbackDir,
        [Parameter(Mandatory = $true)][string]$Reason
    )

    $effective = $OutDir
    try {
        if ($effective -match '^[Ww]:\\' -and -not (Test-WDriveAvailable)) {
            Write-LauncherLog "[backup] WARN: W: drive unavailable ($Reason); falling back to local backups: $FallbackDir"
            $effective = $FallbackDir
        }
    }
    catch {
        # Defensive: if any drive probing fails, fall back to local.
        Write-LauncherLog "[backup] WARN: Backup destination probe failed ($Reason): $($_.Exception.Message); falling back to local backups: $FallbackDir"
        $effective = $FallbackDir
    }

    try { New-Item -ItemType Directory -Force -Path $effective | Out-Null } catch { }
    return $effective
}

function Get-BackupFallbackDir {
    if ($script:DefaultNonRepoBackupRoot -and $script:DefaultNonRepoBackupRoot.ToString().Trim()) {
        return $script:DefaultNonRepoBackupRoot
    }
    return (Join-Path $Root 'backups')
}

function Get-BackupArtifactSizeBytes {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    }
    catch {
        return [int64]0
    }

    if (-not $item.PSIsContainer) {
        return [int64]$item.Length
    }

    $total = [int64]0
    try {
        Get-ChildItem -LiteralPath $item.FullName -Recurse -File -ErrorAction Stop | ForEach-Object {
            $total += [int64]$_.Length
        }
    }
    catch {
        return [int64]0
    }
    return $total
}

function Test-BackupArtifactPresentAndNonEmpty {
    param([Parameter(Mandatory = $true)][string]$Path)

    $exists = $false
    try { $exists = [bool](Test-Path -LiteralPath $Path) } catch { $exists = $false }
    if (-not $exists) {
        return [pscustomobject]@{
            Present = $false
            SizeBytes = [int64]0
            Verified = $false
        }
    }

    $sizeBytes = Get-BackupArtifactSizeBytes -Path $Path
    return [pscustomobject]@{
        Present = $true
        SizeBytes = [int64]$sizeBytes
        Verified = ($sizeBytes -gt 0)
    }
}

$localAppDataRoot = if ($env:LOCALAPPDATA -and $env:LOCALAPPDATA.ToString().Trim()) {
    $env:LOCALAPPDATA.ToString().Trim()
}
else {
    Join-Path $HOME 'AppData\Local'
}

$defaultCandidate = Join-Path $localAppDataRoot 'Von\backups'
try {
    New-Item -ItemType Directory -Force -Path $defaultCandidate | Out-Null
    $script:DefaultNonRepoBackupRoot = $defaultCandidate
}
catch {
    Write-LauncherLog "[backup] WARN: Cannot create/write default non-repo backup root '$defaultCandidate'."
}

if ($env:VON_BACKUP_ROOT -and $env:VON_BACKUP_ROOT.ToString().Trim()) {
    $preferred = $env:VON_BACKUP_ROOT.ToString().Trim().Trim('"')
    try {
        New-Item -ItemType Directory -Force -Path $preferred | Out-Null
        $BackupRoot = $preferred
    }
    catch {
        Write-LauncherLog "[backup] WARN: Cannot create/write VON_BACKUP_ROOT='$preferred'; falling back to automatic selection."
    }
}

if (-not $BackupRoot -and (Test-WDriveAvailable)) {
    $preferred = 'W:\von_backups'
    try {
        New-Item -ItemType Directory -Force -Path $preferred | Out-Null
        $BackupRoot = $preferred
    }
    catch {
        Write-LauncherLog "[backup] WARN: Cannot create/write $preferred; falling back to repo local backups."
    }
}

if (-not $BackupRoot) {
    if ($script:DefaultNonRepoBackupRoot) {
        $BackupRoot = $script:DefaultNonRepoBackupRoot
    }
    else {
        $BackupRoot = Join-Path $Root 'backups'
        try { New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null } catch { }
        Write-LauncherLog "[backup] WARN: Falling back to repo-local backups root '$BackupRoot'. Set VON_BACKUP_ROOT to an external path for safer defaults."
    }
}

if (-not $BackupOutDir) {
    $BackupOutDir = $BackupRoot
}



function Invoke-MigrateLocalBackupsToWDrive {
    <#
        If W: exists and local backups dir exists, move all but the most recent
        timestamped backup artefact from local 'backups' into W:\von_backups.
        Skips if backup root already is W:, or fewer than 2 local backups.
        Moves both directories (<name>_YYYYMMDD_HHMMSS*) and zip artefacts
        (<name>_YYYYMMDD_HHMMSS*.zip or .zip.enc).
        Logs with [backup-migrate]. Failures on individual moves are warned and continue.
    #>
    $moved = 0
    $copied = 0
    if (-not (Test-WDriveAvailable)) { Write-LauncherLog "[backup-migrate] summary moved=$moved copied=$copied (no W: drive)"; return }
    # Even if current backup root is already on W:, still attempt to migrate any residual local backups directory.
    $localBackups = Join-Path $Root 'backups'
    if (-not (Test-Path $localBackups)) {
        try { New-Item -ItemType Directory -Force -Path $localBackups | Out-Null } catch { Write-LauncherLog "[backup-migrate] summary moved=$moved copied=$copied (cannot create local backups dir)"; return }
    }
    try {
        $items = Get-ChildItem -Path $localBackups -ErrorAction Stop | Where-Object {
            $_.PSIsContainer -or $_.Name -match '\.zip(\.enc)?$'
        }
    }
    catch { $items = @() }
    $candidates = $items | Where-Object {
        $_.Name -match '_[0-9]{8}_[0-9]{6}Z?' -and (
            $_.PSIsContainer -or $_.Name -match '\.zip(\.enc)?$'
        )
    }
    $newest = $null; $toMove = @()
    if ($candidates -and $candidates.Count -gt 0) {
        $sorted = $candidates | Sort-Object LastWriteTime
        $newest = $sorted[-1]
        if ($sorted.Count -gt 1) { $toMove = $sorted[0..($sorted.Count - 2)] }
    }
    $destRoot = 'W:\von_backups'
    if (-not (Test-Path $destRoot)) {
        try { New-Item -ItemType Directory -Force -Path $destRoot | Out-Null } catch { Write-LauncherLog "[backup-migrate] summary moved=$moved copied=$copied (create dest failed)"; return }
    }
    if ($toMove.Count -gt 0) {
        foreach ($item in $toMove) {
            $dest = Join-Path $destRoot $item.Name
            if (Test-Path $dest) {
                Write-Verbose ("[backup-migrate] Skipping existing {0} already on W:" -f $item.Name)
                continue
            }
            try {
                Write-LauncherLog "[backup-migrate] Moving $($item.Name) -> $destRoot"
                Move-Item -Path $item.FullName -Destination $dest -Force -ErrorAction Stop
                $moved++
                $verification = Test-BackupArtifactPresentAndNonEmpty -Path $dest
                if ($verification.Verified) {
                    Write-LauncherLog "[backup-migrate] Verified offsite artefact $dest size_bytes=$($verification.SizeBytes)"
                }
                else {
                    Write-LauncherLog "[backup-migrate] WARN offsite verification failed after move: $dest"
                }
            }
            catch {
                Write-LauncherLog "[backup-migrate] WARN move failed $($item.Name): $($_.Exception.Message)"
            }
        }
    }
    if ($newest -and -not (Test-Path (Join-Path $destRoot $newest.Name))) {
        try {
            Write-LauncherLog "[backup-migrate] Copying newest $($newest.Name) to W: (preserve local copy)"
            if ($newest.PSIsContainer) {
                Copy-Item -Path $newest.FullName -Destination (Join-Path $destRoot $newest.Name) -Recurse -Force -ErrorAction Stop
            }
            else {
                Copy-Item -Path $newest.FullName -Destination (Join-Path $destRoot $newest.Name) -Force -ErrorAction Stop
            }
            $copied++
            $copiedDest = Join-Path $destRoot $newest.Name
            $verification = Test-BackupArtifactPresentAndNonEmpty -Path $copiedDest
            if ($verification.Verified) {
                Write-LauncherLog "[backup-migrate] Verified offsite artefact $copiedDest size_bytes=$($verification.SizeBytes)"
            }
            else {
                Write-LauncherLog "[backup-migrate] WARN offsite verification failed after copy: $copiedDest"
            }
        }
        catch {
            Write-LauncherLog "[backup-migrate] WARN copy newest failed $($newest.Name): $($_.Exception.Message)"
        }
    }
    # Down-sync: if W: holds a newer backup than local newest (or local newest missing), copy newest W: back locally
    try {
        $wBackups = Get-ChildItem -Path $destRoot -ErrorAction Stop | Where-Object {
            $_.Name -match '_[0-9]{8}_[0-9]{6}Z?' -and (
                $_.PSIsContainer -or $_.Name -match '\.zip(\.enc)?$'
            )
        }
        if ($wBackups) {
            $wSorted = $wBackups | Sort-Object LastWriteTime
            $wNewest = $wSorted[-1]
            $localNewestPath = if ($newest) { $newest.FullName } else { $null }
            $needsDownSync = $false
            if (-not $localNewestPath) { $needsDownSync = $true }
            else {
                try {
                    $localNewestItem = Get-Item -LiteralPath $localNewestPath -ErrorAction Stop
                    if ($wNewest.LastWriteTime -gt $localNewestItem.LastWriteTime) { $needsDownSync = $true }
                }
                catch { $needsDownSync = $true }
            }
            if ($needsDownSync -and $wNewest) {
                $destLocal = Join-Path $localBackups $wNewest.Name
                if (-not (Test-Path $destLocal)) {
                    try {
                        Write-LauncherLog "[backup-migrate] Down-sync newer $($wNewest.Name) -> local backups"
                        if ($wNewest.PSIsContainer) {
                            Copy-Item -Path $wNewest.FullName -Destination $destLocal -Recurse -Force -ErrorAction Stop
                        }
                        else {
                            Copy-Item -Path $wNewest.FullName -Destination $destLocal -Force -ErrorAction Stop
                        }
                    }
                    catch { Write-LauncherLog "[backup-migrate] WARN down-sync failed $($wNewest.Name): $($_.Exception.Message)" }
                }
            }
        }
    }
    catch { Write-LauncherLog "[backup-migrate] WARN down-sync scan failed: $($_.Exception.Message)" }
    Write-LauncherLog "[backup-migrate] summary moved=$moved copied=$copied"
}
if (Test-AgentTestInstance) {
    Write-Host "[backup-migrate] disabled via -AgentTest"
}
elseif (-not $NoBackupMigrate) {
    Invoke-MigrateLocalBackupsToWDrive
}
else {
    Write-Host "[backup-migrate] disabled via -NoBackupMigrate"
}

# Sentinel file to ensure we open the browser only once automatically. Agent
# test instances are normally headless, but keep their forced-browser sentinel
# separate so acceptance runs cannot suppress the user's next normal start.
$BrowserSentinelName = if (Test-AgentTestInstance) { "browser_opened_once_${Port}" } else { 'browser_opened_once' }
$BrowserSentinel = Join-Path $RunDir $BrowserSentinelName
$WorkflowPurityPidFile = Join-Path $RunDir 'workflow_purity_check.pid'
$WorkflowPurityResultFile = Join-Path $RunDir 'workflow_purity_check_last_result.json'
$WorkflowPurityReportedFile = Join-Path $RunDir 'workflow_purity_check_last_reported.txt'
$WorkflowPurityRunnerScript = Join-Path $RunDir 'workflow_purity_runner.ps1'
$WorkflowPurityTtlSeconds = 86400
if ($env:VON_WORKFLOW_PURITY_TTL_SECONDS -match '^[0-9]+$') {
    $WorkflowPurityTtlSeconds = [int]$env:VON_WORKFLOW_PURITY_TTL_SECONDS
}

# PID & log paths (port-scoped for isolated launcher instances)
$PidFile = Join-Path $RunDir "von_${Port}.pid"
$CurrentLog = Join-Path $LogsDir "von_${Port}_current.log"
$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$NewLog = Join-Path $LogsDir "von_${Port}_${Timestamp}.log"
$RagPidFile = Join-Path $RunDir "rag_worker.pid"
$RagLogFile = Join-Path $LogsDir "rag_worker_${Timestamp}.log"
$ConceptIndexPidFile = Join-Path $RunDir "concept_index_worker.pid"
$ConceptIndexLogFile = Join-Path $LogsDir "concept_index_worker_${Timestamp}.log"

$script:RepairAttempted = $false

function Get-ExistingProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $content = Get-Content $PidFile -ErrorAction SilentlyContinue | Where-Object { $_ }
    if (-not $content) { return $null }
    $pidLine = $content | Select-Object -First 1
    if ($pidLine -notmatch 'PID=([0-9]+)') { return $null }
    $savedPid = [int]$Matches[1]
    try { $p = Get-Process -Id $savedPid -ErrorAction Stop; return $p } catch { return $null }
}

function Get-ListeningProcessByPort {
    param([int]$Port)
    try {
        if (-not $IsWindows) {
            $lsof = Get-Command lsof -ErrorAction SilentlyContinue
            if ($lsof) {
                $lines = & $lsof.Source -nP "-iTCP:$Port" -sTCP:LISTEN -Fp 2>$null
                foreach ($line in $lines) {
                    if ($line -notmatch '^p(\d+)$') { continue }
                    return (Get-Process -Id ([int]$Matches[1]) -ErrorAction Stop)
                }
            }
        }

        # Get-NetTCPConnection can hang on some hosts; parse netstat output instead.
        $lines = netstat -ano -p tcp 2>$null
        foreach ($line in $lines) {
            if ($line -notmatch '^\s*TCP\s+') { continue }
            $parts = (($line -replace '\s+', ' ').Trim() -split ' ')
            if ($parts.Count -lt 5) { continue }

            $localAddress = $parts[1]
            $state = $parts[3]
            $pidToken = $parts[4]
            if ($state -ne 'LISTENING') { continue }
            if ($localAddress -notmatch ':(\d+)$') { continue }
            if ([int]$Matches[1] -ne $Port) { continue }
            if ($pidToken -notmatch '^\d+$') { continue }

            return (Get-Process -Id ([int]$pidToken) -ErrorAction Stop)
        }
    }
    catch { }
    return $null
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)
    try {
        return (Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" | Select-Object -ExpandProperty CommandLine)
    }
    catch {
        return ''
    }
}

function Get-ProcessWorkingDirectory {
    param([int]$ProcessId)
    try {
        if (-not $IsWindows) {
            $lsof = Get-Command lsof -ErrorAction SilentlyContinue
            if ($lsof) {
                $lines = & $lsof.Source -a -p $ProcessId -d cwd -Fn 2>$null
                foreach ($line in $lines) {
                    if ($line -like 'n*') {
                        return $line.Substring(1)
                    }
                }
            }

            $procCwd = "/proc/$ProcessId/cwd"
            if (Test-Path $procCwd) {
                return (Resolve-Path $procCwd -ErrorAction Stop).Path
            }
        }
    }
    catch { }
    return ''
}

function Get-ProjectPythonExecutable {
    $candidatePaths = @()
    $pdmPythonPath = Join-Path $Root '.pdm-python'
    if (Test-Path $pdmPythonPath) {
        $recordedPython = (Get-Content $pdmPythonPath -Raw -ErrorAction SilentlyContinue).Trim()
        if ($recordedPython) {
            $candidatePaths += $recordedPython
        }
    }

    $candidatePaths += (Join-Path $Root '.venv\Scripts\python.exe')

    foreach ($candidatePath in $candidatePaths) {
        if (-not (Test-Path $candidatePath)) {
            continue
        }
        try {
            & $candidatePath -c "import sys" 1>$null 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $candidatePath
            }
        }
        catch { }
    }
    return 'python'
}

function Stop-ProcessTreeWithEscalation {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [int]$WaitMs = 10000
    )

    if ($ProcessId -le 0) { return $true }
    try { & taskkill /PID $ProcessId /T /F 1>$null 2>$null } catch { }

    $elapsedMs = 0
    while ($elapsedMs -lt $WaitMs) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $true }
        $elapsedMs += 500
    }

    try { Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    Start-Sleep -Milliseconds 250
    return (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue))
}

function Stop-PythonProcessesByScript {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptRelativePath,
        [Parameter(Mandatory = $true)][string]$Label,
        [int]$ExcludePid = 0
    )

    $pythonExe = Get-ProjectPythonExecutable
    $scriptPath = Join-Path $Root $ScriptRelativePath
    $helperScript = Join-Path $Root 'scripts/cleanup_stale_python_processes.py'
    if (-not (Test-Path $helperScript)) {
        Write-LauncherLog ("WARN: Stale-process cleanup helper missing for {0}: {1}" -f $Label, $helperScript)
        return
    }

    $raw = @()
    try {
        # Use a file-backed helper rather than `python -c` to avoid native-command
        # argument parsing regressions in newer PowerShell hosts.
        $raw = & $pythonExe $helperScript $scriptPath $ExcludePid 2>$null
    }
    catch {
        Write-LauncherLog ("WARN: Failed stale-process cleanup for {0}: {1}" -f $Label, $_.Exception.Message)
        return
    }

    if (-not $raw) { return }
    $lastLine = ($raw | Select-Object -Last 1)
    if (-not $lastLine) { return }
    try {
        $killed = ConvertFrom-Json $lastLine
        if ($killed -and $killed.Count -gt 0) {
            Write-LauncherLog ("Stopped stale {0} process(es): {1}" -f $Label, ($killed -join ', '))
        }
    }
    catch { }
}

function Test-IsVonMainProcess {
    param([int]$ProcessId)
    $cmdLine = Get-ProcessCommandLine -ProcessId $ProcessId
    if (-not $cmdLine) { return $false }

    $normalisedCmd = $cmdLine.ToLowerInvariant().Replace('\', '/')
    $normalisedRoot = $Root.ToLowerInvariant().Replace('\', '/')
    if (-not $normalisedCmd.Contains('src/workflows/von/main.py')) { return $false }
    if ($normalisedCmd.Contains($normalisedRoot)) { return $true }

    # Launchers start the server from $Root with a relative script argument.
    # In that case the command line may not contain the absolute repo path.
    $workingDirectory = Get-ProcessWorkingDirectory -ProcessId $ProcessId
    if ($workingDirectory) {
        $normalisedWorkingDirectory = $workingDirectory.ToLowerInvariant().Replace('\', '/')
        if ($normalisedWorkingDirectory -eq $normalisedRoot) { return $true }
    }

    return $false
}

function Stop-ProcessWithEscalation {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [int]$WaitMs = 10000
    )

    try { Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue } catch { }

    $elapsedMs = 0
    while ($elapsedMs -lt $WaitMs) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $true }
        $elapsedMs += 500
    }

    try { Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    Start-Sleep -Milliseconds 250
    return (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue))
}

function Invoke-RestartPortTakeover {
    param([Parameter(Mandatory = $true)]$Listener)

    $listenerPid = [int]$Listener.Id
    if (-not (Test-IsVonMainProcess -ProcessId $listenerPid)) {
        Write-LauncherLog ("Port {0} is owned by PID={1}, which does not look like this Von server. Aborting restart takeover." -f $Port, $listenerPid)
        return $false
    }

    Write-LauncherLog ("Restart takeover: stopping untracked Von listener PID={0} on port {1}..." -f $listenerPid, $Port)
    $stopped = Stop-ProcessWithEscalation -ProcessId $listenerPid
    if (-not $stopped) {
        Write-LauncherLog ("Failed to stop PID={0}; restart cannot continue safely." -f $listenerPid)
        return $false
    }

    $remaining = Get-ListeningProcessByPort -Port $Port
    if ($remaining) {
        Write-LauncherLog ("Port {0} is still in use by PID={1} after takeover attempt; aborting start." -f $Port, $remaining.Id)
        return $false
    }

    Write-LauncherLog ("Restart takeover succeeded; port {0} is clear." -f $Port)
    return $true
}

function Read-AdminToken {
    if ($AdminToken) { return $AdminToken }
    $tokenFile = Join-Path $RunDir 'admin_token.txt'
    if (Test-Path $tokenFile) { return (Get-Content $tokenFile -Raw).Trim() }
    $gen = [guid]::NewGuid().ToString('N')
    Set-Content $tokenFile $gen
    return $gen
}

function Get-MongoConnectionSummary {
    param([int]$Port)
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:$Port/api/system/db_status" -TimeoutSec 5
        if ($null -eq $resp) { return "Mongo: unknown" }
        $usingFallback = $resp.using_fallback
        $atlasDetected = $resp.atlas_detected
        $effectiveHost = $resp.effective_host
        if ($usingFallback -eq $true) {
            if ($effectiveHost) { return "Mongo: local fallback ($effectiveHost)" }
            return "Mongo: local fallback"
        }
        if ($atlasDetected -eq $true) {
            if ($effectiveHost) { return "Mongo: Atlas ($effectiveHost)" }
            return "Mongo: Atlas"
        }
        if ($effectiveHost) { return "Mongo: $effectiveHost" }
        return "Mongo: unknown"
    }
    catch {
        return "Mongo: status unavailable"
    }
}

function Invoke-VonLogRotation {
    # was Rotate-Logs
    # Keep a fixed number of logs per port
    $pattern = "von_${Port}_*.log"
    $files = Get-ChildItem -Path $LogsDir -Filter $pattern | Sort-Object LastWriteTime -Descending
    if ($files.Count -gt $LogRetention) {
        $filesToDelete = $files | Select-Object -Skip $LogRetention
        foreach ($f in $filesToDelete) { Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue }
    }
}

function Write-PidFile {
    param($ServerPid)
    $startIso = (Get-Date).ToString('o')
    Set-Content $PidFile "PID=$ServerPid`nPORT=$Port`nSTART=$startIso"
}

function Remove-PidFile { if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue } }

# Daily remote backup logic (auto every 24h) ---------------------------------

function Find-NextAllowedValue {
    param(
        [Parameter(Mandatory = $true)] [int[]]$AllowedValues,
        [Parameter(Mandatory = $true)] [int]$Start
    )
    foreach ($v in $AllowedValues) {
        if ($v -ge $Start) { return $v }
    }
    return $null
}

function Convert-CronTokenToInt {
    param(
        [Parameter(Mandatory = $true)] [string]$Token,
        [Parameter(Mandatory = $true)] [int]$Min,
        [Parameter(Mandatory = $true)] [int]$Max,
        [Parameter(Mandatory = $false)] [hashtable]$NameMap,
        [Parameter(Mandatory = $false)] [switch]$IsDayOfWeek
    )
    $t = $Token.Trim()
    if ($t -match '^\d+$') {
        $v = [int]$t
        if ($IsDayOfWeek -and $v -eq 7) { $v = 0 }
        if ($v -lt $Min -or $v -gt $Max) { throw "Cron value '$t' out of bounds [$Min,$Max]." }
        return $v
    }
    if ($NameMap) {
        $k = $t.ToUpperInvariant()
        if ($NameMap.ContainsKey($k)) {
            $v = [int]$NameMap[$k]
            if ($IsDayOfWeek -and $v -eq 7) { $v = 0 }
            if ($v -lt $Min -or $v -gt $Max) { throw "Cron value '$t' out of bounds [$Min,$Max]." }
            return $v
        }
    }
    throw "Unsupported cron token '$t'."
}

function ConvertFrom-CronField {
    param(
        [Parameter(Mandatory = $true)] [string]$Field,
        [Parameter(Mandatory = $true)] [int]$Min,
        [Parameter(Mandatory = $true)] [int]$Max,
        [Parameter(Mandatory = $false)] [switch]$IsDayOfWeek,
        [Parameter(Mandatory = $false)] [hashtable]$NameMap
    )
    $fieldTrim = $Field.Trim()
    if (-not $fieldTrim) { throw "Invalid cron field (empty)." }

    $allowed = New-Object 'System.Boolean[]' ($Max + 1)
    $isStar = $false

    if ($fieldTrim -eq '*') {
        $isStar = $true
        for ($i = $Min; $i -le $Max; $i++) { $allowed[$i] = $true }
    }
    else {
        $parts = $fieldTrim -split ','
        foreach ($rawPart in $parts) {
            $part = $rawPart.Trim()
            if (-not $part) { continue }

            # Supported forms: *, */n, a, a-b, a/n, a-b/n (lists via commas)
            if (-not ($part -match '^(?<base>[^/]+?)(?:\/(?<step>\d+))?$')) {
                throw "Unsupported cron token '$part' in field '$Field'."
            }
            $base = $Matches['base'].Trim()
            $step = 1
            if ($Matches['step']) {
                $step = [int]$Matches['step']
                if ($step -lt 1) { throw "Invalid cron step '/$step' in field '$Field'." }
            }

            $start = $null
            $end = $null
            if ($base -eq '*') {
                $start = $Min
                $end = $Max
            }
            elseif ($base -match '^(?<a>[^-]+)-(?<b>[^-]+)$') {
                $start = Convert-CronTokenToInt -Token $Matches['a'] -Min $Min -Max $Max -NameMap $NameMap -IsDayOfWeek:$IsDayOfWeek
                $end = Convert-CronTokenToInt -Token $Matches['b'] -Min $Min -Max $Max -NameMap $NameMap -IsDayOfWeek:$IsDayOfWeek
            }
            else {
                $start = Convert-CronTokenToInt -Token $base -Min $Min -Max $Max -NameMap $NameMap -IsDayOfWeek:$IsDayOfWeek
                $end = $start
            }

            if ($start -lt $Min -or $end -gt $Max -or $start -gt $end) {
                throw "Cron value range '$base' out of bounds [$Min,$Max] in field '$Field'."
            }

            for ($v = $start; $v -le $end; $v += $step) {
                $vv = $v
                if ($IsDayOfWeek -and $vv -eq 7) { $vv = 0 }
                $allowed[$vv] = $true
            }
        }
    }

    $allowedValues = @()
    for ($i = $Min; $i -le $Max; $i++) {
        if ($allowed[$i]) { $allowedValues += $i }
    }
    # NOTE: Do not use `-not $allowedValues` here; a single value of 0 (e.g. minute=0)
    # is treated as falsy in PowerShell, which incorrectly rejects valid cron fields.
    if ($null -eq $allowedValues -or $allowedValues.Count -eq 0) {
        throw "Cron field '$Field' selects no values."
    }

    return @{
        Allowed       = $allowed
        AllowedValues = [int[]]$allowedValues
        IsStar        = $isStar
        Min           = $Min
        Max           = $Max
    }
}

function ConvertFrom-CronSchedule {
    param([Parameter(Mandatory = $true)] [string]$Schedule)

    $tokens = ($Schedule.Trim() -split '\s+')
    if ($tokens.Count -ne 5) {
        throw "Cron schedule must have 5 fields: '<minute> <hour> <day-of-month> <month> <day-of-week>'."
    }

    $monthNames = @{
        'JAN' = 1; 'FEB' = 2; 'MAR' = 3; 'APR' = 4; 'MAY' = 5; 'JUN' = 6;
        'JUL' = 7; 'AUG' = 8; 'SEP' = 9; 'OCT' = 10; 'NOV' = 11; 'DEC' = 12
    }
    $dowNames = @{
        'SUN' = 0; 'MON' = 1; 'TUE' = 2; 'WED' = 3; 'THU' = 4; 'FRI' = 5; 'SAT' = 6
    }

    $minute = ConvertFrom-CronField -Field $tokens[0] -Min 0 -Max 59
    $hour = ConvertFrom-CronField -Field $tokens[1] -Min 0 -Max 23
    $dom = ConvertFrom-CronField -Field $tokens[2] -Min 1 -Max 31
    $month = ConvertFrom-CronField -Field $tokens[3] -Min 1 -Max 12 -NameMap $monthNames
    $dow = ConvertFrom-CronField -Field $tokens[4] -Min 0 -Max 7 -IsDayOfWeek -NameMap $dowNames

    return @{
        Minute     = $minute
        Hour       = $hour
        DayOfMonth = $dom
        Month      = $month
        DayOfWeek  = $dow
        Raw        = $Schedule
    }
}

function ConvertTo-CronSchedule {
    param([Parameter(Mandatory = $true)] [string]$Schedule)

    $clean = $Schedule.Trim()
    if (-not $clean) { return $clean }

    # Strip inline comments (e.g. "... # note")
    $hashIndex = $clean.IndexOf('#')
    if ($hashIndex -ge 0) {
        $clean = $clean.Substring(0, $hashIndex).Trim()
    }

    # Remove stray wrapping quotes
    $clean = $clean -replace "^[\""']+", ''
    $clean = $clean -replace "[\""']+$", ''

    # If extra tokens exist, keep only the first 5 cron fields
    $tokens = ($clean -split '\s+')
    if ($tokens.Count -gt 5) {
        $clean = ($tokens[0..4] -join ' ')
    }

    return $clean
}

function Test-CronDayMatch {
    param(
        [Parameter(Mandatory = $true)] $Cron,
        [Parameter(Mandatory = $true)] [datetime]$UtcDateTime
    )

    $day = $UtcDateTime.Day
    $dow = [int]$UtcDateTime.DayOfWeek  # Sunday=0
    $domOk = $Cron.DayOfMonth.Allowed[$day]
    $dowOk = $Cron.DayOfWeek.Allowed[$dow]

    # Vixie-style semantics: if both DOM and DOW are restricted (not '*'), match if either matches.
    if ($Cron.DayOfMonth.IsStar -and $Cron.DayOfWeek.IsStar) { return $true }
    if ($Cron.DayOfMonth.IsStar) { return $dowOk }
    if ($Cron.DayOfWeek.IsStar) { return $domOk }
    return ($domOk -or $dowOk)
}

function Test-CronMatch {
    param(
        [Parameter(Mandatory = $true)] $Cron,
        [Parameter(Mandatory = $true)] [datetime]$UtcDateTime
    )

    if (-not $Cron.Month.Allowed[$UtcDateTime.Month]) { return $false }
    if (-not (Test-CronDayMatch -Cron $Cron -UtcDateTime $UtcDateTime)) { return $false }
    if (-not $Cron.Hour.Allowed[$UtcDateTime.Hour]) { return $false }
    if (-not $Cron.Minute.Allowed[$UtcDateTime.Minute]) { return $false }
    return $true
}

function Get-NextCronOccurrenceUtc {
    param(
        [Parameter(Mandatory = $true)] $Cron,
        [Parameter(Mandatory = $true)] [datetime]$AfterUtc
    )

    $after = $AfterUtc
    if ($after.Kind -ne [DateTimeKind]::Utc) { $after = $after.ToUniversalTime() }

    # Round down to minute, then advance by one minute (cron has minute granularity).
    $dt = [datetime]::new($after.Year, $after.Month, $after.Day, $after.Hour, $after.Minute, 0, [DateTimeKind]::Utc).AddMinutes(1)
    $limit = $dt.AddDays(370)

    while ($dt -le $limit) {
        # Month
        if (-not $Cron.Month.Allowed[$dt.Month]) {
            $nextMonth = Find-NextAllowedValue -AllowedValues $Cron.Month.AllowedValues -Start ($dt.Month + 1)
            $year = $dt.Year
            if ($null -eq $nextMonth) {
                $year = $year + 1
                $nextMonth = $Cron.Month.AllowedValues[0]
            }
            $dt = [datetime]::new($year, $nextMonth, 1, 0, 0, 0, [DateTimeKind]::Utc)
            continue
        }

        # Day (DOM/DOW)
        if (-not (Test-CronDayMatch -Cron $Cron -UtcDateTime $dt)) {
            $dt = [datetime]::new($dt.Year, $dt.Month, $dt.Day, 0, 0, 0, [DateTimeKind]::Utc).AddDays(1)
            continue
        }

        # Hour
        if (-not $Cron.Hour.Allowed[$dt.Hour]) {
            $nextHour = Find-NextAllowedValue -AllowedValues $Cron.Hour.AllowedValues -Start ($dt.Hour + 1)
            if ($null -ne $nextHour) {
                $dt = [datetime]::new($dt.Year, $dt.Month, $dt.Day, $nextHour, 0, 0, [DateTimeKind]::Utc)
            }
            else {
                $dt = [datetime]::new($dt.Year, $dt.Month, $dt.Day, 0, 0, 0, [DateTimeKind]::Utc).AddDays(1)
            }
            continue
        }

        # Minute
        if (-not $Cron.Minute.Allowed[$dt.Minute]) {
            $nextMinute = Find-NextAllowedValue -AllowedValues $Cron.Minute.AllowedValues -Start ($dt.Minute + 1)
            if ($null -ne $nextMinute) {
                $dt = [datetime]::new($dt.Year, $dt.Month, $dt.Day, $dt.Hour, $nextMinute, 0, [DateTimeKind]::Utc)
            }
            else {
                $dt = [datetime]::new($dt.Year, $dt.Month, $dt.Day, $dt.Hour, 0, 0, [DateTimeKind]::Utc).AddHours(1)
            }
            continue
        }

        if (Test-CronMatch -Cron $Cron -UtcDateTime $dt) { return $dt }
        $dt = $dt.AddMinutes(1)
    }

    return $null
}

$script:DailyBackupLauncherReceiptFileName = 'last_successful_backup_receipt.json'
$script:DailyBackupReceiptSidecarSuffix = '.backup_receipt.json'

function Get-DailyBackupLauncherReceiptPath {
    return Join-Path $RunDir $script:DailyBackupLauncherReceiptFileName
}

function Resolve-BackupReceiptCompletedAtUtc {
    param(
        [AllowNull()][object]$Value
    )

    if ($null -eq $Value) { return $null }

    if ($Value -is [datetimeoffset]) {
        $completedAtUtc = $Value.UtcDateTime
        $completedAtText = $completedAtUtc.ToString(
            "yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'",
            [System.Globalization.CultureInfo]::InvariantCulture
        )
    }
    elseif ($Value -is [datetime]) {
        $completedAtUtc = $Value.ToUniversalTime()
        $completedAtText = $completedAtUtc.ToString(
            "yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'",
            [System.Globalization.CultureInfo]::InvariantCulture
        )
    }
    else {
        $completedAtText = [string]$Value
        if (-not $completedAtText) { return $null }
        $dateStyles = [System.Globalization.DateTimeStyles]::AllowWhiteSpaces `
            -bor [System.Globalization.DateTimeStyles]::AssumeUniversal `
            -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal
        $completedAtUtc = [DateTime]::Parse(
            $completedAtText,
            [System.Globalization.CultureInfo]::InvariantCulture,
            $dateStyles
        )
    }

    return [pscustomobject]@{
        CompletedAtText = $completedAtText
        CompletedAtUtc = $completedAtUtc
    }
}

function Read-BackupSuccessReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Context,
        [switch]$RequireExistingArtifact
    )

    if (-not (Test-Path -LiteralPath $Path)) { return $null }

    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        Write-LauncherLog "[$Context] WARN: could not parse backup receipt '$Path': $($_.Exception.Message)"
        return $null
    }

    try {
        $completedAtInfo = Resolve-BackupReceiptCompletedAtUtc -Value $raw.completed_at_utc
    }
    catch {
        $completedAtText = if ($null -ne $raw.completed_at_utc) { [string]$raw.completed_at_utc } else { '' }
        Write-LauncherLog "[$Context] WARN: backup receipt '$Path' has invalid completed_at_utc '$completedAtText'."
        return $null
    }

    if ($null -eq $completedAtInfo) {
        Write-LauncherLog "[$Context] WARN: backup receipt '$Path' is missing completed_at_utc."
        return $null
    }

    $completedAtText = [string]$completedAtInfo.CompletedAtText
    $completedAtUtc = [datetime]$completedAtInfo.CompletedAtUtc

    $artifactPath = [string]$raw.final_artifact_path
    if (-not $artifactPath) {
        Write-LauncherLog "[$Context] WARN: backup receipt '$Path' is missing final_artifact_path."
        return $null
    }

    $artifactExists = $false
    try { $artifactExists = [bool](Test-Path -LiteralPath $artifactPath) } catch { $artifactExists = $false }
    if ($RequireExistingArtifact -and -not $artifactExists) {
        return $null
    }

    return [pscustomobject]@{
        Path = $Path
        Raw = $raw
        CompletedAtUtc = $completedAtUtc
        CompletedAtText = $completedAtText
        FinalArtifactPath = $artifactPath
        ArtifactExists = $artifactExists
        Tag = [string]$raw.tag
        DbName = [string]$raw.db_name
        ArtifactKind = [string]$raw.artifact_kind
    }
}

function Set-LauncherBackupSuccessReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][object]$Receipt,
        [string]$LegacySentinelPath,
        [Parameter(Mandatory = $true)][string]$Context
    )

    $receiptDir = Split-Path -Parent $ReceiptPath
    if ($receiptDir) {
        New-Item -ItemType Directory -Force -Path $receiptDir | Out-Null
    }

    $Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8

    if ($LegacySentinelPath) {
        try {
            $completedAtInfo = Resolve-BackupReceiptCompletedAtUtc -Value $Receipt.completed_at_utc
            $completedAtText = if ($completedAtInfo) { [string]$completedAtInfo.CompletedAtText } else { [string]$Receipt.completed_at_utc }
            $completedAtText | Set-Content -LiteralPath $LegacySentinelPath -Encoding ASCII
        }
        catch {
            Write-LauncherLog "[$Context] WARN: could not mirror legacy sentinel '$LegacySentinelPath': $($_.Exception.Message)"
        }
    }
}

function Get-NewestValidatedDailyBackupArtifactReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$Context
    )

    if (-not $BackupRoot) { return $null }
    try {
        if (-not (Test-Path -LiteralPath $BackupRoot)) { return $null }
    }
    catch {
        return $null
    }

    $sidecars = @()
    try {
        $sidecars = Get-ChildItem -LiteralPath $BackupRoot -File -ErrorAction Stop | Where-Object {
            $_.Name.EndsWith($script:DailyBackupReceiptSidecarSuffix, [System.StringComparison]::OrdinalIgnoreCase)
        }
    }
    catch {
        Write-LauncherLog "[$Context] WARN: could not inspect backup artefact receipts under '$BackupRoot': $($_.Exception.Message)"
        return $null
    }

    $validReceipts = @()
    foreach ($sidecar in $sidecars) {
        $receipt = Read-BackupSuccessReceipt -Path $sidecar.FullName -Context $Context -RequireExistingArtifact
        if ($null -eq $receipt) { continue }
        if ($receipt.Tag -ne 'auto-daily') { continue }
        $validReceipts += $receipt
    }

    if (-not $validReceipts) { return $null }
    return $validReceipts | Sort-Object -Property CompletedAtUtc -Descending | Select-Object -First 1
}

function Resolve-DailyBackupLastSuccessRecord {
    param(
        [Parameter(Mandatory = $true)][string]$LauncherReceiptPath,
        [Parameter(Mandatory = $true)][string]$LegacySentinelPath,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$Context
    )

    $launcherReceipt = Read-BackupSuccessReceipt -Path $LauncherReceiptPath -Context $Context
    $newestArtifactReceipt = Get-NewestValidatedDailyBackupArtifactReceipt -BackupRoot $BackupRoot -Context $Context

    if ($newestArtifactReceipt -and (
            (-not $launcherReceipt) -or
            ($newestArtifactReceipt.CompletedAtUtc -gt $launcherReceipt.CompletedAtUtc)
        )) {
        $launcherSummary = if ($launcherReceipt) {
            "$($launcherReceipt.CompletedAtText) -> $($launcherReceipt.FinalArtifactPath)"
        }
        else {
            'missing'
        }
        Write-LauncherLog "[$Context] WARN: authoritative launcher receipt is stale/missing ($launcherSummary); repairing from newest validated backup artefact receipt '$($newestArtifactReceipt.FinalArtifactPath)'."
        Set-LauncherBackupSuccessReceipt -ReceiptPath $LauncherReceiptPath -Receipt $newestArtifactReceipt.Raw -LegacySentinelPath $LegacySentinelPath -Context $Context
        return [pscustomobject]@{
            CompletedAtUtc = $newestArtifactReceipt.CompletedAtUtc
            CompletedAtText = $newestArtifactReceipt.CompletedAtText
            Source = 'artifact_receipt_repaired'
            FinalArtifactPath = $newestArtifactReceipt.FinalArtifactPath
            ReceiptPath = $LauncherReceiptPath
        }
    }

    if ($launcherReceipt) {
        return [pscustomobject]@{
            CompletedAtUtc = $launcherReceipt.CompletedAtUtc
            CompletedAtText = $launcherReceipt.CompletedAtText
            Source = 'launcher_receipt'
            FinalArtifactPath = $launcherReceipt.FinalArtifactPath
            ReceiptPath = $LauncherReceiptPath
        }
    }

    if (Test-Path -LiteralPath $LegacySentinelPath) {
        try {
            $legacyText = (Get-Content -LiteralPath $LegacySentinelPath -Raw -ErrorAction Stop).Trim()
            if ($legacyText) {
                $legacyUtc = [DateTime]::Parse($legacyText).ToUniversalTime()
                Write-LauncherLog "[$Context] WARN: falling back to legacy last_backup_utc.txt sentinel because the authoritative launcher receipt is unavailable."
                return [pscustomobject]@{
                    CompletedAtUtc = $legacyUtc
                    CompletedAtText = $legacyUtc.ToString('o')
                    Source = 'legacy_sentinel'
                    FinalArtifactPath = $null
                    ReceiptPath = $LegacySentinelPath
                }
            }
        }
        catch {
            Write-LauncherLog "[$Context] WARN: could not parse legacy backup sentinel '$LegacySentinelPath': $($_.Exception.Message)"
        }
    }

    return $null
}

function Invoke-DailyBackupIfDue {
    <#
        Performs a non-blocking (background job) backup of the remote DB
        at most once per interval (default 24h) using scripts/backup_von_db.py.
        Skips unless VON_ENABLE_DAILY_BACKUP is set to a truthy value.
        Also skips if VON_DISABLE_DAILY_BACKUP is set to a truthy value, for
        compatibility with existing local opt-out configurations.
        Supports schedule via VON_BACKUP_SCHEDULE (cron-like string) using 5 fields:
        "<minute> <hour> <day-of-month> <month> <day-of-week>".
        Falls back to interval-based schedule via VON_BACKUP_INTERVAL_HOURS if the cron
        string is missing or unsupported.
        Uses a structured last-success launcher receipt as the authoritative state
        source and mirrors the legacy ISO8601 UTC sentinel during migration.
        Logs are prefixed with [daily-backup].
    #>
    if (-not (Test-TruthySetting $env:VON_ENABLE_DAILY_BACKUP)) {
        Write-LauncherLog '[daily-backup] Skip: automatic backups disabled. Set VON_ENABLE_DAILY_BACKUP=1 on a designated backup host to enable.'
        return
    }
    if (Test-TruthySetting $env:VON_DISABLE_DAILY_BACKUP) {
        Write-LauncherLog '[daily-backup] Skip: disabled via VON_DISABLE_DAILY_BACKUP.'
        return
    }
    $schedule = $null
    if ($env:VON_BACKUP_SCHEDULE -and $env:VON_BACKUP_SCHEDULE.ToString().Trim()) {
        $rawSchedule = $env:VON_BACKUP_SCHEDULE.ToString()
        $schedule = ConvertTo-CronSchedule -Schedule $rawSchedule
    }
    $intervalHours = 24
    try { if ($env:VON_BACKUP_INTERVAL_HOURS) { $intervalHours = [int]$env:VON_BACKUP_INTERVAL_HOURS } } catch { }
    if ($intervalHours -lt 1) { $intervalHours = 24 }
    $localFallback = Get-BackupFallbackDir
    $effectiveBackupRoot = Resolve-BackupOutDir -OutDir $BackupRoot -FallbackDir $localFallback -Reason 'daily-backup'
    $receiptPath = Get-DailyBackupLauncherReceiptPath
    $sentinel = Join-Path $RunDir 'last_backup_utc.txt'
    $lastRecord = Resolve-DailyBackupLastSuccessRecord -LauncherReceiptPath $receiptPath -LegacySentinelPath $sentinel -BackupRoot $effectiveBackupRoot -Context 'daily-backup'
    $last = if ($lastRecord) { $lastRecord.CompletedAtUtc } else { $null }
    $nowUtc = (Get-Date).ToUniversalTime()
    $due = $true
    $next = $null
    $hours = $null
    if ($schedule) {
        try {
            $cron = ConvertFrom-CronSchedule -Schedule $schedule
            $effectiveLast = if ($last) { $last } else { $nowUtc.AddDays(-370) }
            $next = Get-NextCronOccurrenceUtc -Cron $cron -AfterUtc $effectiveLast
            if ($null -eq $next -or $next -gt $nowUtc) { $due = $false }
        }
        catch {
            Write-LauncherLog "[daily-backup] WARN: Unsupported VON_BACKUP_SCHEDULE='$schedule' ($($_.Exception.Message)); falling back to interval ${intervalHours}h."
            $schedule = $null
        }
    }
    if (-not $schedule) {
        if ($last) {
            $hours = ($nowUtc - $last).TotalHours
            if ($hours -lt $intervalHours) { $due = $false }
        }
    }
    if (-not $due) {
        if ($schedule -and $lastRecord -and $next) {
            Write-LauncherLog "[daily-backup] Skip: last-success=$($lastRecord.CompletedAtText) source=$($lastRecord.Source) next-due=$($next.ToString('o')) now=$($nowUtc.ToString('o'))."
        }
        elseif ($lastRecord -and $null -ne $hours) {
            $roundedHours = [Math]::Round($hours, 2)
            Write-LauncherLog "[daily-backup] Skip: last-success=$($lastRecord.CompletedAtText) source=$($lastRecord.Source) age_hours=$roundedHours interval_hours=$intervalHours."
        }
        else {
            Write-LauncherLog '[daily-backup] Skip: backup not due.'
        }
        return
    }
    # Avoid launching duplicate job
    $existingJob = Get-Job -Name 'von_daily_backup' -ErrorAction SilentlyContinue | Where-Object { $_.State -in 'Running', 'NotStarted' }
    if ($existingJob) {
        Write-LauncherLog '[daily-backup] Skip: background backup job is already running.'
        return
    }
    $backupScript = Join-Path $Root 'scripts/backup_von_db.py'
    if (-not (Test-Path $backupScript)) {
        Write-LauncherLog "[daily-backup] WARN: backup script missing: $backupScript (skipping)"
        return
    }
    $pdmExe = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    if (-not (Test-BackupOutputPathAllowed -Path $effectiveBackupRoot -Context 'daily-backup' -ApplyMode $true)) {
        Write-LauncherLog "[daily-backup] WARN: skipping scheduled backup until VON_BACKUP_ROOT points outside repo (or VON_ALLOW_BACKUP_IN_REPO=1)."
        return
    }
    $dueReason = if ($schedule) { "schedule=$schedule" } else { "interval=${intervalHours}h" }
    $lastSuccessSummary = if ($lastRecord) { $lastRecord.CompletedAtText } else { 'none' }
    $lastSuccessSource = if ($lastRecord) { $lastRecord.Source } else { 'none' }
    Write-LauncherLog "[daily-backup] Launching background backup ($dueReason last-success=$lastSuccessSummary source=$lastSuccessSource out=$effectiveBackupRoot)."
    Start-Job -Name 'von_daily_backup' -ScriptBlock {
        param($pdmExe, $root, $runDir, $receiptPath, $sentinelPath, $backupRoot, $backupScript)
        try {
            Set-Location $root
            # Force remote; disable local fallback for this backup invocation
            $env:MONGO_ALLOW_LOCAL_FALLBACK = '0'
            $backupOutputPath = Join-Path $runDir 'daily_backup_last_output.log'
            $backupOutput = & $pdmExe run python $backupScript --apply --out-dir $backupRoot --tag auto-daily --launcher-receipt-path $receiptPath --legacy-sentinel-path $sentinelPath 2>&1 | ForEach-Object { "[daily-backup] $_" }
            try { $backupOutput | Out-File -FilePath $backupOutputPath -Encoding UTF8 } catch { }
            $backupOutput | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { throw "backup script failed with exit code $LASTEXITCODE" }
            if (-not ($env:VON_DISABLE_CODE_MENTION_SCAN -and $env:VON_DISABLE_CODE_MENTION_SCAN.ToString() -match '^(1|true|yes)$')) {
                $scanScript = Join-Path $root 'src/utilities/scan_code_concepts.py'
                if (Test-Path $scanScript) {
                    & $pdmExe run python $scanScript --sync-mentions --apply 2>&1 | ForEach-Object { "[code-mention-scan] $_" }
                    if ($LASTEXITCODE -ne 0) { Write-Host "[code-mention-scan] ERROR exit=$LASTEXITCODE" }
                }
                else {
                    Write-Host "[code-mention-scan] WARN: script missing ($scanScript)"
                }
            }
            else {
                Write-Host "[code-mention-scan] disabled via VON_DISABLE_CODE_MENTION_SCAN"
            }
            if (-not ($env:VON_DISABLE_CODE_PREDICATE_SYNC -and $env:VON_DISABLE_CODE_PREDICATE_SYNC.ToString() -match '^(1|true|yes)$')) {
                $syncScript = Join-Path $root 'src/utilities/sync_code_predicates.py'
                if (Test-Path $syncScript) {
                    & $pdmExe run python $syncScript --retag-non-predicates 2>&1 | ForEach-Object { "[code-predicate-sync] $_" }
                    if ($LASTEXITCODE -ne 0) { Write-Host "[code-predicate-sync] ERROR exit=$LASTEXITCODE" }
                }
                else {
                    Write-Host "[code-predicate-sync] WARN: script missing ($syncScript)"
                }
            }
            else {
                Write-Host "[code-predicate-sync] disabled via VON_DISABLE_CODE_PREDICATE_SYNC"
            }
            Write-Host '[daily-backup] Completed.'
        }
        catch {
            Write-Host ("[daily-backup] ERROR: {0}" -f $_.Exception.Message)
        }
    } -ArgumentList $pdmExe, $Root, $RunDir, $receiptPath, $sentinel, $effectiveBackupRoot, $backupScript | Out-Null
}

# Backward-compatible convenience: if called as ".\run.ps1 -BackupDryRun" (no explicit action)
# then run the backup action, even if the server is already running.
if ($Action -eq 'start' -and (
        $PSBoundParameters.ContainsKey('BackupDryRun') -or
        $PSBoundParameters.ContainsKey('BackupTag') -or
        $PSBoundParameters.ContainsKey('BackupOutDir')
    )) {
    Write-LauncherLog "Start requested with backup flags; running backup action only."
    $Action = 'backup'
}

# Test DB periodic refresh logic (every 4 hours) ------------------------------
function Invoke-TestDbRefreshIfDue {
    <#
        Clones primary DB into test DB using refresh_test_db.py every 4 hours
        if ALL conditions are met:
          - Not disabled via VON_DISABLE_TEST_DB_REFRESH (truthy => disable)
          - Source concepts collection has >= 1000 documents
          - refresh_test_db.py present
          - mongodump & mongorestore available (delegated to script itself)
        Interval override (hours) via VON_TEST_DB_REFRESH_INTERVAL_HOURS (min 1).
        Sentinel: last_test_db_refresh_utc.txt stores ISO8601 UTC timestamp.
        Logs are prefixed with [test-db-refresh]. Dry-run first unless
        VON_TEST_DB_REFRESH_APPLY=1 (then passes --apply). Always uses --drop-target.
    #>
    if ($env:VON_DISABLE_TEST_DB_REFRESH -and $env:VON_DISABLE_TEST_DB_REFRESH.ToString() -match '^(1|true|yes)$') { return }
    $intervalHours = 4
    try { if ($env:VON_TEST_DB_REFRESH_INTERVAL_HOURS) { $intervalHours = [int]$env:VON_TEST_DB_REFRESH_INTERVAL_HOURS } } catch { }
    if ($intervalHours -lt 1) { $intervalHours = 4 }
    $sentinel = Join-Path $RunDir 'last_test_db_refresh_utc.txt'
    $last = $null
    if (Test-Path $sentinel) { try { $last = [DateTime]::Parse((Get-Content $sentinel -Raw).Trim()).ToUniversalTime() } catch { $last = $null } }
    $nowUtc = (Get-Date).ToUniversalTime()
    $due = $true
    if ($last) {
        $hours = ($nowUtc - $last).TotalHours
        if ($hours -lt $intervalHours) { $due = $false }
    }
    if (-not $due) { return }
    # Avoid duplicate concurrent job
    $existingJob = Get-Job -Name 'von_test_db_refresh' -ErrorAction SilentlyContinue | Where-Object { $_.State -in 'Running', 'NotStarted' }
    if ($existingJob) { return }
    $refreshScript = Join-Path $Root 'scripts/maintenance/refresh_test_db.py'
    if (-not (Test-Path $refreshScript)) { return }
    $countScript = Join-Path $Root 'scripts/maintenance/count_concepts.py'
    if (-not (Test-Path $countScript)) { return }
    $pdmExe = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    # Get concept count (safely parse int)
    try {
        $countRaw = & $pdmExe run python $countScript 2>$null
    }
    catch { return }
    if (-not $countRaw) { return }
    $conceptCount = 0
    if (-not [int]::TryParse($countRaw, [ref]$conceptCount)) { return }
    if ($conceptCount -lt 1000) { return }
    Write-LauncherLog ("[test-db-refresh] Launching background refresh (concepts={0}, interval={1}h)..." -f $conceptCount, $intervalHours)
    $apply = $false
    if ($env:VON_TEST_DB_REFRESH_APPLY -and $env:VON_TEST_DB_REFRESH_APPLY.ToString() -match '^(1|true|yes)$') { $apply = $true }
    Start-Job -Name 'von_test_db_refresh' -ScriptBlock {
        param($pdmExe, $root, $refreshPath, $sentinelPath, $applyFlag)
        try {
            Set-Location $root
            $refreshArgs = @('run', 'python', $refreshPath, '--drop-target')
            if ($applyFlag) { $refreshArgs += '--apply' }
            & $pdmExe @refreshArgs 2>&1 | ForEach-Object { "[test-db-refresh] $_" }
            (Get-Date).ToUniversalTime().ToString('o') | Set-Content $sentinelPath
            Write-Host '[test-db-refresh] Completed.'
        }
        catch {
            Write-Host ("[test-db-refresh] ERROR: {0}" -f $_.Exception.Message)
        }
    } -ArgumentList $pdmExe, $Root, $refreshScript, $sentinel, $apply | Out-Null
}

function Get-ConfiguredPathList {
    param([object]$Value)
    $paths = @()
    if ($null -eq $Value) { return $paths }
    $text = $Value.ToString().Trim()
    if (-not $text) { return $paths }
    foreach ($item in ($text -split '[,;]')) {
        $trimmed = $item.Trim().Trim('"')
        if ($trimmed) { $paths += $trimmed }
    }
    return $paths
}

function Get-AiChatSessionSyncCodexRoot {
    $configured = if ($env:VON_AI_CHAT_SESSION_SYNC_CODEX_ROOT -and $env:VON_AI_CHAT_SESSION_SYNC_CODEX_ROOT.ToString().Trim()) {
        $env:VON_AI_CHAT_SESSION_SYNC_CODEX_ROOT.ToString().Trim()
    }
    else {
        Join-Path $HOME '.codex\sessions'
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($configured)
    if (-not (Test-Path -LiteralPath $expanded)) {
        Write-LauncherLog "[ai-chat-session-sync] WARN: Codex root missing: $expanded"
        return $null
    }
    return (Get-NormalisedAbsolutePath -Path $expanded)
}

function Get-AiChatSessionSyncCopilotRoots {
    $userRoots = Get-ConfiguredPathList -Value $env:VON_AI_CHAT_SESSION_SYNC_COPILOT_USER_ROOTS
    if (-not $userRoots -or $userRoots.Count -eq 0) {
        if ($env:APPDATA -and $env:APPDATA.ToString().Trim()) {
            $appdata = $env:APPDATA.ToString().Trim()
            $userRoots = @(
                (Join-Path $appdata 'Code\User'),
                (Join-Path $appdata 'Code - Insiders\User')
            )
        }
    }

    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($userRoot in @($userRoots)) {
        $expandedUserRoot = [Environment]::ExpandEnvironmentVariables($userRoot)
        if (-not (Test-Path -LiteralPath $expandedUserRoot)) {
            Write-LauncherLog "[ai-chat-session-sync] WARN: Copilot user root missing: $expandedUserRoot"
            continue
        }

        $workspaceStorage = Join-Path $expandedUserRoot 'workspaceStorage'
        if (Test-Path -LiteralPath $workspaceStorage) {
            Get-ChildItem -LiteralPath $workspaceStorage -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $chatSessions = Join-Path $_.FullName 'chatSessions'
                if (Test-Path -LiteralPath $chatSessions) {
                    $roots.Add((Get-NormalisedAbsolutePath -Path $chatSessions))
                }
            }
        }

        $emptyWindowChatSessions = Join-Path $expandedUserRoot 'globalStorage\emptyWindowChatSessions'
        if (Test-Path -LiteralPath $emptyWindowChatSessions) {
            $roots.Add((Get-NormalisedAbsolutePath -Path $emptyWindowChatSessions))
        }
    }

    foreach ($extraRoot in (Get-ConfiguredPathList -Value $env:VON_AI_CHAT_SESSION_SYNC_COPILOT_EXTRA_ROOTS)) {
        $expandedExtraRoot = [Environment]::ExpandEnvironmentVariables($extraRoot)
        if (-not (Test-Path -LiteralPath $expandedExtraRoot)) {
            Write-LauncherLog "[ai-chat-session-sync] WARN: extra Copilot root missing: $expandedExtraRoot"
            continue
        }
        $roots.Add((Get-NormalisedAbsolutePath -Path $expandedExtraRoot))
    }

    return @($roots | Sort-Object -Unique)
}

function Invoke-AiChatSessionSyncIfDue {
    <#
        Synchronise local AI programming chat-session exports into Von once per
        configured interval after the launcher has started the server.
        This is intentionally non-blocking and disabled by default.
    #>
    if (-not (Test-TruthySetting $env:VON_ENABLE_AI_CHAT_SESSION_SYNC)) { return }

    $userConceptId = if ($env:VON_USER_CONCEPT_ID -and $env:VON_USER_CONCEPT_ID.ToString().Trim()) {
        $env:VON_USER_CONCEPT_ID.ToString().Trim()
    }
    else {
        $null
    }
    if (-not $userConceptId) {
        Write-LauncherLog "[ai-chat-session-sync] WARN: VON_USER_CONCEPT_ID is not set; skipping."
        return
    }

    $intervalHours = 24
    try {
        if ($env:VON_AI_CHAT_SESSION_SYNC_INTERVAL_HOURS) {
            $intervalHours = [int]$env:VON_AI_CHAT_SESSION_SYNC_INTERVAL_HOURS
        }
    }
    catch { }
    if ($intervalHours -lt 1) { $intervalHours = 24 }

    $sentinel = Join-Path $RunDir 'last_ai_chat_session_sync_utc.txt'
    $last = $null
    if (Test-Path $sentinel) {
        try { $last = [DateTime]::Parse((Get-Content $sentinel -Raw).Trim()).ToUniversalTime() } catch { $last = $null }
    }
    $nowUtc = (Get-Date).ToUniversalTime()
    if ($last) {
        $hours = ($nowUtc - $last).TotalHours
        if ($hours -lt $intervalHours) { return }
    }

    $existingJob = Get-Job -Name 'von_ai_chat_session_sync' -ErrorAction SilentlyContinue | Where-Object { $_.State -in 'Running', 'NotStarted' }
    if ($existingJob) { return }

    $syncScript = Join-Path $Root 'scripts/sync_ai_programming_chat_sessions.py'
    if (-not (Test-Path $syncScript)) {
        Write-LauncherLog "[ai-chat-session-sync] WARN: sync script missing: $syncScript"
        return
    }

    $codexRoot = Get-AiChatSessionSyncCodexRoot
    $copilotRoots = @(Get-AiChatSessionSyncCopilotRoots)
    if (-not $codexRoot -and $copilotRoots.Count -eq 0) {
        Write-LauncherLog "[ai-chat-session-sync] WARN: no configured chat-session roots are available; skipping."
        return
    }

    $pdmExe = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    $outputJsonPath = Join-Path $Root 'data/sync_ai_programming_chat_sessions.latest.json'
    $rawOutputPath = Join-Path $RunDir 'ai_chat_session_sync_last_output.log'
    $liveLogPath = $CurrentLog
    Write-LauncherLog "[ai-chat-session-sync] Launching background sync (interval ${intervalHours}h codex=$([bool]$codexRoot) copilot_roots=$($copilotRoots.Count) live_log=$liveLogPath raw_log=$rawOutputPath)..."

    Start-Job -Name 'von_ai_chat_session_sync' -ScriptBlock {
        param($pdmExe, $root, $syncScript, $sentinelPath, $userConceptId, $codexRoot, $copilotRoots, $outputJsonPath, $rawOutputPath, $liveLogPath)
        function Write-AiChatSessionSyncProgress {
            param([string]$Message, [string]$LiveLogPath)
            if (-not $Message) { return }
            $line = "[ai-chat-session-sync] $Message"
            if ($LiveLogPath) {
                try { Add-Content -Path $LiveLogPath -Value $line -Encoding UTF8 } catch { }
            }
            Write-Host $line
        }

        function Write-AiChatSessionSyncRawLine {
            param([string]$Line, [string]$RawOutputPath)
            if ($null -eq $Line) { return }
            try { Add-Content -Path $RawOutputPath -Value $Line -Encoding UTF8 } catch { }
        }

        function Format-AiChatSessionSyncProgress {
            param([string]$Line)
            if (-not $Line) { return $null }
            try {
                $event = $Line | ConvertFrom-Json -ErrorAction Stop
            }
            catch {
                return $null
            }
            if (-not $event.event) { return $null }

            switch ($event.event) {
                'discovery_complete' {
                    return "discovered $($event.discovered) candidate chat-session file(s)."
                }
                'record_processed' {
                    if ($event.storage_object_written) {
                        $msg = "uploaded $($event.environment) session $($event.source_session_id) ($($event.index)/$($event.total))"
                        if ($event.storage_uri) { $msg += " -> $($event.storage_uri)" }
                        return $msg
                    }
                    if ($event.action -eq 'repair') {
                        return "repaired $($event.environment) session $($event.source_session_id) ($($event.index)/$($event.total))."
                    }
                    if ($event.action -eq 'unchanged' -and $event.index -eq 1) {
                        return "first discovered session is unchanged; sync is reusing existing ontology/file-copy state where possible."
                    }
                    return $null
                }
                'sync_completed' {
                    return "completed status=$($event.status) created=$($event.counters.created) updated=$($event.counters.updated) skipped=$($event.counters.skipped) failed=$($event.counters.failed)."
                }
                default {
                    return $null
                }
            }
        }

        try {
            Set-Location $root
            if (Test-Path $rawOutputPath) {
                try { Remove-Item -Path $rawOutputPath -Force -ErrorAction Stop } catch { }
            }
            $syncArgs = @('run', 'python', '-u', $syncScript, '--apply', '--user-concept-id', $userConceptId, '--output-json', $outputJsonPath)
            $syncArgs += '--emit-progress-ndjson'
            if ($codexRoot) {
                $syncArgs += @('--codex-root', $codexRoot)
            }
            foreach ($copilotRoot in @($copilotRoots)) {
                if ($copilotRoot) {
                    $syncArgs += @('--copilot-root', $copilotRoot)
                }
            }

            & $pdmExe @syncArgs 2>&1 | ForEach-Object {
                $line = $_.ToString()
                Write-AiChatSessionSyncRawLine -Line $line -RawOutputPath $rawOutputPath
                $progressLine = Format-AiChatSessionSyncProgress -Line $line
                if ($progressLine) {
                    Write-AiChatSessionSyncProgress -Message $progressLine -LiveLogPath $liveLogPath
                }
            }

            $exitCode = $LASTEXITCODE
            if ($exitCode -eq 0) {
                (Get-Date).ToUniversalTime().ToString('o') | Set-Content $sentinelPath
                Write-AiChatSessionSyncProgress -Message 'background sync finished successfully.' -LiveLogPath $liveLogPath
            }
            else {
                Write-AiChatSessionSyncProgress -Message ("ERROR exit={0} see {1}" -f $exitCode, $rawOutputPath) -LiveLogPath $liveLogPath
            }
        }
        catch {
            Write-AiChatSessionSyncProgress -Message ("ERROR: {0}" -f $_.Exception.Message) -LiveLogPath $liveLogPath
        }
    } -ArgumentList $pdmExe, $Root, $syncScript, $sentinel, $userConceptId, $codexRoot, $copilotRoots, $outputJsonPath, $rawOutputPath, $liveLogPath | Out-Null
}

function Get-VonHealthProbe($Port) {
    $hosts = @('127.0.0.1')
    if ($env:VON_HEALTH_HOSTS -and $env:VON_HEALTH_HOSTS.ToString().Trim()) {
        $configuredHosts = $env:VON_HEALTH_HOSTS.ToString().Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        if ($configuredHosts -and $configuredHosts.Count -gt 0) { $hosts = $configuredHosts }
    }
    elseif (Test-TruthySetting $env:VON_HEALTH_INCLUDE_LOCALHOST) {
        $hosts += 'localhost'
    }

    $httpTimeout = 2
    try {
        $envTimeout = [int]$env:VON_HEALTH_HTTP_TIMEOUT
        if ($envTimeout -gt 0 -and $envTimeout -lt 61) { $httpTimeout = $envTimeout }
    }
    catch { }
    $lastProbe = $null
    foreach ($h in $hosts) {
        $url = "http://${h}:$Port/health"
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $httpTimeout
            $payload = $null
            if ($resp.Content) {
                try { $payload = $resp.Content | ConvertFrom-Json -ErrorAction Stop }
                catch {
                    if ($HealthDebug) { Write-LauncherLog "Health attempt $url returned non-JSON content: $($_.Exception.Message)" }
                }
            }
            if ($resp.StatusCode -eq 200) {
                return [pscustomobject]@{
                    Healthy = $true
                    Url = $url
                    StatusCode = [int]$resp.StatusCode
                    Payload = $payload
                    Error = $null
                }
            }
            elseif ($HealthDebug) { Write-LauncherLog "Health attempt $url status=$($resp.StatusCode)" }
        }
        catch {
            if ($HealthDebug) { Write-LauncherLog "Health attempt failed $url : $($_.Exception.Message)" }
            $statusCode = $null
            try {
                if ($_.Exception.Response -and $null -ne $_.Exception.Response.StatusCode) {
                    $statusCode = [int]$_.Exception.Response.StatusCode
                }
            }
            catch { }
            $lastProbe = [pscustomobject]@{
                Healthy = $false
                Url = $url
                StatusCode = $statusCode
                Payload = $null
                Error = $_.Exception.Message
            }
        }
    }
    if ($lastProbe) { return $lastProbe }
    return [pscustomobject]@{
        Healthy = $false
        Url = $null
        StatusCode = $null
        Payload = $null
        Error = "No configured health host responded successfully."
    }
}

function Test-VonHealthEndpoint($Port) {
    # was Health-Check
    $probe = Get-VonHealthProbe -Port $Port
    return [bool]($probe -and $probe.Healthy)
}

function Write-VonVersionLog {
    param([int]$TargetPort = $Port)
    $probe = Get-VonHealthProbe -Port $TargetPort
    $version = $null
    try {
        if ($probe -and $probe.Payload -and $probe.Payload.version) {
            $version = [string]$probe.Payload.version
        }
    }
    catch { $version = $null }
    if ($version) {
        Write-LauncherLog "Von version: $version"
    }
    else {
        Write-LauncherLog "Von version: unavailable (/health did not report version)."
    }
}

function Test-AgentTestHealthProbe {
    param(
        [Parameter(Mandatory = $true)]$Probe,
        [int]$Port,
        [int]$ExpectedPid = 0,
        [switch]$LogFailure
    )
    if (-not $Probe -or -not $Probe.Healthy) {
        if ($LogFailure -and $Probe) {
            Write-LauncherLog ("Agent test health read-back not ready: status={0} error={1}" -f $Probe.StatusCode, $Probe.Error)
        }
        return $false
    }

    $payload = $Probe.Payload
    if (-not $payload) {
        if ($LogFailure) { Write-LauncherLog "Agent test health read-back rejected: /health returned no JSON payload." }
        return $false
    }
    if ($payload.agent_test_instance -ne $true) {
        if ($LogFailure) { Write-LauncherLog "Agent test health read-back rejected: agent_test_instance marker was not true." }
        return $false
    }

    $listener = if ($Port -gt 0) { Get-ListeningProcessByPort -Port $Port } else { $null }
    if ($Port -gt 0 -and -not $listener) {
        if ($LogFailure) { Write-LauncherLog "Agent test health read-back rejected: no listener remained on port $Port." }
        return $false
    }

    $payloadPid = 0
    try { if ($payload.pid) { $payloadPid = [int]$payload.pid } } catch { $payloadPid = 0 }
    if ($payloadPid -le 0) {
        if ($LogFailure) { Write-LauncherLog "Agent test health read-back rejected: /health payload did not include a valid pid." }
        return $false
    }
    if ($listener -and $payloadPid -ne $listener.Id) {
        if ($LogFailure) { Write-LauncherLog ("Agent test health read-back rejected: /health pid {0} did not match listener PID {1}." -f $payloadPid, $listener.Id) }
        return $false
    }
    return $true
}

function Test-VonLauncherHealthReady {
    param(
        [int]$Port,
        [int]$ExpectedPid = 0,
        [switch]$LogFailure
    )
    $probe = Get-VonHealthProbe -Port $Port
    if (Test-AgentTestInstance) {
        return (Test-AgentTestHealthProbe -Probe $probe -Port $Port -ExpectedPid $ExpectedPid -LogFailure:$LogFailure)
    }
    return [bool]($probe -and $probe.Healthy)
}

function Confirm-AgentTestHealthStable {
    param(
        [int]$Port,
        [int]$ExpectedPid = 0
    )
    $attempts = 4
    for ($i = 0; $i -lt $attempts; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Test-VonLauncherHealthReady -Port $Port -ExpectedPid $ExpectedPid -LogFailure:($i -eq ($attempts - 1)))) {
            return $false
        }
    }
    return $true
}

function Test-VonLogReadyShortcutAllowed {
    return -not (Test-AgentTestInstance)
}

function Test-VonPortListening($Port) {
    # was Test-PortListening
    try {
        $own = Get-ListeningProcessByPort -Port $Port
        return $null -ne $own
    }
    catch { return $false }
}

function Test-VonLogReady {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][string[]]$Patterns
    )
    if (-not (Test-Path $LogPath)) { return $false }
    try {
        $tail = Get-Content $LogPath -Tail 400 -ErrorAction Stop
        foreach ($p in $Patterns) {
            if ($tail -match [Regex]::Escape($p)) { return $true }
        }
    }
    catch { return $false }
    return $false
}

function Get-ActualServerPort {
    <#
    .SYNOPSIS
    Scans the server log to detect if the server fell back to a different port.
    Returns the actual port the server is using, or $null if not yet determinable.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][int]$RequestedPort
    )
    if (-not (Test-Path $LogPath)) { return $null }
    try {
        $tail = Get-Content $LogPath -Tail 100 -ErrorAction Stop
        # Look for: [INFO] Port 5000 is in use. The UI will start on port 5001 instead.
        foreach ($line in $tail) {
            if ($line -match '\[INFO\] Port (\d+) is in use\. The UI will start on port (\d+) instead\.') {
                return [int]$Matches[2]
            }
            # Also check for "Serving on http://127.0.0.1:XXXX" as definitive
            if ($line -match 'Serving on http://[^:]+:(\d+)') {
                return [int]$Matches[1]
            }
        }
    }
    catch { return $null }
    return $null
}

function Get-CurrentPowerShellExecutable {
    try {
        $current = Get-Process -Id $PID -ErrorAction Stop
        if ($current.Path) { return $current.Path }
    }
    catch { }

    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwsh -and $pwsh.Source) { return $pwsh.Source }

    $powershell = Get-Command powershell -ErrorAction SilentlyContinue
    if ($powershell -and $powershell.Source) { return $powershell.Source }

    return $null
}

function Get-RecordedPortFromPidFile {
    param(
        [string]$Path = $PidFile,
        [int]$FallbackPort = $Port
    )

    if (-not $Path -or -not (Test-Path $Path)) { return $FallbackPort }
    try {
        $content = Get-Content $Path -ErrorAction Stop
        foreach ($line in $content) {
            if ($line -match '^PORT=([0-9]+)$') {
                return [int]$Matches[1]
            }
        }
    }
    catch { }
    return $FallbackPort
}

function Open-VonBrowserIfNeeded {
    param([int]$TargetPort = $Port)

    $shouldOpen = $false
    if ($script:ForceBrowser) { $shouldOpen = $true }
    elseif (-not (Test-Path $script:BrowserSentinel)) { $shouldOpen = $true }

    if (-not $shouldOpen) {
        Write-LauncherLog "Browser already opened previously (use -ForceBrowser to open again)."
        return $false
    }

    try {
        $url = "http://localhost:$TargetPort/"
        # Try to open with Chrome (Beta first if -ChromeBeta, otherwise Beta has priority for MCP debugging)
        $chromeBetaPaths = @(
            "${env:ProgramFiles}\Google\Chrome Beta\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome Beta\Application\chrome.exe",
            "${env:LocalAppData}\Google\Chrome Beta\Application\chrome.exe"
        )
        $chromeStablePaths = @(
            "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "${env:LocalAppData}\Google\Chrome\Application\chrome.exe"
        )
        # If -ChromeBeta is set, only try Beta; otherwise try Beta first then stable
        if ($script:ChromeBeta) {
            $chromePaths = $chromeBetaPaths
        }
        else {
            $chromePaths = $chromeBetaPaths + $chromeStablePaths
        }
        $chromeFound = $false
        $browserUsed = "default browser"
        foreach ($chromePath in $chromePaths) {
            if (Test-Path $chromePath) {
                Start-Process $chromePath -ArgumentList $url | Out-Null
                $chromeFound = $true
                $browserUsed = "Chrome ($chromePath)"
                break
            }
        }
        if (-not $chromeFound) {
            # Fallback to default browser if Chrome not found
            Start-Process $url | Out-Null
        }
        if (-not (Test-Path $script:BrowserSentinel)) { Set-Content $script:BrowserSentinel (Get-Date).ToString('o') }
        if ($script:ForceBrowser) { Write-LauncherLog "Opened browser (forced): $browserUsed" } else { Write-LauncherLog "Opened browser (first launch): $browserUsed" }
        return $true
    }
    catch {
        Write-LauncherLog "Browser open attempt failed: $($_.Exception.Message)"
        return $false
    }
}

function Report-WorkflowPurityCheckStatus {
    if (Test-Path $script:WorkflowPurityPidFile) {
        $pidContent = Get-Content $script:WorkflowPurityPidFile -Raw -ErrorAction SilentlyContinue
        if ($pidContent -match 'PID=([0-9]+)') {
            $runningPid = [int]$Matches[1]
            if (Get-Process -Id $runningPid -ErrorAction SilentlyContinue) {
                Write-LauncherLog "Workflow purity check still running from a previous start (PID=$runningPid)."
                return
            }
        }
        Remove-Item $script:WorkflowPurityPidFile -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path $script:WorkflowPurityResultFile)) { return }

    try {
        $result = Get-Content $script:WorkflowPurityResultFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        Write-LauncherLog "WARN: Failed to read workflow purity result receipt: $($_.Exception.Message)"
        return
    }

    $completedAtUtc = ''
    if ($null -ne $result.completed_at_utc) {
        if ($result.completed_at_utc -is [datetime]) {
            $completedAtUtc = $result.completed_at_utc.ToUniversalTime().ToString('o')
        }
        else {
            $completedAtUtc = [string]$result.completed_at_utc
        }
    }
    $resultExitCode = if ($null -ne $result.exit_code) { [int]$result.exit_code } else { 0 }
    $resultStatus = if ($null -ne $result.status) { [string]$result.status } else { '' }
    $completionKey = "{0}|{1}|{2}" -f $completedAtUtc, $resultExitCode, $resultStatus
    $lastReported = ''
    if (Test-Path $script:WorkflowPurityReportedFile) {
        $lastReported = (Get-Content $script:WorkflowPurityReportedFile -Raw -ErrorAction SilentlyContinue).Trim()
    }
    if ($completionKey -eq $lastReported) { return }

    if ($resultExitCode -eq 0) {
        Set-Content $script:WorkflowPurityReportedFile $completionKey
        return
    }

    Write-LauncherLog "WARN: Previous workflow purity check failed (status=$resultStatus exit=$resultExitCode)."
    if ($result.log_path -or $result.err_log_path) {
        Write-LauncherLog "WARN: Review purity logs: $($result.log_path) ; stderr: $($result.err_log_path)"
    }
    if ($result.message) {
        Write-LauncherLog "[purity-check:last] $($result.message)"
    }

    $previewLines = @()
    foreach ($path in @($result.log_path, $result.err_log_path)) {
        if (-not $path) { continue }
        if (-not (Test-Path $path)) { continue }
        try {
            foreach ($line in (Get-Content $path -ErrorAction Stop | Where-Object { $_ -and $_.Trim() } | Select-Object -First 5)) {
                $previewLines += [string]$line
            }
        }
        catch { }
    }
    foreach ($line in ($previewLines | Select-Object -Unique -First 5)) {
        Write-LauncherLog "[purity-check:last] $line"
    }

    Set-Content $script:WorkflowPurityReportedFile $completionKey
}

function Start-WorkflowPurityCheckNonBlocking {
    $purityScript = Join-Path $script:Root 'scripts/check_workflow_purity.py'
    if (-not (Test-Path $purityScript)) { return $false }

    if (Test-Path $script:WorkflowPurityPidFile) {
        $pidContent = Get-Content $script:WorkflowPurityPidFile -Raw -ErrorAction SilentlyContinue
        if ($pidContent -match 'PID=([0-9]+)') {
            $existingPid = [int]$Matches[1]
            if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
                Write-LauncherLog "Workflow purity check already running (PID=$existingPid)."
                return $false
            }
        }
        Remove-Item $script:WorkflowPurityPidFile -Force -ErrorAction SilentlyContinue
    }

    if ($env:VON_FORCE_WORKFLOW_PURITY_CHECK -match '^(1|true|TRUE|yes|YES|y|Y|on|ON)$') {
        Write-LauncherLog "Workflow purity check forced by VON_FORCE_WORKFLOW_PURITY_CHECK."
    }
    elseif (Test-Path $script:WorkflowPurityResultFile) {
        try {
            $previousResult = Get-Content $script:WorkflowPurityResultFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ([int]$previousResult.exit_code -eq 0 -and [string]$previousResult.status -eq 'passed') {
                $completedAtUtc = [datetime]::Parse([string]$previousResult.completed_at_utc).ToUniversalTime()
                $ageSeconds = ((Get-Date).ToUniversalTime() - $completedAtUtc).TotalSeconds
                if ($ageSeconds -ge 0 -and $ageSeconds -lt $script:WorkflowPurityTtlSeconds) {
                    Write-LauncherLog "Skipping workflow purity check; last successful pass is less than $($script:WorkflowPurityTtlSeconds)s old."
                    return $false
                }
            }
        }
        catch { }
    }

    $pdm = if (Test-Path (Join-Path $script:Root '.venv\Scripts\pdm.exe')) { Join-Path $script:Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    $venvPython = Join-Path $script:Root '.venv\Scripts\python.exe'
    $purityTimestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $purityLog = Join-Path $script:LogsDir "workflow_purity_${purityTimestamp}.log"
    $purityErrLog = "$purityLog.err"
    $powerShellExe = Get-CurrentPowerShellExecutable

    $purityExe = $null
    $purityArgs = @()
    $launchMode = 'pdm-fallback'
    if (Test-Path $venvPython) {
        $purityExe = $venvPython
        $purityArgs = @($purityScript, '--quiet-on-pass')
        $launchMode = 'direct-python'
    }
    else {
        $purityExe = $pdm
        $purityArgs = @('run', 'python', $purityScript, '--quiet-on-pass')
    }

    if (-not $powerShellExe) {
        Write-LauncherLog "WARN: Could not resolve PowerShell executable for workflow purity runner."
        return $false
    }

    $purityArgLiteral = if ($purityArgs.Count -gt 0) {
        "@(" + (($purityArgs | ForEach-Object { ConvertTo-PowerShellSingleQuotedLiteral $_ }) -join ', ') + ")"
    }
    else {
        "@()"
    }
    $wrapperContent = @"
`$ErrorActionPreference = 'Stop'
`$purityExe = $(ConvertTo-PowerShellSingleQuotedLiteral $purityExe)
`$purityArgs = $purityArgLiteral
`$workingDirectory = $(ConvertTo-PowerShellSingleQuotedLiteral $script:Root)
`$purityLog = $(ConvertTo-PowerShellSingleQuotedLiteral $purityLog)
`$purityErrLog = $(ConvertTo-PowerShellSingleQuotedLiteral $purityErrLog)
`$resultFile = $(ConvertTo-PowerShellSingleQuotedLiteral $script:WorkflowPurityResultFile)
`$pidFile = $(ConvertTo-PowerShellSingleQuotedLiteral $script:WorkflowPurityPidFile)
`$startedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
`$exitCode = -1
`$status = 'failed_to_launch'
`$message = `$null
try {
    if (Test-Path `$purityLog) { Remove-Item `$purityLog -Force -ErrorAction SilentlyContinue }
    if (Test-Path `$purityErrLog) { Remove-Item `$purityErrLog -Force -ErrorAction SilentlyContinue }
    Push-Location `$workingDirectory
    try {
        & `$purityExe @purityArgs 1> `$purityLog 2> `$purityErrLog
        if (`$null -ne `$LASTEXITCODE) {
            `$exitCode = [int]`$LASTEXITCODE
        }
        elseif (`$?) {
            `$exitCode = 0
        }
        else {
            `$exitCode = 1
        }
    }
    finally {
        Pop-Location
    }
    if (`$exitCode -eq 0) {
        `$status = 'passed'
    }
    else {
        `$status = 'failed'
    }
}
catch {
    `$message = `$_.Exception.Message
}
finally {
    `$completedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    `$result = [ordered]@{
        started_at_utc = `$startedAtUtc
        completed_at_utc = `$completedAtUtc
        exit_code = `$exitCode
        status = `$status
        log_path = `$purityLog
        err_log_path = `$purityErrLog
        message = `$message
    }
    `$result | ConvertTo-Json -Depth 5 | Set-Content `$resultFile -Encoding UTF8
    Remove-Item `$pidFile -Force -ErrorAction SilentlyContinue
}
"@

    try {
        Set-Content -Path $script:WorkflowPurityRunnerScript -Value $wrapperContent -Encoding UTF8
        $proc = Start-Process -FilePath $powerShellExe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script:WorkflowPurityRunnerScript) -WorkingDirectory $script:Root -PassThru -WindowStyle Hidden
        $startIso = (Get-Date).ToString('o')
        Set-Content $script:WorkflowPurityPidFile "PID=$($proc.Id)`nSTART=$startIso"
        Write-LauncherLog "Started workflow purity check in background (PID=$($proc.Id), mode=$launchMode). Logs: $purityLog ; stderr: $purityErrLog"
        return $true
    }
    catch {
        Write-LauncherLog "WARN: Failed to launch workflow purity check in background: $($_.Exception.Message)"
        return $false
    }
}

function Invoke-VonHealthyStartFollowUps {
    param([int]$TargetPort = $Port)

    if (-not $script:NoBrowser) {
        Open-VonBrowserIfNeeded -TargetPort $TargetPort | Out-Null
    }
    if (Test-AgentTestInstance) {
        Write-LauncherLog "Agent test mode: skipping workflow purity background check."
        return
    }
    Start-WorkflowPurityCheckNonBlocking | Out-Null
}

function Start-RagWorker {
    if (Test-Path $RagPidFile) {
        $pidContent = Get-Content $RagPidFile -Raw -ErrorAction SilentlyContinue
        if ($pidContent -match 'PID=([0-9]+)') {
            $oldPid = [int]$Matches[1]
            if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
                Write-LauncherLog "RAG Worker already running (PID=$oldPid)."
                return
            }
        }
        Remove-Item $RagPidFile -Force -ErrorAction SilentlyContinue
    }
    # Remove stale worker processes left by old wrapper-based launches.
    Stop-PythonProcessesByScript -ScriptRelativePath 'src/backend/utilities/rag_indexing_worker.py' -Label 'RAG Worker'

    Write-LauncherLog "Starting RAG Indexing Worker..."
    $pythonExe = Get-ProjectPythonExecutable
    $ragErrLogFile = "$RagLogFile.err"
    $proc = Start-Process -FilePath $pythonExe -ArgumentList @('-u', 'src/backend/utilities/rag_indexing_worker.py') -WorkingDirectory $Root -PassThru -WindowStyle Hidden -RedirectStandardOutput $RagLogFile -RedirectStandardError $ragErrLogFile

    $startIso = (Get-Date).ToString('o')
    Set-Content $RagPidFile "PID=$($proc.Id)`nSTART=$startIso"
    Write-LauncherLog "RAG Worker started (PID=$($proc.Id)). Logs: $RagLogFile, $ragErrLogFile"
}

function Stop-RagWorker {
    $pidToKill = 0
    $content = Get-Content $RagPidFile -Raw -ErrorAction SilentlyContinue
    if ($content -match 'PID=([0-9]+)') {
        $pidToKill = [int]$Matches[1]
        Write-LauncherLog "Stopping RAG Worker (PID=$pidToKill)..."
        $stopped = Stop-ProcessTreeWithEscalation -ProcessId $pidToKill
        if (-not $stopped) {
            Write-LauncherLog "WARN: RAG Worker PID=$pidToKill may still be running."
        }
    }
    Stop-PythonProcessesByScript -ScriptRelativePath 'src/backend/utilities/rag_indexing_worker.py' -Label 'RAG Worker' -ExcludePid $pidToKill
    Remove-Item $RagPidFile -Force -ErrorAction SilentlyContinue
}

function Start-ConceptIndexWorker {
    if (Test-Path $ConceptIndexPidFile) {
        $pidContent = Get-Content $ConceptIndexPidFile -Raw -ErrorAction SilentlyContinue
        if ($pidContent -match 'PID=([0-9]+)') {
            $oldPid = [int]$Matches[1]
            if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
                Write-LauncherLog "Concept Index Worker already running (PID=$oldPid)."
                return
            }
        }
        Remove-Item $ConceptIndexPidFile -Force -ErrorAction SilentlyContinue
    }
    # Remove stale worker processes left by old wrapper-based launches.
    Stop-PythonProcessesByScript -ScriptRelativePath 'src/backend/utilities/concept_index_worker.py' -Label 'Concept Index Worker'

    Write-LauncherLog "Starting Concept Index Worker..."
    $pythonExe = Get-ProjectPythonExecutable
    $conceptErrLogFile = "$ConceptIndexLogFile.err"
    $proc = Start-Process -FilePath $pythonExe -ArgumentList @('-u', 'src/backend/utilities/concept_index_worker.py') -WorkingDirectory $Root -PassThru -WindowStyle Hidden -RedirectStandardOutput $ConceptIndexLogFile -RedirectStandardError $conceptErrLogFile

    $startIso = (Get-Date).ToString('o')
    Set-Content $ConceptIndexPidFile "PID=$($proc.Id)`nSTART=$startIso"
    Write-LauncherLog "Concept Index Worker started (PID=$($proc.Id)). Logs: $ConceptIndexLogFile, $conceptErrLogFile"
}

function Stop-ConceptIndexWorker {
    $pidToKill = 0
    $content = Get-Content $ConceptIndexPidFile -Raw -ErrorAction SilentlyContinue
    if ($content -match 'PID=([0-9]+)') {
        $pidToKill = [int]$Matches[1]
        Write-LauncherLog "Stopping Concept Index Worker (PID=$pidToKill)..."
        $stopped = Stop-ProcessTreeWithEscalation -ProcessId $pidToKill
        if (-not $stopped) {
            Write-LauncherLog "WARN: Concept Index Worker PID=$pidToKill may still be running."
        }
    }
    Stop-PythonProcessesByScript -ScriptRelativePath 'src/backend/utilities/concept_index_worker.py' -Label 'Concept Index Worker' -ExcludePid $pidToKill
    Remove-Item $ConceptIndexPidFile -Force -ErrorAction SilentlyContinue
}

function Invoke-VonServerStartProcessCleanup {
    if (Test-AgentTestInstance) {
        Write-LauncherLog "Agent test mode: preserving other Von server processes; only port $Port is managed."
        return
    }
    Stop-PythonProcessesByScript -ScriptRelativePath 'src/workflows/von/main.py' -Label 'Von Server'
}

function Invoke-VonStartupBackgroundServices {
    if (Test-AgentTestInstance) {
        Write-LauncherLog "Agent test mode: skipping startup maintenance, sync, and shared workers."
        return
    }

    # Trigger daily backup (non-blocking) if due
    try { Invoke-DailyBackupIfDue } catch { Write-LauncherLog "[daily-backup] ERROR (scheduling failed): $($_.Exception.Message)" }
    # Trigger test DB refresh (non-blocking) if due
    try { Invoke-TestDbRefreshIfDue } catch { Write-LauncherLog "[test-db-refresh] ERROR (scheduling failed): $($_.Exception.Message)" }
    # Trigger AI chat-session sync (non-blocking) if due
    try { Invoke-AiChatSessionSyncIfDue } catch { Write-LauncherLog "[ai-chat-session-sync] ERROR (scheduling failed): $($_.Exception.Message)" }

    # Start RAG Worker
    try { Start-RagWorker } catch { Write-LauncherLog "[rag-worker] ERROR: $($_.Exception.Message)" }

    # Start Concept Index Worker
    try { Start-ConceptIndexWorker } catch { Write-LauncherLog "[concept-index-worker] ERROR: $($_.Exception.Message)" }
}

function Stop-VonSharedWorkersForCurrentMode {
    if (Test-AgentTestInstance) {
        Write-LauncherLog "Agent test mode: preserving shared RAG and concept-index workers."
        return
    }

    Stop-RagWorker
    Stop-ConceptIndexWorker
}

function Sync-PidFileToListener {
    # Align PID file with actual port-owning python process if mismatch
    $listener = Get-ListeningProcessByPort -Port $Port
    if (-not $listener) { return }
    $listenerPid = $listener.Id
    $currentPid = $null
    if (Test-Path $PidFile) {
        $pidLine = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($pidLine -match 'PID=([0-9]+)') { $currentPid = [int]$Matches[1] }
    }
    if ($currentPid -eq $listenerPid -and $currentPid) { return }
    # Additional verification: ensure command line references our main script
    $cmdLine = Get-ProcessCommandLine -ProcessId $listenerPid
    if ($cmdLine -and $cmdLine -like '*src/workflows/von/main.py*') {
        Write-PidFile $listenerPid
        Write-LauncherLog ("Synchronized PID file to listener PID={0}" -f $listenerPid)
    }
}

function Start-VonServer {
    param([switch]$RestartTakeover)
    # was Start-Von
    Report-WorkflowPurityCheckStatus
    # Set production database name first (before any checks)
    # This ensures we default to production database and only throw if user explicitly wants test mode
    if (-not $env:VON_DB_NAME -or $env:VON_DB_NAME -eq 'test_von_db') {
        $env:VON_DB_NAME = 'von_db'
    }

    # Safety check: prevent running server with test database (only if explicitly set to test)
    if ($env:VON_DB_NAME -eq 'test_von_db') {
        Write-Host "ERROR: Cannot start server with test database (VON_DB_NAME=test_von_db)." -ForegroundColor Red
        Write-Host "This prevents accidental data corruption from running production server against test data." -ForegroundColor Yellow
        Write-Host "To fix: `$env:VON_DB_NAME = 'von_db'" -ForegroundColor Green
        throw "Server start blocked: test database detected"
    }

    $existing = Get-ExistingProcess
    if ($existing) {
        $existingPort = Get-RecordedPortFromPidFile -FallbackPort $Port
        Write-LauncherLog "Already running (PID=$($existing.Id)). Use .\run.ps1 stop or restart."
        if (-not $script:NoBrowser) {
            Open-VonBrowserIfNeeded -TargetPort $existingPort | Out-Null
        }
        Write-VonVersionLog -TargetPort $existingPort
        return
    }
    if ($RestartTakeover) {
        $listenerToTakeOver = Get-ListeningProcessByPort -Port $Port
        if ($listenerToTakeOver) {
            if (-not (Invoke-RestartPortTakeover -Listener $listenerToTakeOver)) { return }
        }
    }
    # If no valid PID file process, but port already has a listener, treat that as running and sync PID file
    $listener = Get-ListeningProcessByPort -Port $Port
    if ($listener) {
        $currentPidInFile = $null
        if (Test-Path $PidFile) {
            $pidLine = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($pidLine -match 'PID=([0-9]+)') { $currentPidInFile = [int]$Matches[1] }
        }
        if (-not $currentPidInFile) {
            if ((Test-AgentTestInstance) -and -not (Test-IsVonMainProcess -ProcessId $listener.Id)) {
                Write-LauncherLog ("Agent test mode: port {0} is owned by non-Von PID={1}; aborting instead of adopting it." -f $Port, $listener.Id)
                return
            }
            Write-LauncherLog ("Port {0} already in use by PID={1}; assuming server already running (untracked)." -f $Port, $listener.Id)
            Write-PidFile $listener.Id
            if (-not $script:NoBrowser) {
                Open-VonBrowserIfNeeded -TargetPort $Port | Out-Null
            }
            Write-VonVersionLog -TargetPort $Port
            return
        }
        elseif ($currentPidInFile -ne $listener.Id) {
            if ((Test-AgentTestInstance) -and -not (Test-IsVonMainProcess -ProcessId $listener.Id)) {
                Write-LauncherLog ("Agent test mode: port {0} is owned by non-Von PID={1}; refusing to replace PID file PID={2}." -f $Port, $listener.Id, $currentPidInFile)
                return
            }
            Write-LauncherLog ("Port {0} owned by PID={1} but PID file had PID={2}; updating PID file to listener." -f $Port, $listener.Id, $currentPidInFile)
            Write-PidFile $listener.Id
            if (-not $script:NoBrowser) {
                Open-VonBrowserIfNeeded -TargetPort $Port | Out-Null
            }
            Write-VonVersionLog -TargetPort $Port
            return
        }
    }
    $token = Read-AdminToken
    # Export token for child process
    $env:VON_ADMIN_TOKEN = $token
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONPATH = $Root
    # Force Python to use UTF-8 for IO to avoid platform encoding issues
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    # Prevent Python main process from attempting to open an additional browser tab; launcher manages this.
    $env:VON_SKIP_BROWSER_LAUNCH = '1'
    # Clear stale server script processes so restart/start cannot accumulate orphaned wrappers.
    Invoke-VonServerStartProcessCleanup

    $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    $projectPython = Get-ProjectPythonExecutable

    $serverExe = $null
    $serverArgs = @()
    $launchMode = 'direct-python'
    if ($projectPython -and $projectPython -ne 'python') {
        $serverExe = $projectPython
        $serverArgs = @('-u', 'src/workflows/von/main.py', '--port', "$Port")
    }
    else {
        $serverExe = $pdm
        $serverArgs = @('run', 'python', '-u', 'src/workflows/von/main.py', '--port', "$Port")
        $launchMode = 'pdm-fallback'
    }

    # Launch background with explicit stdout/stderr redirection.
    $requestedStartPort = $Port
    Write-LauncherLog "Starting Von server on port $Port (mode=$launchMode)..."
    $serverErrLog = "$NewLog.err"
    if (Test-Path $NewLog) { Remove-Item $NewLog -Force -ErrorAction SilentlyContinue }
    if (Test-Path $serverErrLog) { Remove-Item $serverErrLog -Force -ErrorAction SilentlyContinue }
    try {
        $usePythonDetachedLauncher = (-not $IsWindows) -and ($PSVersionTable.PSEdition -ne 'Desktop') -and $projectPython -and ($projectPython -ne 'python') -and (Test-Path $projectPython)
        if ($usePythonDetachedLauncher) {
            $launcherPayload = @{
                executable = $serverExe
                args = $serverArgs
                cwd = $Root
                stdout = $NewLog
                stderr = $serverErrLog
            } | ConvertTo-Json -Compress
            $launcherScript = @'
import json
import subprocess
import sys

payload = json.loads(sys.stdin.read())
stdout = open(payload["stdout"], "ab", buffering=0)
stderr = open(payload["stderr"], "ab", buffering=0)
process = subprocess.Popen(
    [payload["executable"], *payload["args"]],
    cwd=payload["cwd"],
    stdin=subprocess.DEVNULL,
    stdout=stdout,
    stderr=stderr,
    close_fds=True,
    start_new_session=True,
)
print(process.pid)
'@
            $launchedPidText = ($launcherPayload | & $projectPython -c $launcherScript | Select-Object -Last 1)
            if (-not $launchedPidText -or $launchedPidText -notmatch '^\d+$') {
                throw "launcher did not return a child PID"
            }
            $proc = Get-Process -Id ([int]$launchedPidText) -ErrorAction Stop
        }
        else {
            $startProcessArgs = @{
                FilePath = $serverExe
                ArgumentList = $serverArgs
                WorkingDirectory = $Root
                PassThru = $true
                RedirectStandardOutput = $NewLog
                RedirectStandardError = $serverErrLog
            }
            if ($IsWindows -or $PSVersionTable.PSEdition -eq 'Desktop') {
                $startProcessArgs['WindowStyle'] = 'Hidden'
            }
            $proc = Start-Process @startProcessArgs
        }
    }
    catch {
        Write-LauncherLog "ERROR: Failed to launch server process ($launchMode): $($_.Exception.Message)"
        return
    }
    if (-not (Test-Path $NewLog)) { New-Item -ItemType File -Path $NewLog -Force | Out-Null }
    Write-LauncherLog "Launched PID=$($proc.Id). Logs: $NewLog ; stderr: $serverErrLog"
    Write-PidFile $proc.Id

    # Update current log pointer: prefer a hard link so the "current" file
    # refers to the same underlying file as the timestamped log. This ensures
    # that readers (e.g. tail) see live writes. Fallback to copy if hard link
    # creation is not permitted (cross-volume, permissions, etc.).
    try {
        if (Test-Path $CurrentLog) { Remove-Item $CurrentLog -Force -ErrorAction SilentlyContinue }
        New-Item -ItemType HardLink -Path $CurrentLog -Value $NewLog -ErrorAction Stop | Out-Null
        Write-LauncherLog "Created hard link for current log -> $NewLog"
    }
    catch {
        Write-LauncherLog "Hard link creation failed: $($_.Exception.Message). Falling back to copy."
        Copy-Item $NewLog $CurrentLog -Force -ErrorAction SilentlyContinue
    }
    Invoke-VonLogRotation
    # Poll health
    if ($SkipHealth) {
        Write-LauncherLog "Skipping health wait (use .\\run.ps1 status to check)."
    }
    else {
        $maxAttempts = [int]($HealthTimeoutSec * 2) # 500ms intervals (phase 1)
        $attempt = 0; $healthy = $false; $listeningLogged = $false; $portFallbackDetected = $false
        while ($attempt -lt $maxAttempts) {
            Start-Sleep -Milliseconds 500
            # Check if server fell back to a different port
            if (-not $portFallbackDetected) {
                $actualPort = Get-ActualServerPort -LogPath $CurrentLog -RequestedPort $Port
                if ($actualPort -and $actualPort -ne $Port) {
                    $portFallbackDetected = $true
                    if (Test-AgentTestInstance) {
                        Write-LauncherLog ("ERROR: Agent test mode refused port fallback from {0} to {1}; stop the occupying process or pass an unused -Port." -f $requestedStartPort, $actualPort)
                        try { Stop-ProcessTreeWithEscalation -ProcessId $proc.Id | Out-Null } catch { }
                        Remove-PidFile
                        return
                    }
                    Write-Host ""
                    Write-Host "================================================================================" -ForegroundColor Yellow
                    Write-Host "  WARNING: Port $Port was in use! Server started on port $actualPort instead." -ForegroundColor Yellow
                    Write-Host "  URL: http://localhost:$actualPort/" -ForegroundColor Yellow
                    Write-Host "================================================================================" -ForegroundColor Yellow
                    Write-Host ""
                    Write-LauncherLog "PORT FALLBACK: Requested port $Port was in use; server is running on port $actualPort"
                    $Port = $actualPort
                }
            }
            # Early crash detection: if the launched process has exited and the port never began listening, abort fast
            if (-not $listeningLogged) {
                try {
                    $procCheck = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
                }
                catch { $procCheck = $null }
                if (-not $procCheck) {
                    Write-LauncherLog "ERROR: Server process exited early before listening on port $Port. Showing last 40 log lines:"
                    $logTail = @()
                    $errTail = @()
                    if (Test-Path $CurrentLog) {
                        try {
                            $logTail = Get-Content $CurrentLog -Tail 40
                            $logTail | ForEach-Object { Write-Host $_ }
                        }
                        catch { Write-LauncherLog "(Log tail unavailable: $($_.Exception.Message))" }
                    }
                    if (Test-Path $serverErrLog) {
                        try {
                            Write-LauncherLog "Last 40 stderr log lines:"
                            $errTail = Get-Content $serverErrLog -Tail 40
                            $errTail | ForEach-Object { Write-Host $_ }
                        }
                        catch { Write-LauncherLog "(stderr tail unavailable: $($_.Exception.Message))" }
                    }

                    # Auto-repair logic for missing dependencies
                    if (-not $script:RepairAttempted) {
                        $logText = ($logTail + $errTail) -join "`n"
                        if ($logText -match "ModuleNotFoundError" -or $logText -match "ImportError") {
                            Write-LauncherLog "Detected missing dependencies. Attempting auto-repair..."
                            $script:RepairAttempted = $true

                            $setupScript = Join-Path $Root "setup_py.ps1"
                            if (Test-Path $setupScript) {
                                & $setupScript
                                if ($LASTEXITCODE -eq 0) {
                                    Write-LauncherLog "Repair completed successfully. Retrying server start..."
                                    Start-VonServer
                                    return
                                }
                                else {
                                    Write-LauncherLog "Repair failed."
                                }
                            }
                        }
                    }

                    Write-LauncherLog "Aborting start. (Use -HealthDebug for verbose retries)"
                    return
                }
            }
            if (-not $listeningLogged -and (Test-VonPortListening $Port)) {
                Write-LauncherLog "Port $Port is listening; waiting for /health..."
                $listeningLogged = $true
            }
            if (Test-VonLauncherHealthReady -Port $Port -ExpectedPid $proc.Id) { $healthy = $true; break }
            elseif ($HealthDebug -and ($attempt % 4 -eq 0)) { Write-LauncherLog ("Health not ready yet (attempt {0}/{1})" -f $attempt, $maxAttempts) }
            if (-not $DisableLogReady -and $ReadyLogPatterns -and (Test-VonLogReady -LogPath $CurrentLog -Patterns $ReadyLogPatterns)) {
                if (Test-VonLogReadyShortcutAllowed) {
                    Write-LauncherLog "Detected readiness log pattern; marking healthy (log shortcut)."
                    $healthy = $true; break
                }
                elseif ($HealthDebug -and ($attempt % 4 -eq 0)) {
                    Write-LauncherLog "Detected readiness log pattern; Agent test mode still requires /health marker read-back."
                }
            }
            $attempt++
        }
        if (-not $healthy -and $listeningLogged -and $HealthGraceSec -gt 0) {
            Write-LauncherLog "Extending wait up to ${HealthGraceSec}s for /health (phase 2)..."
            $graceAttempts = [int]($HealthGraceSec * 2)
            $g = 0
            while ($g -lt $graceAttempts -and -not $healthy) {
                Start-Sleep -Milliseconds 500
                if (Test-VonLauncherHealthReady -Port $Port -ExpectedPid $proc.Id) { $healthy = $true; break }
                elseif ($HealthDebug -and ($g % 10 -eq 0)) { Write-LauncherLog ("Grace wait health not ready ({0}s/{1}s)" -f ([int]($g / 2)), $HealthGraceSec) }
                if (-not $DisableLogReady -and $ReadyLogPatterns -and (Test-VonLogReady -LogPath $CurrentLog -Patterns $ReadyLogPatterns)) {
                    if (Test-VonLogReadyShortcutAllowed) {
                        Write-LauncherLog "Detected readiness log pattern during grace; marking healthy (log shortcut)."
                        $healthy = $true; break
                    }
                    elseif ($HealthDebug -and ($g % 10 -eq 0)) {
                        Write-LauncherLog "Detected readiness log pattern during grace; Agent test mode still requires /health marker read-back."
                    }
                }
                if (($g % 10) -eq 0) {
                    # every 5s
                    $elapsed = [int]($g / 2)
                    Write-LauncherLog "Still waiting for /health... ${elapsed}s/${HealthGraceSec}s grace"
                }
                $g++
            }
        }
        if ($healthy -and (Test-AgentTestInstance) -and -not (Confirm-AgentTestHealthStable -Port $Port -ExpectedPid $proc.Id)) {
            $healthy = $false
            $script:VonStartFailed = $true
            Write-LauncherLog "ERROR: Agent test /health marker read-back did not remain stable after initial readiness."
            try {
                $procCheck = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
                if (-not $procCheck) { Remove-PidFile }
            }
            catch { Remove-PidFile }
        }
        if ($healthy) {
            Write-LauncherLog "Server healthy (http://localhost:$Port)"
            Write-LauncherLog (Get-MongoConnectionSummary -Port $Port)
            Invoke-VonHealthyStartFollowUps -TargetPort $Port
        }
        elseif ($listeningLogged) {
            if (Test-AgentTestInstance) {
                $script:VonStartFailed = $true
                Write-LauncherLog "ERROR: Agent test port is listening but /health marker read-back did not validate in ${HealthTimeoutSec + $HealthGraceSec}s."
            }
            else {
                Write-LauncherLog "WARNING: Port is listening but /health did not respond in ${HealthTimeoutSec + $HealthGraceSec}s; continuing (service may still be initializing)."
            }
            Write-LauncherLog (Get-MongoConnectionSummary -Port $Port)
        }
        else {
            if (Test-AgentTestInstance) {
                $script:VonStartFailed = $true
                Write-LauncherLog "ERROR: Agent test server not healthy after initial ${HealthTimeoutSec}s (port not listening); check logs: $CurrentLog and $serverErrLog"
            }
            else {
                Write-LauncherLog "WARNING: Server not healthy after initial ${HealthTimeoutSec}s (port not listening); check logs: $CurrentLog and $serverErrLog"
            }
        }
    }
    # Attempt to refine PID to child python only when launch mode used a wrapper.
    try {
        if ($launchMode -eq 'pdm-fallback') {
            Start-Sleep -Milliseconds 400
            $wrapperPid = $proc.Id
            $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$wrapperPid" | Where-Object { $_.CommandLine -like '*src/workflows/von/main.py*' }
            if ($children -and $children.ProcessId) {
                $pythonPid = ($children | Select-Object -First 1 -ExpandProperty ProcessId)
                if ($pythonPid -and $pythonPid -ne $wrapperPid) {
                    Write-PidFile $pythonPid
                    Write-LauncherLog "Updated PID file to python process PID=$pythonPid (was wrapper PID=$wrapperPid)."
                }
            }
        }
    }
    catch { Write-LauncherLog "PID refinement skipped: $($_.Exception.Message)" }
    # Final sync (in case refinement missed due to timing)
    Sync-PidFileToListener
    Write-LauncherLog "Admin token file: $(Join-Path $RunDir 'admin_token.txt')"
    if ($ShowRelationCoverage) {
        try { Invoke-RelationCoverageSummary } catch { Write-LauncherLog "[relation-coverage] ERROR: $($_.Exception.Message)" }
    }
    Invoke-VonStartupBackgroundServices
    Write-VonVersionLog -TargetPort $Port
}

function Stop-VonServer {
    # was Stop-Von
    # Capture initial process (by PID file or port detection) and track that specific PID for wait/kill
    $proc = Get-ExistingProcess
    $detected = $null
    if (-not $proc) {
        $detected = Get-ListeningProcessByPort -Port $Port
        if ($detected) {
            Write-LauncherLog "PID file missing/stale; detected listener PID=$($detected.Id) on port $Port. Attempting shutdown.";
            $proc = $detected
        }
    }
    if (-not $proc) { Write-LauncherLog "Not running"; Remove-PidFile; return }
    $targetPid = $proc.Id
    $tokenFile = Join-Path $RunDir 'admin_token.txt'
    $token = if (Test-Path $tokenFile) { (Get-Content $tokenFile -Raw).Trim() } else { $null }
    $graceful = $false
    if ($token) {
        try {
            Write-LauncherLog "Attempting graceful shutdown (PID=$targetPid)..."
            $resp = Invoke-WebRequest -Method POST -Uri "http://localhost:$Port/admin/shutdown" -UseBasicParsing -Headers @{ 'X-Admin-Token' = $token } -TimeoutSec 5
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) { $graceful = $true }
            else { Write-LauncherLog "Graceful shutdown endpoint returned status $($resp.StatusCode)" }
        }
        catch { Write-LauncherLog "Graceful shutdown request failed: $($_.Exception.Message)" }
    }
    else { Write-LauncherLog "No admin token available; skipping graceful attempt." }
    # Wait up to 10s for target PID exit
    $waitMs = 0
    while ($waitMs -lt 10000) {
        Start-Sleep -Milliseconds 500
        $still = (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)
        if (-not $still) { break }
        $waitMs += 500
    }
    $stillAfter = (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)
    if ($stillAfter) {
        Write-LauncherLog "Process PID=$targetPid still running; issuing force kill..."
        try {
            $stopped = Stop-ProcessTreeWithEscalation -ProcessId $targetPid
            if (-not $stopped) { Write-LauncherLog "Force kill failed: PID=$targetPid remained alive after escalation." }
        }
        catch { Write-LauncherLog "Force kill failed: $($_.Exception.Message)" }
    }
    else {
        if ($graceful) { Write-LauncherLog "Graceful shutdown completed." } else { Write-LauncherLog "Process exited." }
    }
    if ((Test-Path $PidFile) -and (Select-String -Path $PidFile -Pattern "PID=$targetPid" -Quiet)) { Remove-PidFile }

    Stop-VonSharedWorkersForCurrentMode
}

function Get-VonStatus {
    # was Status-Von
    Sync-PidFileToListener
    $proc = Get-ExistingProcess
    $listening = Get-ListeningProcessByPort -Port $Port
    if (-not $proc) {
        if ($listening) {
            Write-LauncherLog ("RUNNING (untracked) DetectedPID={0} (no or stale PID file)" -f $listening.Id)
        }
        else {
            if (Test-Path $PidFile) { Write-LauncherLog "STALE: PID file exists but process missing. Run .\run.ps1 start to clear." } else { Write-LauncherLog "STOPPED" }
        }
        return
    }
    if ($listening -and $listening.Id -ne $proc.Id) {
        Write-LauncherLog ("WARNING: PID file PID={0} but port {1} is owned by PID={2}" -f $proc.Id, $Port, $listening.Id)
    }
    $healthy = Test-VonHealthEndpoint $Port
    $pidContent = Get-Content $PidFile | ForEach-Object { $_ }
    $startLine = ($pidContent | Where-Object { $_ -like 'START=*' })
    $startTime = $null; if ($startLine -and $startLine -match 'START=(.*)') { $startTime = Get-Date $Matches[1] }
    $uptime = if ($startTime) { (Get-Date) - $startTime } else { [timespan]::Zero }
    Write-LauncherLog ("RUNNING PID={0} Uptime={1} Healthy={2}" -f $proc.Id, [int]$uptime.TotalMinutes, $healthy)
    Write-LauncherLog (Get-MongoConnectionSummary -Port $Port)
    Write-LauncherLog "Log: $CurrentLog"
    $runMaintenance = $StatusRunMaintenance -or (Test-TruthySetting $env:VON_STATUS_RUN_MAINTENANCE)
    if ($runMaintenance) {
        try { Invoke-DailyGovernanceScan -Port $Port -StartupHealthy $healthy } catch { Write-LauncherLog "[governance-scan] ERROR: $($_.Exception.Message)" }
        try { Invoke-ConceptDataAbsenceCheck } catch { Write-LauncherLog "[concept-data-check] ERROR: $($_.Exception.Message)" }
        try { Invoke-ResidualLegacyTextAudit } catch { Write-LauncherLog "[residual-text-audit] ERROR: $($_.Exception.Message)" }
        try { Invoke-CleanupPreservedFields } catch { Write-LauncherLog "[cleanup-preserved-fields] ERROR: $($_.Exception.Message)" }
    }
    elseif ($HealthDebug) {
        Write-LauncherLog "Status maintenance checks skipped (set -StatusRunMaintenance or VON_STATUS_RUN_MAINTENANCE=1 to enable)."
    }
    if ($ShowRelationCoverage) {
        try { Invoke-RelationCoverageSummary } catch { Write-LauncherLog "[relation-coverage] ERROR: $($_.Exception.Message)" }
    }
}

function Show-VonLogs {
    # was Show-Logs
    if (-not (Test-Path $CurrentLog)) { Write-LauncherLog "No log file yet."; return }
    if ($Follow) {
        Get-Content $CurrentLog -Wait -Tail $Tail
    }
    else {
        Get-Content $CurrentLog -Tail $Tail
    }
}

function Restart-VonServer { Stop-VonServer; Start-VonServer -RestartTakeover }

# -----------------------------------------------------------------------------
# Governance Daily Scan
# -----------------------------------------------------------------------------
function Invoke-DailyGovernanceScan {
    param(
        [int]$Port,
        [bool]$StartupHealthy = $false
    )
    if ($env:VON_GOV_SCAN_DISABLE -eq '1') { return }
    # Optionally require healthy server first (we only need DB; if unhealthy but port listening we'll still try)
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_governance_scan.txt'
    $metaPath = Join-Path $runDir 'governance_scan_meta.json'
    $intervalHours = 24
    if ($env:VON_GOV_SCAN_INTERVAL_HOURS -match '^[0-9]+$') { $intervalHours = [int]$env:VON_GOV_SCAN_INTERVAL_HOURS }
    $force = ($env:VON_GOV_SCAN_FORCE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    $failureCount = 0
    if (Test-Path $metaPath) {
        try {
            $metaJson = Get-Content $metaPath -Raw | ConvertFrom-Json -ErrorAction Stop
            if ($metaJson.failure_count -match '^[0-9]+$') { $failureCount = [int]$metaJson.failure_count }
        }
        catch { }
    }
    # Dynamic backoff thresholds (hours) after successive failures: 1,3,6,12,24 then cap
    $backoffSchedule = @(1, 3, 6, 12, 24)
    $appliedBackoff = if ($failureCount -gt 0) { $backoffSchedule[ [Math]::Min($failureCount - 1, $backoffSchedule.Count - 1) ] } else { $intervalHours }
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($failureCount -gt 0) {
                if ($elapsed.TotalHours -lt $appliedBackoff) { $shouldRun = $false }
            }
            else {
                if ($elapsed.TotalHours -lt $intervalHours) { $shouldRun = $false }
            }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scanner = 'src/utilities/scan_code_concepts.py'
    if (-not (Test-Path (Join-Path $root $scanner))) { Write-LauncherLog "[governance-scan] scanner script missing ($scanner)"; return }
    $includeDocstrings = $false
    if ($env:VON_GOV_SCAN_INCLUDE_DOCSTRINGS -and $env:VON_GOV_SCAN_INCLUDE_DOCSTRINGS.ToString() -match '^(1|true|yes)$') {
        $includeDocstrings = $true
    }
    Write-LauncherLog "[governance-scan] Running governance concept tag scan (interval ${intervalHours}h; force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $pdmArgs = @('run', 'python', $scanner, '--sync-mentions', '--apply')
    if ($includeDocstrings) { $pdmArgs += '--include-docstrings' }
    $start = Get-Date
    $output = & $pdm @pdmArgs 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $updated = 0; $skipped = 0; $missing = 0; $virtual = 0; $warnings = 0
    $rawOutPath = Join-Path $runDir 'governance_scan_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    $parsed = $null
    try {
        $jsonStart = ($output | Select-String -Pattern '^\s*\{' | Select-Object -First 1).LineNumber
        if ($jsonStart -gt 0) {
            $jsonText = ($output | Select-Object -Skip ($jsonStart - 1)) -join "`n"
            try { $parsed = $jsonText | ConvertFrom-Json -ErrorAction Stop } catch { }
            if ($parsed -and $parsed.sync_result) {
                $sync = $parsed.sync_result
                $updated = if ($sync.updated) { ($sync.updated | Measure-Object).Count } else { 0 }
                $skipped = if ($sync.skipped) { ($sync.skipped | Measure-Object).Count } else { 0 }
                $missing = if ($sync.missing) { ($sync.missing | Measure-Object).Count } else { 0 }
                $virtual = if ($sync.virtual) { ($sync.virtual | Measure-Object).Count } else { 0 }
                $warnings = if ($sync.warnings) { ($sync.warnings | Measure-Object).Count } else { 0 }
            }
        }
    }
    catch { }
    $success = ($exitCode -eq 0 -and $null -ne $parsed)
    if ($success) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        $failureCount = 0
        $status = 'success'
        if ($updated -eq 0 -and $missing -eq 0 -and $virtual -eq 0 -and $warnings -eq 0) { $status = 'no-op' }
        Write-LauncherLog "[governance-scan] $status updated=$updated skipped=$skipped missing=$missing virtual=$virtual warnings=$warnings elapsed=${elapsedMs}ms failures=$failureCount"
    }
    else {
        # Enhanced hint logic: prefer parsed.error(s); else first error/trace line; else last 5 lines
        $hintLine = $null
        if ($parsed -and $parsed.error) { $hintLine = $parsed.error }
        elseif ($parsed -and $parsed.errors -and $parsed.errors.Length -gt 0) { $hintLine = ($parsed.errors | Select-Object -First 1) }
        if (-not $hintLine) { $hintLine = ($output | Where-Object { $_ -match 'Traceback|ERROR|Error|Exception' } | Select-Object -First 1) }
        if (-not $hintLine) { $hintLine = (($output | Select-Object -Last 5) -join ' || ') }
        $failureCount += 1
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        Write-LauncherLog "[governance-scan] FAILED elapsed=${elapsedMs}ms failures=$failureCount hint=$hintLine"
        Write-LauncherLog "[governance-scan] raw_output=$rawOutPath"
        if ($env:VON_GOV_SCAN_DEBUG -eq '1') { $output | ForEach-Object { Write-LauncherLog "[governance-scan][debug] $_" } }
    }
    # Persist meta (failure count)
    try { @{ failure_count = $failureCount } | ConvertTo-Json -Compress | Set-Content -Path $metaPath -Encoding UTF8 } catch { }
}

# -----------------------------------------------------------------------------
# Periodic Concept Data Absence Check (ensures migration remains enforced)
# -----------------------------------------------------------------------------
function Invoke-ConceptDataAbsenceCheck {
    param([int]$IntervalHours = 24)
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_concept_data_absence_check.txt'
    $force = ($env:VON_CONCEPT_DATA_ABSENCE_FORCE -eq '1')
    if ($env:VON_CONCEPT_DATA_ABSENCE_DISABLE -eq '1') { return }
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'src/utilities/verify_predicate_concepts.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[predicate-verify] script missing ($scriptPath)"; return }
    Write-LauncherLog "[predicate-verify] Running predicate concept verification (interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $output = & $pdm run python $scriptPath 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'predicate_verify_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    $parsed = $null
    $total = 0; $missing = 0; $virtual = 0; $warnings = 0
    try {
        $jsonStart = ($output | Select-String -Pattern '^\s*\{' | Select-Object -First 1).LineNumber
        if ($jsonStart -gt 0) {
            $jsonText = ($output | Select-Object -Skip ($jsonStart - 1)) -join "`n"
            try { $parsed = $jsonText | ConvertFrom-Json -ErrorAction Stop } catch { }
            if ($parsed) {
                $total = if ($parsed.total_registry) { [int]$parsed.total_registry } else { 0 }
                $missing = if ($parsed.missing) { ($parsed.missing | Measure-Object).Count } else { 0 }
                $virtual = if ($parsed.virtual) { ($parsed.virtual | Measure-Object).Count } else { 0 }
                $warnings = if ($parsed.warnings) { ($parsed.warnings | Measure-Object).Count } else { 0 }
            }
        }
    }
    catch { }
    if ($exitCode -eq 0 -and $null -ne $parsed) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        if ($missing -eq 0 -and $virtual -eq 0) {
            Write-LauncherLog "[predicate-verify] OK total=$total missing=$missing virtual=$virtual warnings=$warnings elapsed=${elapsedMs}ms"
        }
        else {
            Write-LauncherLog "[predicate-verify] MISSING total=$total missing=$missing virtual=$virtual warnings=$warnings elapsed=${elapsedMs}ms see $rawOutPath"
        }
    }
    else {
        Write-LauncherLog "[predicate-verify] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

# -----------------------------------------------------------------------------
# Residual Legacy Text Audit (concept_data / preserved_fields still present)
# -----------------------------------------------------------------------------
function Invoke-ResidualLegacyTextAudit {
    param([int]$IntervalHours = 24)
    if ($env:VON_RESIDUAL_TEXT_AUDIT_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_residual_text_audit.txt'
    $force = ($env:VON_RESIDUAL_TEXT_AUDIT_FORCE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/audit_residual_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[residual-text-audit] script missing ($scriptPath)"; return }
    Write-LauncherLog "[residual-text-audit] Running residual legacy text audit (interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $output = & $pdm run python $scriptPath 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'residual_text_audit_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        Write-LauncherLog "[residual-text-audit] OK (elapsed=${elapsedMs}ms)"
    }
    elseif ($exitCode -eq 2) {
        Write-LauncherLog "[residual-text-audit] VIOLATION: residual legacy text fields detected (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
    else {
        Write-LauncherLog "[residual-text-audit] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

# -----------------------------------------------------------------------------
# Cleanup Preserved Fields (concept_data.preserved_fields migration & purge)
# -----------------------------------------------------------------------------
function Invoke-CleanupPreservedFields {
    param([int]$IntervalHours = 24)
    if ($env:VON_CLEANUP_PRESERVED_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_cleanup_preserved_fields.txt'
    $force = ($env:VON_CLEANUP_PRESERVED_FORCE -eq '1')
    $execute = ($env:VON_CLEANUP_PRESERVED_EXECUTE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/cleanup_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[cleanup-preserved-fields] script missing ($scriptPath)"; return }
    $mode = if ($execute) { 'execute' } else { 'dry-run' }
    Write-LauncherLog "[cleanup-preserved-fields] Running cleanup (mode=$mode interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $pdmArgsLocal = @('run', 'python', $scriptPath)
    if (-not $execute) { $pdmArgsLocal += '--dry-run' }
    $output = & $pdm @pdmArgsLocal 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'cleanup_preserved_fields_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        if ($execute) { Write-LauncherLog "[cleanup-preserved-fields] OK executed (elapsed=${elapsedMs}ms)" }
        else { Write-LauncherLog "[cleanup-preserved-fields] OK no legacy fields detected (elapsed=${elapsedMs}ms)" }
    }
    elseif ($exitCode -eq 2 -and -not $execute) {
        Write-LauncherLog "[cleanup-preserved-fields] DETECTED legacy preserved_fields (elapsed=${elapsedMs}ms) enable VON_CLEANUP_PRESERVED_EXECUTE=1 to migrate. See $rawOutPath"
    }
    else {
        Write-LauncherLog "[cleanup-preserved-fields] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

# -----------------------------------------------------------------------------
# Relation Coverage Summary (hasDescription / hasNote / hasContent)
# -----------------------------------------------------------------------------
if (-not (Get-Command Invoke-RelationCoverageSummary -ErrorAction SilentlyContinue)) {
    function Invoke-RelationCoverageSummary {
        if ($env:VON_RELATION_COVERAGE_DISABLE -eq '1') { return }
        $scriptPath = 'scripts/maintenance/relation_coverage_summary.py'
        if (-not (Test-Path (Join-Path $Root $scriptPath))) { Write-LauncherLog "[relation-coverage] script missing ($scriptPath)"; return }
        $pdm = if (Test-Path (Join-Path $Root '.venv/Scripts/pdm.exe')) { (Join-Path $Root '.venv/Scripts/pdm.exe') } else { 'pdm' }
        # Optionally compute test DB concept count (best-effort; suppress errors)
        $testDbCount = $null
        try {
            $countScript = Join-Path $Root 'scripts/maintenance/count_concepts.py'
            if (Test-Path $countScript) {
                $origDb = $env:VON_DB_NAME
                $env:VON_DB_NAME = 'test_von_db'
                $raw = & $pdm run python $countScript 2>$null
                $env:VON_DB_NAME = $origDb
                $parsed = 0
                if ([int]::TryParse($raw, [ref]$parsed)) { $testDbCount = $parsed }
            }
        }
        catch { $testDbCount = $null }
        try {
            $output = & $pdm run python $scriptPath 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-LauncherLog "[relation-coverage] ERROR exit=$LASTEXITCODE"
            }
            else {
                # Prefix each line for clarity at launcher tail end
                $output | ForEach-Object {
                    if ($_ -match '^Total concepts:') {
                        if ($null -ne $testDbCount) {
                            # Append test DB concept count inline.
                            Write-LauncherLog ("[relation-coverage] {0} | test_db={1}" -f $_, $testDbCount)
                            try {
                                # If test DB behind primary, opportunistically trigger refresh (best-effort)
                                if (-not $env:VON_DISABLE_TEST_DB_REFRESH -and ($env:VON_DISABLE_TEST_DB_REFRESH -notmatch '^(1|true|yes)$')) {
                                    $primaryMatch = [regex]::Match($_, 'Total concepts:\s*(?<n>\d+)')
                                    if ($primaryMatch.Success) {
                                        $primaryCount = [int]$primaryMatch.Groups['n'].Value
                                        if ($primaryCount -gt 0 -and $testDbCount -lt $primaryCount) {
                                            # Avoid spawning if a refresh job already running
                                            $existingJob = Get-Job -Name 'von_test_db_refresh' -ErrorAction SilentlyContinue | Where-Object { $_.State -in 'Running', 'NotStarted' }
                                            if (-not $existingJob) {
                                                Write-LauncherLog ("[relation-coverage] test_db lag detected (primary={0} test={1}) triggering refresh job..." -f $primaryCount, $testDbCount)
                                                # Fire background job mirroring Invoke-TestDbRefreshIfDue logic but forced
                                                $refreshScript = Join-Path $Root 'scripts/maintenance/refresh_test_db.py'
                                                $pdmExeLocal = if (Test-Path (Join-Path $Root '.venv/Scripts/pdm.exe')) { (Join-Path $Root '.venv/Scripts/pdm.exe') } else { 'pdm' }
                                                if (Test-Path $refreshScript) {
                                                    $applyImmediate = $false
                                                    if ($env:VON_TEST_DB_REFRESH_APPLY -and $env:VON_TEST_DB_REFRESH_APPLY -match '^(1|true|yes)$') { $applyImmediate = $true }
                                                    Start-Job -Name 'von_test_db_refresh' -ScriptBlock {
                                                        param($pdmExeInner, $rootInner, $refreshPathInner, $applyFlagInner)
                                                        try {
                                                            Set-Location $rootInner
                                                            $refreshInvocation = @('run', 'python', $refreshPathInner, '--drop-target')
                                                            if ($applyFlagInner) { $refreshInvocation += '--apply' }
                                                            & $pdmExeInner @refreshInvocation 2>&1 | ForEach-Object { "[test-db-refresh] $_" }
                                                            Write-Host '[test-db-refresh] Completed (on mismatch trigger).'
                                                        }
                                                        catch { Write-Host ("[test-db-refresh] ERROR: {0}" -f $_.Exception.Message) }
                                                    } -ArgumentList $pdmExeLocal, $Root, $refreshScript, $applyImmediate | Out-Null
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            catch { Write-LauncherLog "[relation-coverage] WARN refresh trigger failed: $($_.Exception.Message)" }
                        }
                        else {
                            Write-LauncherLog "[relation-coverage] $_"
                        }
                    }
                    elseif ($_ -match '^(predicate|---)') { Write-LauncherLog "[relation-coverage] $_" }
                    elseif ($_ -match '^(hasDescription|hasNote|hasContent)') { Write-LauncherLog "[relation-coverage] $_" }
                }
            }
        }
        catch {
            Write-LauncherLog "[relation-coverage] ERROR exception: $($_.Exception.Message)"
        }
    }
} # end guard for Invoke-RelationCoverageSummary

# -----------------------------------------------------------------------------
# Residual Legacy Text Audit (concept_data / preserved_fields still present)
# -----------------------------------------------------------------------------
function Invoke-ResidualLegacyTextAudit {
    param([int]$IntervalHours = 24)
    if ($env:VON_RESIDUAL_TEXT_AUDIT_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_residual_text_audit.txt'
    $force = ($env:VON_RESIDUAL_TEXT_AUDIT_FORCE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/audit_residual_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[residual-text-audit] script missing ($scriptPath)"; return }
    Write-LauncherLog "[residual-text-audit] Running residual legacy text audit (interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $output = & $pdm run python $scriptPath 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'residual_text_audit_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        Write-LauncherLog "[residual-text-audit] OK (elapsed=${elapsedMs}ms)"
    }
    elseif ($exitCode -eq 2) {
        Write-LauncherLog "[residual-text-audit] VIOLATION: residual legacy text fields detected (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
    else {
        Write-LauncherLog "[residual-text-audit] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

# -----------------------------------------------------------------------------
# Cleanup Preserved Fields (concept_data.preserved_fields migration & purge)
# -----------------------------------------------------------------------------
function Invoke-CleanupPreservedFields {
    param([int]$IntervalHours = 24)
    if ($env:VON_CLEANUP_PRESERVED_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_cleanup_preserved_fields.txt'
    $force = ($env:VON_CLEANUP_PRESERVED_FORCE -eq '1')
    $execute = ($env:VON_CLEANUP_PRESERVED_EXECUTE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/cleanup_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[cleanup-preserved-fields] script missing ($scriptPath)"; return }
    $mode = if ($execute) { 'execute' } else { 'dry-run' }
    Write-LauncherLog "[cleanup-preserved-fields] Running cleanup (mode=$mode interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $pdmArgsLocal = @('run', 'python', $scriptPath)
    if (-not $execute) { $pdmArgsLocal += '--dry-run' }
    $output = & $pdm @pdmArgsLocal 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'cleanup_preserved_fields_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        if ($execute) { Write-LauncherLog "[cleanup-preserved-fields] OK executed (elapsed=${elapsedMs}ms)" }
        else { Write-LauncherLog "[cleanup-preserved-fields] OK no legacy fields detected (elapsed=${elapsedMs}ms)" }
    }
    elseif ($exitCode -eq 2 -and -not $execute) {
        Write-LauncherLog "[cleanup-preserved-fields] DETECTED legacy preserved_fields (elapsed=${elapsedMs}ms) enable VON_CLEANUP_PRESERVED_EXECUTE=1 to migrate. See $rawOutPath"
    }
    else {
        Write-LauncherLog "[cleanup-preserved-fields] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

## (Removed duplicate Invoke-RelationCoverageSummary stub here)
## Residual Legacy Text Audit (concept_data / preserved_fields still present)
function Invoke-ResidualLegacyTextAudit {
    param([int]$IntervalHours = 24)
    if ($env:VON_RESIDUAL_TEXT_AUDIT_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_residual_text_audit.txt'
    $force = ($env:VON_RESIDUAL_TEXT_AUDIT_FORCE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/audit_residual_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[residual-text-audit] script missing ($scriptPath)"; return }
    Write-LauncherLog "[residual-text-audit] Running residual legacy text audit (interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $output = & $pdm run python $scriptPath 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'residual_text_audit_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        Write-LauncherLog "[residual-text-audit] OK (elapsed=${elapsedMs}ms)"
    }
    elseif ($exitCode -eq 2) {
        Write-LauncherLog "[residual-text-audit] VIOLATION: residual legacy text fields detected (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
    else {
        Write-LauncherLog "[residual-text-audit] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

# -----------------------------------------------------------------------------
# Cleanup Preserved Fields (concept_data.preserved_fields migration & purge)
# -----------------------------------------------------------------------------
function Invoke-CleanupPreservedFields {
    param([int]$IntervalHours = 24)
    if ($env:VON_CLEANUP_PRESERVED_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_cleanup_preserved_fields.txt'
    $force = ($env:VON_CLEANUP_PRESERVED_FORCE -eq '1')
    $execute = ($env:VON_CLEANUP_PRESERVED_EXECUTE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/cleanup_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[cleanup-preserved-fields] script missing ($scriptPath)"; return }
    $mode = if ($execute) { 'execute' } else { 'dry-run' }
    Write-LauncherLog "[cleanup-preserved-fields] Running cleanup (mode=$mode interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $pdmArgsLocal = @('run', 'python', $scriptPath)
    if (-not $execute) { $pdmArgsLocal += '--dry-run' }
    $output = & $pdm @pdmArgsLocal 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'cleanup_preserved_fields_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        if ($execute) { Write-LauncherLog "[cleanup-preserved-fields] OK executed (elapsed=${elapsedMs}ms)" }
        else { Write-LauncherLog "[cleanup-preserved-fields] OK no legacy fields detected (elapsed=${elapsedMs}ms)" }
    }
    elseif ($exitCode -eq 2 -and -not $execute) {
        Write-LauncherLog "[cleanup-preserved-fields] DETECTED legacy preserved_fields (elapsed=${elapsedMs}ms) enable VON_CLEANUP_PRESERVED_EXECUTE=1 to migrate. See $rawOutPath"
    }
    else {
        Write-LauncherLog "[cleanup-preserved-fields] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

## (Removed duplicate Invoke-RelationCoverageSummary stub here)
## Residual Legacy Text Audit (concept_data / preserved_fields still present)
function Invoke-ResidualLegacyTextAudit {
    param([int]$IntervalHours = 24)
    if ($env:VON_RESIDUAL_TEXT_AUDIT_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_residual_text_audit.txt'
    $force = ($env:VON_RESIDUAL_TEXT_AUDIT_FORCE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/audit_residual_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[residual-text-audit] script missing ($scriptPath)"; return }
    Write-LauncherLog "[residual-text-audit] Running residual legacy text audit (interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $output = & $pdm run python $scriptPath 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'residual_text_audit_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        Write-LauncherLog "[residual-text-audit] OK (elapsed=${elapsedMs}ms)"
    }
    elseif ($exitCode -eq 2) {
        Write-LauncherLog "[residual-text-audit] VIOLATION: residual legacy text fields detected (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
    else {
        Write-LauncherLog "[residual-text-audit] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

# -----------------------------------------------------------------------------
# Cleanup Preserved Fields (concept_data.preserved_fields migration & purge)
# -----------------------------------------------------------------------------
function Invoke-CleanupPreservedFields {
    param([int]$IntervalHours = 24)
    if ($env:VON_CLEANUP_PRESERVED_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_cleanup_preserved_fields.txt'
    $force = ($env:VON_CLEANUP_PRESERVED_FORCE -eq '1')
    $execute = ($env:VON_CLEANUP_PRESERVED_EXECUTE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'scripts/maintenance/cleanup_preserved_fields.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[cleanup-preserved-fields] script missing ($scriptPath)"; return }
    $mode = if ($execute) { 'execute' } else { 'dry-run' }
    Write-LauncherLog "[cleanup-preserved-fields] Running cleanup (mode=$mode interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $pdmArgsLocal = @('run', 'python', $scriptPath)
    if (-not $execute) { $pdmArgsLocal += '--dry-run' }
    $output = & $pdm @pdmArgsLocal 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'cleanup_preserved_fields_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        if ($execute) { Write-LauncherLog "[cleanup-preserved-fields] OK executed (elapsed=${elapsedMs}ms)" }
        else { Write-LauncherLog "[cleanup-preserved-fields] OK no legacy fields detected (elapsed=${elapsedMs}ms)" }
    }
    elseif ($exitCode -eq 2 -and -not $execute) {
        Write-LauncherLog "[cleanup-preserved-fields] DETECTED legacy preserved_fields (elapsed=${elapsedMs}ms) enable VON_CLEANUP_PRESERVED_EXECUTE=1 to migrate. See $rawOutPath"
    }
    else {
        Write-LauncherLog "[cleanup-preserved-fields] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

## (Removed duplicate Invoke-RelationCoverageSummary stub here)
# -----------------------------------------------------------------------------
# Relationship Alias Audit (normalise legacy relationship keys)
# -----------------------------------------------------------------------------
function Invoke-ResidualLegacyTextAudit {
    param([int]$IntervalHours = 24)
    if ($env:VON_RESIDUAL_TEXT_AUDIT_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_residual_text_audit.txt'
    $force = ($env:VON_RESIDUAL_TEXT_AUDIT_FORCE -eq '1')
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'src/utilities/normalise_relationship_aliases.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[relation-alias-audit] script missing ($scriptPath)"; return }
    Write-LauncherLog "[relation-alias-audit] Running relationship alias audit (interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $pdmArgsLocal = @('run', 'python', $scriptPath, '--dry-run')
    $output = & $pdm @pdmArgsLocal 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'relation_alias_audit_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    $parsed = $null
    $updated = 0; $warnings = 0
    try {
        $jsonStart = ($output | Select-String -Pattern '^\s*\{' | Select-Object -First 1).LineNumber
        if ($jsonStart -gt 0) {
            $jsonText = ($output | Select-Object -Skip ($jsonStart - 1)) -join "`n"
            try { $parsed = $jsonText | ConvertFrom-Json -ErrorAction Stop } catch { }
            if ($parsed -and $parsed.updated) {
                $updated = ($parsed.updated.PSObject.Properties | Measure-Object).Count
            }
            if ($parsed -and $parsed.warnings) {
                $warnings = ($parsed.warnings | Measure-Object).Count
            }
        }
    }
    catch { }
    if ($exitCode -eq 0 -and $null -ne $parsed) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        if ($updated -eq 0) {
            Write-LauncherLog "[relation-alias-audit] OK updated=$updated warnings=$warnings elapsed=${elapsedMs}ms"
        }
        else {
            Write-LauncherLog "[relation-alias-audit] DETECTED updated=$updated warnings=$warnings elapsed=${elapsedMs}ms see $rawOutPath"
        }
    }
    else {
        Write-LauncherLog "[relation-alias-audit] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

# -----------------------------------------------------------------------------
# Relationship Alias Cleanup (apply normalisation when enabled)
# -----------------------------------------------------------------------------
function Invoke-CleanupPreservedFields {
    param([int]$IntervalHours = 24)
    if ($env:VON_CLEANUP_PRESERVED_DISABLE -eq '1') { return }
    $root = $Root
    $runDir = Join-Path $root '.run'
    $sentinel = Join-Path $runDir 'last_cleanup_preserved_fields.txt'
    $force = ($env:VON_CLEANUP_PRESERVED_FORCE -eq '1')
    $execute = $false
    if ($env:VON_RELATION_ALIAS_EXECUTE -eq '1') { $execute = $true }
    elseif ($env:VON_CLEANUP_PRESERVED_EXECUTE -eq '1') { $execute = $true }
    $now = Get-Date
    $shouldRun = $true
    if (-not $force -and (Test-Path $sentinel)) {
        try {
            $last = Get-Date (Get-Content $sentinel -Raw).Trim()
            $elapsed = $now - $last
            if ($elapsed.TotalHours -lt $IntervalHours) { $shouldRun = $false }
        }
        catch { }
    }
    if (-not $shouldRun -and -not $force) { return }
    $scriptPath = 'src/utilities/normalise_relationship_aliases.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[relation-alias-cleanup] script missing ($scriptPath)"; return }
    $mode = if ($execute) { 'apply' } else { 'dry-run' }
    Write-LauncherLog "[relation-alias-cleanup] Running normalisation (mode=$mode interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $pdmArgsLocal = @('run', 'python', $scriptPath)
    if (-not $execute) { $pdmArgsLocal += '--dry-run' }
    $output = & $pdm @pdmArgsLocal 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'relation_alias_cleanup_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    $parsed = $null
    $updated = 0; $warnings = 0
    try {
        $jsonStart = ($output | Select-String -Pattern '^\s*\{' | Select-Object -First 1).LineNumber
        if ($jsonStart -gt 0) {
            $jsonText = ($output | Select-Object -Skip ($jsonStart - 1)) -join "`n"
            try { $parsed = $jsonText | ConvertFrom-Json -ErrorAction Stop } catch { }
            if ($parsed -and $parsed.updated) {
                $updated = ($parsed.updated.PSObject.Properties | Measure-Object).Count
            }
            if ($parsed -and $parsed.warnings) {
                $warnings = ($parsed.warnings | Measure-Object).Count
            }
        }
    }
    catch { }
    if ($exitCode -eq 0 -and $null -ne $parsed) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        if ($execute) {
            Write-LauncherLog "[relation-alias-cleanup] OK applied updated=$updated warnings=$warnings elapsed=${elapsedMs}ms"
        }
        elseif ($updated -eq 0) {
            Write-LauncherLog "[relation-alias-cleanup] OK updated=$updated warnings=$warnings elapsed=${elapsedMs}ms"
        }
        else {
            Write-LauncherLog "[relation-alias-cleanup] DETECTED updated=$updated warnings=$warnings elapsed=${elapsedMs}ms enable VON_RELATION_ALIAS_EXECUTE=1 to apply. See $rawOutPath"
        }
    }
    else {
        Write-LauncherLog "[relation-alias-cleanup] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

## (Removed final duplicate Invoke-RelationCoverageSummary stub here)

function Show-Help {
    @'
Von Launcher Help
    Usage: .\run.ps1 [action] [options]
    Actions: start | foreground | stop | status | restart | logs | check | backup | restore-backup | autoupdate | rag-worker | help
    Options:
        -Port <int>            Server port (default 5001 on macOS, 5000 elsewhere; -AgentTest defaults to 5010)
        -AgentTest             Isolated coding-agent test instance mode:
                               defaults to port 5010 unless -Port is supplied,
                               implies -NoBrowser unless -ForceBrowser is supplied,
                               preserves other Von servers, and skips shared
                               background workers/startup maintenance.
        -IsolatedTestInstance  Alias for -AgentTest
    -NoBrowser             Do not auto open browser
    -ForceBrowser          Force opening browser even if already opened once
        -LogRetention <n>      Keep last n logs (default 20)
        -Tail <n>              Lines for logs action (default 100)
        -Follow                Stream log (logs action)
        -AdminToken <token>    Provide explicit shutdown token
    -SkipHealth            Do not wait for health during start
        -ShowRelationCoverage  Show relationship coverage summary on start/status
        -HealthTimeoutSec <n>  Seconds to wait for /health (default 960)
        -HealthGraceSec <n>    Extra seconds after port listens to keep waiting (default 45)
        -ReadyLogPatterns <p>  One or more substrings that indicate readiness (log shortcut)
        -DisableLogReady       Disable log pattern readiness shortcut
        -HealthDebug           Verbose health polling diagnostics
        -StatusRunMaintenance  Include maintenance scans in status output (default: off)
        -BackupDryRun           For backup action: do not run mongodump (prints what would happen)
        -BackupTag <tag>        For backup action: tag suffix for backup dir (default manual)
        -BackupOutDir <path>    For backup action: output root dir (default resolved backup root)
        -RestoreBackupPath <p>  For restore-backup: backup artefact or receipt path
        -RestoreTargetDbName <n> For restore-backup: target DB (default source_db_restore_probe)
        -RestoreApply            For restore-backup: actually run mongorestore (default dry-run)
        -RestoreDropTarget       For restore-backup: drop target collections before restore
    Backup safety environment variables:
        VON_ENABLE_BACKUP_ACTION=1   Required to run on-demand ".\run.ps1 backup"
        VON_BACKUP_ROOT=<path>       Recommended explicit backup root outside this repo
        VON_ALLOW_BACKUP_IN_REPO=1   Override safety block for in-repo backup apply mode
        VON_ENABLE_RESTORE_ACTION=1  Required to run ".\run.ps1 restore-backup"
    AI chat-session sync environment variables:
        VON_ENABLE_AI_CHAT_SESSION_SYNC=1
        VON_AI_CHAT_SESSION_SYNC_INTERVAL_HOURS=<int>
        VON_AI_CHAT_SESSION_SYNC_CODEX_ROOT=<path>
        VON_AI_CHAT_SESSION_SYNC_COPILOT_USER_ROOTS=<comma-separated VS Code User roots>
        VON_AI_CHAT_SESSION_SYNC_COPILOT_EXTRA_ROOTS=<comma-separated direct chat roots>
    Default backup root resolution order:
        VON_BACKUP_ROOT -> W:\von_backups -> %LOCALAPPDATA%\Von\backups -> .\backups (last resort)
        -UpdateIntervalMinutes <n>  Minutes between git update checks (autoupdate action; default 60)
        -UpdateBranch <name>        Branch to track (default main)
        -UpdateNoRestartIfRunning   Skip restart if server already running (still pull code)
    Single-tab behavior: Browser auto-opens only on first successful start; subsequent restarts reuse the same tab unless -ForceBrowser is supplied. Sentinel: .run/browser_opened_once

    Examples:
        .\run.ps1 start
        .\run.ps1 restart -AgentTest -HealthTimeoutSec 960
        .\run.ps1 restart -AgentTest -Port 5011 -HealthTimeoutSec 960
        .\run.ps1 status
        .\run.ps1 check          # returns exit code (0 healthy, 2 unhealthy, 3 not running)
        .\run.ps1 logs -Tail 200 -Follow
        .\run.ps1 backup -BackupDryRun
        .\run.ps1 backup -BackupTag manual
        .\run.ps1 backup -BackupTag pre-change -BackupOutDir C:\von_backups
        .\run.ps1 restore-backup -RestoreBackupPath C:\von_backups\last_successful_backup_receipt.json
        .\run.ps1 restore-backup -RestoreBackupPath C:\von_backups\von_db_20260406_010203Z_auto-daily.zip -RestoreTargetDbName von_db_restore_probe -RestoreApply -RestoreDropTarget
        .\run.ps1 stop
    .\run.ps1 stop 12345        # kill specific PID directly
    .\run.ps1 stop force        # detect by port, verify command line, then kill
    .\run.ps1 stop force-any    # detect by port, skip verification, then kill
        .\run.ps1 foreground
        .\run.ps1 autoupdate -UpdateIntervalMinutes 30 -UpdateBranch main
'
'@ | Write-Host
}

function Invoke-CodeMentionScan {
    param([string]$Reason = 'backup')
    if ($env:VON_DISABLE_CODE_MENTION_SCAN -and $env:VON_DISABLE_CODE_MENTION_SCAN.ToString() -match '^(1|true|yes)$') {
        Write-LauncherLog "[code-mention-scan] disabled via VON_DISABLE_CODE_MENTION_SCAN"
        return
    }
    $scriptPath = 'src/utilities/scan_code_concepts.py'
    $fullPath = Join-Path $Root $scriptPath
    if (-not (Test-Path $fullPath)) {
        Write-LauncherLog "[code-mention-scan] WARN: script missing ($scriptPath)"
        return
    }
    $pdm = if (Test-Path (Join-Path $Root '.venv\\Scripts\\pdm.exe')) { Join-Path $Root '.venv\\Scripts\\pdm.exe' } else { 'pdm' }
    Write-LauncherLog "[code-mention-scan] Running scan (reason=$Reason)"
    $start = Get-Date
    $output = & $pdm run python $scriptPath --sync-mentions --apply 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $RunDir 'code_mention_scan_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-LauncherLog "[code-mention-scan] OK (elapsed=${elapsedMs}ms)"
    }
    else {
        Write-LauncherLog "[code-mention-scan] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

function Invoke-CodePredicateSync {
    param([string]$Reason = 'backup')
    if ($env:VON_DISABLE_CODE_PREDICATE_SYNC -and $env:VON_DISABLE_CODE_PREDICATE_SYNC.ToString() -match '^(1|true|yes)$') {
        Write-LauncherLog "[code-predicate-sync] disabled via VON_DISABLE_CODE_PREDICATE_SYNC"
        return
    }
    $scriptPath = 'src/utilities/sync_code_predicates.py'
    $fullPath = Join-Path $Root $scriptPath
    if (-not (Test-Path $fullPath)) {
        Write-LauncherLog "[code-predicate-sync] WARN: script missing ($scriptPath)"
        return
    }
    $pdm = if (Test-Path (Join-Path $Root '.venv\\Scripts\\pdm.exe')) { Join-Path $Root '.venv\\Scripts\\pdm.exe' } else { 'pdm' }
    Write-LauncherLog "[code-predicate-sync] Running sync (reason=$Reason)"
    $start = Get-Date
    $output = & $pdm run python $scriptPath --retag-non-predicates 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $RunDir 'code_predicate_sync_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-LauncherLog "[code-predicate-sync] OK (elapsed=${elapsedMs}ms)"
    }
    else {
        Write-LauncherLog "[code-predicate-sync] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
}

function Invoke-BackupNow {
    <#
        Run an on-demand DB backup without restarting the server.
        Defaults:
          - Output directory: resolved backup root (VON_BACKUP_ROOT or safe fallback)
          - Tag: "manual"
          - Mode: apply unless -BackupDryRun
    #>
    if (-not (Test-TruthySetting $env:VON_ENABLE_BACKUP_ACTION)) {
        Write-LauncherLog "[backup] ERROR: manual backup action is disabled by default. Set VON_ENABLE_BACKUP_ACTION=1 to acknowledge admin-level access and enable this action."
        return
    }

    $backupScript = Join-Path $Root 'scripts/backup_von_db.py'
    if (-not (Test-Path $backupScript)) {
        Write-LauncherLog "[backup] ERROR: backup script missing: $backupScript"
        return
    }
    $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    $tag = if ($BackupTag) { $BackupTag } else { 'manual' }
    $requestedOutDir = if ($BackupOutDir) { $BackupOutDir } else { $BackupRoot }
    $outDir = Resolve-BackupOutDir -OutDir $requestedOutDir -FallbackDir (Get-BackupFallbackDir) -Reason 'manual-backup'
    $apply = -not $BackupDryRun
    if (-not (Test-BackupOutputPathAllowed -Path $outDir -Context 'backup' -ApplyMode $apply)) {
        return
    }
    $mode = if ($apply) { 'apply' } else { 'dry-run' }
    Write-LauncherLog "[backup] Starting backup (mode=$mode tag=$tag out=$outDir root=$BackupRoot)"

    if ($apply) {
        & $pdm run python $backupScript --apply --out-dir $outDir --tag $tag
    }
    else {
        & $pdm run python $backupScript --out-dir $outDir --tag $tag
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-LauncherLog "[backup] OK"
        try { Invoke-CodeMentionScan -Reason 'manual-backup' } catch { Write-LauncherLog "[code-mention-scan] WARN: $($_.Exception.Message)" }
        try { Invoke-CodePredicateSync -Reason 'manual-backup' } catch { Write-LauncherLog "[code-predicate-sync] WARN: $($_.Exception.Message)" }
    }
    else {
        Write-LauncherLog "[backup] ERROR exit=$exitCode"
    }

    # Best-effort: if W: is (re)connected, migrate local backup artefacts now.
    if (-not $NoBackupMigrate) {
        try { Invoke-MigrateLocalBackupsToWDrive } catch { Write-LauncherLog "[backup-migrate] WARN post-backup migrate failed: $($_.Exception.Message)" }
    }
}

function Invoke-RestoreBackupNow {
    <#
        Run a local operator restore from a backup artefact or receipt.
        Defaults to dry-run and requires explicit opt-in via VON_ENABLE_RESTORE_ACTION.
    #>
    if (-not (Test-TruthySetting $env:VON_ENABLE_RESTORE_ACTION)) {
        Write-LauncherLog "[restore-backup] ERROR: restore action is disabled by default. Set VON_ENABLE_RESTORE_ACTION=1 to acknowledge destructive restore risk and enable this action."
        return
    }
    if (-not $RestoreBackupPath) {
        Write-LauncherLog "[restore-backup] ERROR: specify -RestoreBackupPath with a backup artefact or receipt path."
        return
    }

    $restoreScript = Join-Path $Root 'scripts/restore_von_db.py'
    if (-not (Test-Path $restoreScript)) {
        Write-LauncherLog "[restore-backup] ERROR: restore script missing: $restoreScript"
        return
    }

    $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    $args = @('run', 'python', $restoreScript, '--backup-path', $RestoreBackupPath)
    if ($RestoreTargetDbName) {
        $args += @('--target-db-name', $RestoreTargetDbName)
    }
    if ($RestoreDropTarget) {
        $args += '--drop-target'
    }
    if ($RestoreApply) {
        $args += '--apply'
    }

    $mode = if ($RestoreApply) { 'apply' } else { 'dry-run' }
    $drop = if ($RestoreDropTarget) { 'true' } else { 'false' }
    $targetSummary = if ($RestoreTargetDbName) { $RestoreTargetDbName } else { '<auto restore probe>' }
    Write-LauncherLog "[restore-backup] Starting restore (mode=$mode backup=$RestoreBackupPath target_db=$targetSummary drop_target=$drop)"
    & $pdm @args
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-LauncherLog "[restore-backup] OK"
    }
    else {
        Write-LauncherLog "[restore-backup] ERROR exit=$exitCode"
    }
}

switch ($Action) {
    'start' {
        Start-VonServer
        if ($script:VonStartFailed) { exit 2 }
    }
    'foreground' {
        Write-LauncherLog "Running in foreground... (Ctrl+C to stop)"
        $env:VON_ADMIN_TOKEN = Read-AdminToken
        $env:PYTHONUNBUFFERED = '1'; $env:PYTHONPATH = $Root
        # Ensure Python runs in UTF-8 mode in foreground too
        $env:PYTHONUTF8 = '1'
        $env:PYTHONIOENCODING = 'utf-8'
        $projectPython = Get-ProjectPythonExecutable
        if ($projectPython -and $projectPython -ne 'python') {
            & $projectPython -u src/workflows/von/main.py --port "$Port"
        }
        else {
            $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
            & $pdm run python -u src/workflows/von/main.py --port "$Port"
        }
    }
    'stop' {
        if ($ExtraArgs -and $ExtraArgs.Count -ge 1) {
            $arg = $ExtraArgs[0]
            if ($arg -match '^[0-9]+$') {
                $targetPid = [int]$arg
                Write-LauncherLog "Stopping specific PID=$targetPid (direct)"
                try { Stop-Process -Id $targetPid -ErrorAction Stop; Write-LauncherLog "PID $($targetPid) terminated."; Remove-PidFile; return } catch { Write-LauncherLog "Failed to kill PID $($targetPid): $($_.Exception.Message)" }
            }
            elseif ($arg.ToLower() -eq 'force') {
                Write-LauncherLog "Force stop requested: attempting graceful+fallback with command line verification."
                # Do NOT remove PID file before Stop-Von; we want original PID for tracking
                # Detect listener
                $listener = Get-ListeningProcessByPort -Port $Port
                if ($listener) {
                    $cmdLine = Get-ProcessCommandLine -ProcessId $listener.Id
                    $expectedSubstrings = @('src/workflows/von/main.py', 'python', 'pdm')
                    $foundSubstrings = $expectedSubstrings | Where-Object { $cmdLine -like "*$_*" }
                    if ($foundSubstrings.Count -lt 1) {
                        Write-LauncherLog "ABORT: Detected PID $($listener.Id) command line does not look like Von server. Use 'stop force-any' to override."
                        return
                    }
                    else {
                        Write-LauncherLog "Verified server command line contains: $($foundSubstrings -join ', ')"
                    }
                }
                Stop-VonServer
                return
            }
            elseif ($arg.ToLower() -eq 'force-any') {
                Write-LauncherLog "Force-any stop requested: bypassing command line verification."
                # Keep PID file until Stop-Von handles removal
                Stop-VonServer
                return
            }
        }
        Stop-VonServer
    }
    'status' { Get-VonStatus }
    'restart' {
        Restart-VonServer
        if ($script:VonStartFailed) { exit 2 }
    }
    'logs' { Show-VonLogs }
    'check' {
        Sync-PidFileToListener
        $proc = Get-ExistingProcess
        if (-not $proc) {
            $listening = Get-ListeningProcessByPort -Port $Port
            if ($listening) {
                $healthy = Test-VonHealthEndpoint $Port
                if ($healthy) { Write-LauncherLog ("HEALTHY (listener PID={0} no PID file)" -f $listening.Id); exit 0 }
                else { Write-LauncherLog ("UNHEALTHY (listener PID={0} no PID file)" -f $listening.Id); exit 2 }
            }
            else {
                Write-LauncherLog 'NOT RUNNING'; exit 3
            }
        }
        else {
            $healthy = Test-VonHealthEndpoint $Port
            if ($healthy) { Write-LauncherLog ("HEALTHY PID={0}" -f $proc.Id); exit 0 }
            else { Write-LauncherLog ("UNHEALTHY PID={0}" -f $proc.Id); exit 2 }
        }
    }
    'backup' { Invoke-BackupNow }
    'restore-backup' { Invoke-RestoreBackupNow }
    'autoupdate' {
        Write-LauncherLog "Starting auto-update loop (branch=$UpdateBranch interval=${UpdateIntervalMinutes}m)... Press Ctrl+C to stop."
        # Ensure git is available
        try { git --version | Out-Null } catch { Write-LauncherLog 'ERROR: git not found on PATH.'; exit 1 }
        while ($true) {
            try {
                Set-Location $Root
                # Start server if not running (unless user wants to only update when stopped)
                $running = Get-ExistingProcess
                if (-not $running) { Start-VonServer }
                git fetch origin $UpdateBranch 2>$null | Out-Null
                $local = (git rev-parse HEAD).Trim()
                $remoteRef = "origin/$UpdateBranch"
                $remote = (git rev-parse $remoteRef).Trim()
                if ($local -ne $remote) {
                    $mergeBase = (git merge-base HEAD $remoteRef).Trim()
                    if ($mergeBase -eq $local) {
                        if ((git status --porcelain).Trim()) {
                            Write-LauncherLog "Update available ($local -> $remote) but local uncommitted changes present; skipping pull."
                        }
                        else {
                            Write-LauncherLog "Applying update ($local -> $remote)..."
                            if ($running -and -not $UpdateNoRestartIfRunning) {
                                Write-LauncherLog 'Stopping server for update...';
                                Stop-VonServer
                            }
                            git pull --ff-only origin $UpdateBranch | ForEach-Object { Write-LauncherLog $_ }
                            if (-not $UpdateNoRestartIfRunning) {
                                Write-LauncherLog 'Restarting server after update...'
                                Start-VonServer
                            }
                            else {
                                Write-LauncherLog 'Pulled updates without restart (UpdateNoRestartIfRunning set).'
                            }
                        }
                    }
                    else {
                        Write-LauncherLog "Local branch diverged from remote (local=$local remote=$remote base=$mergeBase); manual merge required."
                    }
                }
                else {
                    Write-LauncherLog "No updates (HEAD=$local)."
                }
            }
            catch {
                Write-LauncherLog "Auto-update iteration error: $($_.Exception.Message)"
            }
            for ($m = 0; $m -lt $UpdateIntervalMinutes; $m++) {
                Start-Sleep -Seconds 60
            }
        }
    }
    'rag-worker' {
        Write-LauncherLog "Starting RAG Indexing Worker..."
        $env:PYTHONUNBUFFERED = '1'
        $env:PYTHONPATH = $Root
        $pythonExe = Get-ProjectPythonExecutable
        & $pythonExe -u src/backend/utilities/rag_indexing_worker.py
    }
    'help' { Show-Help }
    default { Write-LauncherLog "Unknown action '$Action'"; Show-Help }
}

# Backward-compatible aliases (old names)
Set-Alias Rotate-Logs Invoke-VonLogRotation -ErrorAction SilentlyContinue
Set-Alias Health-Check Test-VonHealthEndpoint -ErrorAction SilentlyContinue
Set-Alias Test-PortListening Test-VonPortListening -ErrorAction SilentlyContinue
Set-Alias Start-Von Start-VonServer -ErrorAction SilentlyContinue
Set-Alias Stop-Von Stop-VonServer -ErrorAction SilentlyContinue
Set-Alias Status-Von Get-VonStatus -ErrorAction SilentlyContinue
Set-Alias Show-Logs Show-VonLogs -ErrorAction SilentlyContinue
Set-Alias Restart-Von Restart-VonServer -ErrorAction SilentlyContinue

exit 0


