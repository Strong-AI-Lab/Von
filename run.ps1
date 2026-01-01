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
        help         Show help

    Flags:
        -Port <int>              Port (reserved for future multi-instance) [TODO multi-instance]
        -NoBrowser               Do not auto-open browser on start
        -Tail <n>                When action=logs, number of lines (default 100)
        -Follow                  When action=logs, stream updates
        -LogRetention <n>        Keep last n log files (default 20)
        -AdminToken <token>      Explicit admin token (else auto-generate & persist)

    TODO: Multi-instance support using per-port PID/log naming and concurrent starts.
    TODO: Optional size-based log rollover.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)] [string]$Action = 'start',
    [int]$Port = 5000,
    [switch]$NoBrowser,
    [switch]$ForceBrowser,
    [int]$Tail = 100,
    [switch]$Follow,
    [int]$LogRetention = 20,
    [string]$AdminToken,
    [switch]$SkipHealth,
    [int]$HealthTimeoutSec = 60,
    [int]$HealthGraceSec = 45,
    [string[]]$ReadyLogPatterns = @('Running with Waitress', 'Press CTRL+C to quit', 'Flask app running'),
    [switch]$DisableLogReady,
    [switch]$HealthDebug,
    [switch]$ShowRelationCoverage,
    # On-demand backups
    [switch]$BackupDryRun,
    [string]$BackupTag = 'manual',
    [string]$BackupOutDir,
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

# Ensure logging helper is available before any code path that may emit log lines.
if (-not (Get-Command Write-LauncherLog -ErrorAction SilentlyContinue)) {
    function Write-LauncherLog {
        param([Parameter(Mandatory = $true)][string]$Msg)
        $ts = (Get-Date).ToString('HH:mm:ss')
        Write-Host ("[{0}] {1}" -f $ts, $Msg)
    }
}

# Backup root resolution:
# - Prefer explicit VON_BACKUP_ROOT if already present in environment.
# - Else prefer W:\von_backups if W: exists and is writable.
# - Else fall back to repo-local 'backups'.
$BackupRoot = $null

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

if (-not $BackupRoot -and (Test-Path 'W:\')) {
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
    $BackupRoot = Join-Path $Root 'backups'
    try { New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null } catch { }
}

if (-not $BackupOutDir) {
    $BackupOutDir = $BackupRoot
}



function Invoke-MigrateLocalBackupsToWDrive {
    <#
        If W: exists and local backups dir exists, move all but the most recent
        timestamped backup directory from local 'backups' into W:\von_backups.
        Skips if backup root already is W:, or fewer than 2 local backups.
        Only moves directories matching pattern <name>_YYYYMMDD_HHMMSS*.
        Logs with [backup-migrate]. Failures on individual moves are warned and continue.
    #>
    $moved = 0
    $copied = 0
    if (-not (Test-Path 'W:\')) { Write-LauncherLog "[backup-migrate] summary moved=$moved copied=$copied (no W: drive)"; return }
    # Even if current backup root is already on W:, still attempt to migrate any residual local backups directory.
    $localBackups = Join-Path $Root 'backups'
    if (-not (Test-Path $localBackups)) {
        try { New-Item -ItemType Directory -Force -Path $localBackups | Out-Null } catch { Write-LauncherLog "[backup-migrate] summary moved=$moved copied=$copied (cannot create local backups dir)"; return }
    }
    try { $items = Get-ChildItem -Path $localBackups -Directory -ErrorAction Stop } catch { $items = @() }
    $candidates = $items | Where-Object { $_.Name -match '_[0-9]{8}_[0-9]{6}Z?' }
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
        foreach ($dir in $toMove) {
            $dest = Join-Path $destRoot $dir.Name
            if (Test-Path $dest) {
                Write-Verbose ("[backup-migrate] Skipping existing {0} already on W:" -f $dir.Name)
                continue
            }
            try {
                Write-LauncherLog "[backup-migrate] Moving $($dir.Name) -> $destRoot"
                Move-Item -Path $dir.FullName -Destination $dest -Force -ErrorAction Stop
                $moved++
            }
            catch {
                Write-LauncherLog "[backup-migrate] WARN move failed $($dir.Name): $($_.Exception.Message)"
            }
        }
    }
    if ($newest -and -not (Test-Path (Join-Path $destRoot $newest.Name))) {
        try {
            Write-LauncherLog "[backup-migrate] Copying newest $($newest.Name) to W: (preserve local copy)"
            Copy-Item -Path $newest.FullName -Destination (Join-Path $destRoot $newest.Name) -Recurse -Force -ErrorAction Stop
            $copied++
        }
        catch {
            Write-LauncherLog "[backup-migrate] WARN copy newest failed $($newest.Name): $($_.Exception.Message)"
        }
    }
    # Down-sync: if W: holds a newer backup than local newest (or local newest missing), copy newest W: back locally
    try {
        $wBackups = Get-ChildItem -Path $destRoot -Directory -ErrorAction Stop | Where-Object { $_.Name -match '_[0-9]{8}_[0-9]{6}Z?' }
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
                        Copy-Item -Path $wNewest.FullName -Destination $destLocal -Recurse -Force -ErrorAction Stop
                    }
                    catch { Write-LauncherLog "[backup-migrate] WARN down-sync failed $($wNewest.Name): $($_.Exception.Message)" }
                }
            }
        }
    }
    catch { Write-LauncherLog "[backup-migrate] WARN down-sync scan failed: $($_.Exception.Message)" }
    Write-LauncherLog "[backup-migrate] summary moved=$moved copied=$copied"
}
if (-not $NoBackupMigrate) {
    Invoke-MigrateLocalBackupsToWDrive
}
else {
    Write-Host "[backup-migrate] disabled via -NoBackupMigrate"
}

# Sentinel file to ensure we open the browser only once automatically
$BrowserSentinel = Join-Path $RunDir 'browser_opened_once'

# PID & log paths (port included for future multi-instance separation)
$PidFile = Join-Path $RunDir "von_${Port}.pid"
$CurrentLog = Join-Path $LogsDir "von_${Port}_current.log"
$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$NewLog = Join-Path $LogsDir "von_${Port}_${Timestamp}.log"
$RagPidFile = Join-Path $RunDir "rag_worker.pid"
$RagLogFile = Join-Path $LogsDir "rag_worker_${Timestamp}.log"

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
        $owning = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1 -ExpandProperty OwningProcess)
        if ($owning) { return (Get-Process -Id $owning -ErrorAction Stop) }
    }
    catch { }
    return $null
}

function Read-AdminToken {
    if ($AdminToken) { return $AdminToken }
    $tokenFile = Join-Path $RunDir 'admin_token.txt'
    if (Test-Path $tokenFile) { return (Get-Content $tokenFile -Raw).Trim() }
    $gen = [guid]::NewGuid().ToString('N')
    Set-Content $tokenFile $gen
    return $gen
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
    if (-not $allowedValues -or $allowedValues.Count -eq 0) {
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

function Invoke-DailyBackupIfDue {
    <#
        Performs a non-blocking (background job) backup of the remote DB
        at most once per interval (default 24h) using scripts/backup_von_db.py.
        Skips if VON_DISABLE_DAILY_BACKUP is set to a truthy value.
        Supports schedule via VON_BACKUP_SCHEDULE (cron-like string) using 5 fields:
        "<minute> <hour> <day-of-month> <month> <day-of-week>".
        Falls back to interval-based schedule via VON_BACKUP_INTERVAL_HOURS if the cron
        string is missing or unsupported.
        Writes ISO8601 UTC timestamp to last_backup_utc.txt sentinel on success.
        Logs are prefixed with [daily-backup].
    #>
    if ($env:VON_DISABLE_DAILY_BACKUP -and $env:VON_DISABLE_DAILY_BACKUP.ToString() -match '^(1|true|yes)$') {
        return
    }
    $schedule = $null
    if ($env:VON_BACKUP_SCHEDULE -and $env:VON_BACKUP_SCHEDULE.ToString().Trim()) {
        $schedule = $env:VON_BACKUP_SCHEDULE.ToString().Trim().Trim('"')
    }
    $intervalHours = 24
    try { if ($env:VON_BACKUP_INTERVAL_HOURS) { $intervalHours = [int]$env:VON_BACKUP_INTERVAL_HOURS } } catch { }
    if ($intervalHours -lt 1) { $intervalHours = 24 }
    $sentinel = Join-Path $RunDir 'last_backup_utc.txt'
    $last = $null
    if (Test-Path $sentinel) {
        try { $last = [DateTime]::Parse((Get-Content $sentinel -Raw).Trim()).ToUniversalTime() } catch { $last = $null }
    }
    $nowUtc = (Get-Date).ToUniversalTime()
    $due = $true
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
    if (-not $due) { return }
    # Avoid launching duplicate job
    $existingJob = Get-Job -Name 'von_daily_backup' -ErrorAction SilentlyContinue | Where-Object { $_.State -in 'Running', 'NotStarted' }
    if ($existingJob) { return }
    $backupScript = Join-Path $Root 'scripts/backup_von_db.py'
    if (-not (Test-Path $backupScript)) {
        Write-LauncherLog "[daily-backup] WARN: backup script missing: $backupScript (skipping)"
        return
    }
    $pdmExe = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    Write-LauncherLog "[daily-backup] Launching background backup (interval ${intervalHours}h)..."
    Start-Job -Name 'von_daily_backup' -ScriptBlock {
        param($pdmExe, $root, $runDir, $sentinelPath, $backupRoot, $backupScript)
        try {
            Set-Location $root
            # Force remote; disable local fallback for this backup invocation
            $env:MONGO_ALLOW_LOCAL_FALLBACK = '0'
            & $pdmExe run python $backupScript --apply --out-dir $backupRoot --tag auto-daily 2>&1 | ForEach-Object { "[daily-backup] $_" }
            if ($LASTEXITCODE -ne 0) { throw "backup script failed with exit code $LASTEXITCODE" }
            (Get-Date).ToUniversalTime().ToString('o') | Set-Content $sentinelPath
            Write-Host '[daily-backup] Completed.'
        }
        catch {
            Write-Host ("[daily-backup] ERROR: {0}" -f $_.Exception.Message)
        }
    } -ArgumentList $pdmExe, $Root, $RunDir, $sentinel, $BackupRoot, $backupScript | Out-Null
}

# Backward-compatible convenience: if called as ".\run.ps1 -BackupDryRun" (no explicit action)
# then run the backup action, even if the server is already running.
if ($Action -eq 'start' -and ($BackupDryRun -or $BackupTag -or $BackupOutDir)) {
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

function Test-VonHealthEndpoint($Port) {
    # was Health-Check
    $hosts = @('127.0.0.1', 'localhost')
    $httpTimeout = 5
    try {
        $envTimeout = [int]$env:VON_HEALTH_HTTP_TIMEOUT
        if ($envTimeout -gt 0 -and $envTimeout -lt 61) { $httpTimeout = $envTimeout }
    }
    catch { }
    foreach ($h in $hosts) {
        $url = "http://${h}:$Port/health"
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $httpTimeout
            if ($resp.StatusCode -eq 200) { return $true }
            elseif ($HealthDebug) { Write-LauncherLog "Health attempt $url status=$($resp.StatusCode)" }
        }
        catch {
            if ($HealthDebug) { Write-LauncherLog "Health attempt failed $url : $($_.Exception.Message)" }
        }
    }
    return $false
}

function Test-VonPortListening($Port) {
    # was Test-PortListening
    try {
        $own = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
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

    Write-LauncherLog "Starting RAG Indexing Worker..."
    $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }

    $commandToRun = "& `"$pdm`" run python -u src/backend/utilities/rag_indexing_worker.py"
    $psExe = if (Get-Command 'pwsh' -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell.exe' }

    $proc = Start-Process -FilePath $psExe -ArgumentList @('-NoLogo', '-NoProfile', '-Command', "$commandToRun *>> `"$RagLogFile`"") -WorkingDirectory $Root -PassThru -WindowStyle Hidden

    $startIso = (Get-Date).ToString('o')
    Set-Content $RagPidFile "PID=$($proc.Id)`nSTART=$startIso"
    Write-LauncherLog "RAG Worker started (PID=$($proc.Id)). Log: $RagLogFile"
}

function Stop-RagWorker {
    if (-not (Test-Path $RagPidFile)) { return }
    $content = Get-Content $RagPidFile -Raw -ErrorAction SilentlyContinue
    if ($content -match 'PID=([0-9]+)') {
        $pidToKill = [int]$Matches[1]
        Write-LauncherLog "Stopping RAG Worker (PID=$pidToKill)..."
        try { Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue } catch { }
    }
    Remove-Item $RagPidFile -Force -ErrorAction SilentlyContinue
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
    $cmdLine = try { (Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" | Select-Object -ExpandProperty CommandLine) } catch { '' }
    if ($cmdLine -and $cmdLine -like '*src/workflows/von/main.py*') {
        Write-PidFile $listenerPid
        Write-LauncherLog ("Synchronized PID file to listener PID={0}" -f $listenerPid)
    }
}

function Start-VonServer {
    # was Start-Von
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
        Write-LauncherLog "Already running (PID=$($existing.Id)). Use .\run.ps1 stop or restart."; return
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
            Write-LauncherLog ("Port {0} already in use by PID={1}; assuming server already running (untracked)." -f $Port, $listener.Id)
            Write-PidFile $listener.Id
            return
        }
        elseif ($currentPidInFile -ne $listener.Id) {
            Write-LauncherLog ("Port {0} owned by PID={1} but PID file had PID={2}; updating PID file to listener." -f $Port, $listener.Id, $currentPidInFile)
            Write-PidFile $listener.Id
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
    $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    # Launch background (no wrapper file; redirect all streams in child shell)
    Write-LauncherLog "Starting Von server on port $Port ..."
    if (-not (Test-Path $NewLog)) { New-Item -ItemType File -Path $NewLog -Force | Out-Null }
    # Build command string for child PowerShell; use double quotes outside and escape internal quotes minimally
    # Obtain python path via pdm (pdm run python ...) is slower; prefer invoking 'pdm run' once to resolve environment then run program.
    # Simpler: use pdm to run python directly with arguments; we still get a wrapper process (pdm) so we attempt to resolve actual python child later.
    $commandToRun = "& `"$pdm`" run python -u src/workflows/von/main.py --port $Port"
    # Detect available PowerShell executable (prefer pwsh/Core but fallback to Windows PowerShell)
    $psExe = if (Get-Command 'pwsh' -ErrorAction SilentlyContinue) { 'pwsh' } else { 'powershell.exe' }
    $proc = Start-Process -FilePath $psExe -ArgumentList @('-NoLogo', '-NoProfile', '-Command', "$commandToRun *>> `"$NewLog`"") -WorkingDirectory $Root -PassThru -WindowStyle Hidden
    # Initial write uses launcher (pdm shell) PID; we'll refine after short delay by finding child python process if present.
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
        $attempt = 0; $healthy = $false; $listeningLogged = $false
        while ($attempt -lt $maxAttempts) {
            Start-Sleep -Milliseconds 500
            # Early crash detection: if the launched process has exited and the port never began listening, abort fast
            if (-not $listeningLogged) {
                try {
                    $procCheck = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
                }
                catch { $procCheck = $null }
                if (-not $procCheck) {
                    Write-LauncherLog "ERROR: Server process exited early before listening on port $Port. Showing last 40 log lines:"
                    $logTail = @()
                    if (Test-Path $CurrentLog) {
                        try {
                            $logTail = Get-Content $CurrentLog -Tail 40
                            $logTail | ForEach-Object { Write-Host $_ }
                        }
                        catch { Write-LauncherLog "(Log tail unavailable: $($_.Exception.Message))" }
                    }

                    # Auto-repair logic for missing dependencies
                    if (-not $script:RepairAttempted) {
                        $logText = $logTail -join "`n"
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
            if (Test-VonHealthEndpoint $Port) { $healthy = $true; break }
            elseif ($HealthDebug -and ($attempt % 4 -eq 0)) { Write-LauncherLog ("Health not ready yet (attempt {0}/{1})" -f $attempt, $maxAttempts) }
            if (-not $DisableLogReady -and $ReadyLogPatterns -and (Test-VonLogReady -LogPath $CurrentLog -Patterns $ReadyLogPatterns)) {
                Write-LauncherLog "Detected readiness log pattern; marking healthy (log shortcut)."
                $healthy = $true; break
            }
            $attempt++
        }
        if (-not $healthy -and $listeningLogged -and $HealthGraceSec -gt 0) {
            Write-LauncherLog "Extending wait up to ${HealthGraceSec}s for /health (phase 2)..."
            $graceAttempts = [int]($HealthGraceSec * 2)
            $g = 0
            while ($g -lt $graceAttempts -and -not $healthy) {
                Start-Sleep -Milliseconds 500
                if (Test-VonHealthEndpoint $Port) { $healthy = $true; break }
                elseif ($HealthDebug -and ($g % 10 -eq 0)) { Write-LauncherLog ("Grace wait health not ready ({0}s/{1}s)" -f ([int]($g / 2)), $HealthGraceSec) }
                if (-not $DisableLogReady -and $ReadyLogPatterns -and (Test-VonLogReady -LogPath $CurrentLog -Patterns $ReadyLogPatterns)) {
                    Write-LauncherLog "Detected readiness log pattern during grace; marking healthy (log shortcut)."
                    $healthy = $true; break
                }
                if (($g % 10) -eq 0) {
                    # every 5s
                    $elapsed = [int]($g / 2)
                    Write-LauncherLog "Still waiting for /health... ${elapsed}s/${HealthGraceSec}s grace"
                }
                $g++
            }
        }
        if ($healthy) {
            Write-LauncherLog "Server healthy (http://localhost:$Port)"
            if (-not $NoBrowser) {
                $shouldOpen = $false
                if ($ForceBrowser) { $shouldOpen = $true }
                elseif (-not (Test-Path $BrowserSentinel)) { $shouldOpen = $true }
                if ($shouldOpen) {
                    try {
                        $url = "http://localhost:$Port/"
                        # Try to open with Chrome specifically
                        $chromePaths = @(
                            "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
                            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                            "${env:LocalAppData}\Google\Chrome\Application\chrome.exe"
                        )
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
                        if (-not (Test-Path $BrowserSentinel)) { Set-Content $BrowserSentinel (Get-Date).ToString('o') }
                        if ($ForceBrowser) { Write-LauncherLog "Opened browser (forced): $browserUsed" } else { Write-LauncherLog "Opened browser (first launch): $browserUsed" }
                    }
                    catch { Write-LauncherLog "Browser open attempt failed: $($_.Exception.Message)" }
                }
                else {
                    Write-LauncherLog "Browser already opened previously (use -ForceBrowser to open again)."
                }
            }
        }
        elseif ($listeningLogged) {
            Write-LauncherLog "WARNING: Port is listening but /health did not respond in ${HealthTimeoutSec + $HealthGraceSec}s; continuing (service may still be initializing)."
        }
        else {
            Write-LauncherLog "WARNING: Server not healthy after initial ${HealthTimeoutSec}s (port not listening); check logs: $CurrentLog"
        }
    }
    # Attempt to refine PID to the actual python process (child of wrapper) after startup
    try {
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
    catch { Write-LauncherLog "PID refinement skipped: $($_.Exception.Message)" }
    # Final sync (in case refinement missed due to timing)
    Sync-PidFileToListener
    Write-LauncherLog "Admin token file: $(Join-Path $RunDir 'admin_token.txt')"
    if ($ShowRelationCoverage) {
        try { Invoke-RelationCoverageSummary } catch { Write-LauncherLog "[relation-coverage] ERROR: $($_.Exception.Message)" }
    }
    # Trigger daily backup (non-blocking) if due
    try { Invoke-DailyBackupIfDue } catch { Write-LauncherLog "[daily-backup] ERROR (scheduling failed): $($_.Exception.Message)" }
    # Trigger test DB refresh (non-blocking) if due
    try { Invoke-TestDbRefreshIfDue } catch { Write-LauncherLog "[test-db-refresh] ERROR (scheduling failed): $($_.Exception.Message)" }

    # Start RAG Worker
    try { Start-RagWorker } catch { Write-LauncherLog "[rag-worker] ERROR: $($_.Exception.Message)" }
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
            if ($resp.StatusCode -eq 200) { $graceful = $true }
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
        try { Stop-Process -Id $targetPid -Force -ErrorAction Stop } catch { Write-LauncherLog "Force kill failed: $($_.Exception.Message)" }
    }
    else {
        if ($graceful) { Write-LauncherLog "Graceful shutdown completed." } else { Write-LauncherLog "Process exited." }
    }
    if ((Test-Path $PidFile) -and (Select-String -Path $PidFile -Pattern "PID=$targetPid" -Quiet)) { Remove-PidFile }

    # Stop RAG Worker
    Stop-RagWorker
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
    Write-LauncherLog "Log: $CurrentLog"
    try { Invoke-DailyGovernanceScan -Port $Port -StartupHealthy $healthy } catch { Write-LauncherLog "[governance-scan] ERROR: $($_.Exception.Message)" }
    try { Invoke-ConceptDataAbsenceCheck } catch { Write-LauncherLog "[concept-data-check] ERROR: $($_.Exception.Message)" }
    try { Invoke-ResidualLegacyTextAudit } catch { Write-LauncherLog "[residual-text-audit] ERROR: $($_.Exception.Message)" }
    try { Invoke-CleanupPreservedFields } catch { Write-LauncherLog "[cleanup-preserved-fields] ERROR: $($_.Exception.Message)" }
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

function Restart-VonServer { Stop-VonServer; Start-VonServer }

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
    $allowLocal = ($env:VON_GOV_SCAN_ALLOW_LOCAL -eq '1')
    $scanner = 'scripts/mark_code_referenced_concepts.py'
    if (-not (Test-Path (Join-Path $root $scanner))) { Write-LauncherLog "[governance-scan] scanner script missing ($scanner)"; return }
    Write-LauncherLog "[governance-scan] Running governance concept tag scan (interval ${intervalHours}h; force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $pdmArgs = @('run', 'python', $scanner, '--execute')
    if ($allowLocal) { $pdmArgs += '--allow-local' }
    $start = Get-Date
    $output = & $pdm @pdmArgs 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $added = $null; $already = $null; $notFound = $null; $success = $false
    $rawOutPath = Join-Path $runDir 'governance_scan_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    try {
        $jsonStart = ($output | Select-String -Pattern '^\{' -SimpleMatch | Select-Object -First 1).LineNumber
        if ($jsonStart -gt 0) {
            $jsonText = ($output | Select-Object -Skip ($jsonStart - 1)) -join "`n"
            $parsed = $null; try { $parsed = $jsonText | ConvertFrom-Json -ErrorAction Stop } catch { }
            if ($parsed) {
                $success = $parsed.success
                $added = ($parsed.added | Measure-Object).Count
                $already = ($parsed.already_marked | Measure-Object).Count
                $notFound = ($parsed.not_found | Measure-Object).Count
                if (-not $success -and $parsed.error) {
                    Write-LauncherLog "[governance-scan] error=$($parsed.error)"
                }
            }
        }
    }
    catch { }
    if ($success) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        $failureCount = 0
        $status = 'success'
        if ($added -eq 0 -and $already -ge 0 -and $notFound -ge 0) { $status = 'no-op' }
        Write-LauncherLog "[governance-scan] $status added=$added already=$already not_found=$notFound elapsed=${elapsedMs}ms failures=$failureCount"
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
    $scriptPath = 'scripts/maintenance/check_concept_data_absence.py'
    if (-not (Test-Path (Join-Path $root $scriptPath))) { Write-LauncherLog "[concept-data-check] script missing ($scriptPath)"; return }
    Write-LauncherLog "[concept-data-check] Running absence check (interval ${IntervalHours}h force=$force)"
    $pdm = if (Test-Path (Join-Path $root '.venv/Scripts/pdm.exe')) { (Join-Path $root '.venv/Scripts/pdm.exe') } else { 'pdm' }
    $start = Get-Date
    $output = & $pdm run python $scriptPath 2>&1
    $end = Get-Date
    $elapsedMs = [int]($end - $start).TotalMilliseconds
    $rawOutPath = Join-Path $runDir 'concept_data_absence_last_output.log'
    try { $output | Out-File -FilePath $rawOutPath -Encoding UTF8 } catch { }
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        try { Set-Content -Path $sentinel -Value ($now.ToString('o')) -Encoding UTF8 } catch { }
        Write-LauncherLog "[concept-data-check] OK (elapsed=${elapsedMs}ms)"
    }
    elseif ($exitCode -eq 2) {
        Write-LauncherLog "[concept-data-check] VIOLATION: concept_data reappeared (elapsed=${elapsedMs}ms) see $rawOutPath"
    }
    else {
        Write-LauncherLog "[concept-data-check] ERROR exit=$exitCode (elapsed=${elapsedMs}ms) see $rawOutPath"
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

## (Removed final duplicate Invoke-RelationCoverageSummary stub here)

function Show-Help {
    @'
Von Launcher Help
    Usage: .\run.ps1 [action] [options]
    Actions: start | foreground | stop | status | restart | logs | check | backup | autoupdate | rag-worker | help
    Options:
        -Port <int>            (reserved future multi-instance)
    -NoBrowser             Do not auto open browser
    -ForceBrowser          Force opening browser even if already opened once
        -LogRetention <n>      Keep last n logs (default 20)
        -Tail <n>              Lines for logs action (default 100)
        -Follow                Stream log (logs action)
        -AdminToken <token>    Provide explicit shutdown token
    -SkipHealth            Do not wait for health during start
        -ShowRelationCoverage  Show relationship coverage summary on start/status
        -HealthTimeoutSec <n>  Seconds to wait for /health (default 60)
        -HealthGraceSec <n>    Extra seconds after port listens to keep waiting (default 45)
        -ReadyLogPatterns <p>  One or more substrings that indicate readiness (log shortcut)
        -DisableLogReady       Disable log pattern readiness shortcut
        -HealthDebug           Verbose health polling diagnostics
        -BackupDryRun           For backup action: do not run mongodump (prints what would happen)
        -BackupTag <tag>        For backup action: tag suffix for backup dir (default manual)
        -BackupOutDir <path>    For backup action: output root dir (default VON_BACKUP_ROOT)
        -UpdateIntervalMinutes <n>  Minutes between git update checks (autoupdate action; default 60)
        -UpdateBranch <name>        Branch to track (default main)
        -UpdateNoRestartIfRunning   Skip restart if server already running (still pull code)
    Single-tab behavior: Browser auto-opens only on first successful start; subsequent restarts reuse the same tab unless -ForceBrowser is supplied. Sentinel: .run/browser_opened_once

    Examples:
        .\run.ps1 start
        .\run.ps1 status
        .\run.ps1 check          # returns exit code (0 healthy, 2 unhealthy, 3 not running)
        .\run.ps1 logs -Tail 200 -Follow
        .\run.ps1 backup -BackupDryRun
        .\run.ps1 backup -BackupTag manual
        .\run.ps1 backup -BackupTag pre-change -BackupOutDir .\backups
        .\run.ps1 stop
    .\run.ps1 stop 12345        # kill specific PID directly
    .\run.ps1 stop force        # detect by port, verify command line, then kill
    .\run.ps1 stop force-any    # detect by port, skip verification, then kill
        .\run.ps1 foreground
        .\run.ps1 autoupdate -UpdateIntervalMinutes 30 -UpdateBranch main
'
'@ | Write-Host
}

function Invoke-BackupNow {
    <#
        Run an on-demand DB backup without restarting the server.
        Defaults:
          - Output directory: VON_BACKUP_ROOT (resolved at launch)
          - Tag: "manual"
          - Mode: apply unless -BackupDryRun
    #>
    $backupScript = Join-Path $Root 'scripts/backup_von_db.py'
    if (-not (Test-Path $backupScript)) {
        Write-LauncherLog "[backup] ERROR: backup script missing: $backupScript"
        return
    }
    $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
    $tag = if ($BackupTag) { $BackupTag } else { 'manual' }
    $outDir = if ($BackupOutDir) { $BackupOutDir } else { $BackupRoot }
    $apply = -not $BackupDryRun
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
    }
    else {
        Write-LauncherLog "[backup] ERROR exit=$exitCode"
    }
}

switch ($Action) {
    'start' { Start-VonServer }
    'foreground' {
        Write-LauncherLog "Running in foreground... (Ctrl+C to stop)"
        $env:VON_ADMIN_TOKEN = Read-AdminToken
        $env:PYTHONUNBUFFERED = '1'; $env:PYTHONPATH = $Root
        # Ensure Python runs in UTF-8 mode in foreground too
        $env:PYTHONUTF8 = '1'
        $env:PYTHONIOENCODING = 'utf-8'
        $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
        & $pdm run python -u src\workflows\von\main.py
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
                    $cmdLine = try { (Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.Id)" | Select-Object -ExpandProperty CommandLine) } catch { '' }
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
    'restart' { Restart-VonServer }
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
        $pdm = if (Test-Path (Join-Path $Root '.venv\Scripts\pdm.exe')) { Join-Path $Root '.venv\Scripts\pdm.exe' } else { 'pdm' }
        & $pdm run python -u src/backend/utilities/rag_indexing_worker.py
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


