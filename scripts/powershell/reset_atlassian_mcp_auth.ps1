# Reset Atlassian MCP auth/config rows from VS Code state.vscdb.
# NZ English spelling.
#
# Why this exists:
# - Some Atlassian MCP failures loop on invalid_token/Canceled despite successful OAuth UI.
# - VS Code may keep stale dynamic auth/session rows in state.vscdb.
#
# Safety:
# - Dry-run by default.
# - Creates timestamped DB backups by default in apply mode.
# - Refuses to apply while target VS Code host is still running.

[CmdletBinding()]
param(
    [ValidateSet('Insiders', 'Stable', 'Both')]
    [string]$VSCodeProfile = 'Insiders',

    # Dry-run is default. Use -Apply to write.
    [switch]$Apply,

    # Optional compatibility flag for explicit dry-run invocation.
    [object]$DryRun,

    [switch]$NoBackup
)

$ErrorActionPreference = 'Stop'

if ($Apply) {
    $isInVSCodeTerminal = $false
    if ($env:TERM_PROGRAM -eq 'vscode') { $isInVSCodeTerminal = $true }
    if ($env:VSCODE_PID) { $isInVSCodeTerminal = $true }
    if ($isInVSCodeTerminal) {
        throw "Refusing to run in apply mode from the VS Code integrated terminal. Fully close the selected VS Code host, then run this script from an external PowerShell window."
    }
}

function ConvertTo-BooleanOrNull([object]$value) {
    if ($null -eq $value) { return $null }
    if ($value -is [bool]) { return [bool]$value }
    if ($value -is [int] -or $value -is [long]) {
        if ([int]$value -eq 0) { return $false }
        if ([int]$value -eq 1) { return $true }
        throw "DryRun numeric values must be 0 or 1. Got: $value"
    }

    $text = [string]$value
    $text = $text.Trim()
    if ($text.StartsWith('$')) { $text = $text.Substring(1) }

    switch ($text.ToLowerInvariant()) {
        'true' { return $true }
        'false' { return $false }
        '0' { return $false }
        '1' { return $true }
        default { throw "DryRun must be true/false (or 1/0). Got: '$value'" }
    }
}

function Get-ProfileRoot([string]$profileName) {
    switch ($profileName) {
        'Insiders' { return Join-Path $env:APPDATA 'Code - Insiders\User' }
        'Stable' { return Join-Path $env:APPDATA 'Code\User' }
        default { throw "Unsupported profile: $profileName" }
    }
}

function Get-SelectedProfiles([string]$profileName) {
    switch ($profileName) {
        'Insiders' { return @('Insiders') }
        'Stable' { return @('Stable') }
        'Both' { return @('Insiders', 'Stable') }
        default { throw "Unsupported profile: $profileName" }
    }
}

function Assert-HostNotRunning([string]$profileName) {
    $running = switch ($profileName) {
        'Insiders' {
            Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq 'Code - Insiders' }
        }
        'Stable' {
            Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq 'Code' }
        }
        default { @() }
    }

    if ($running) {
        throw "VS Code profile '$profileName' appears to be running. Fully close it first, then re-run with -Apply."
    }
}

function Invoke-DbReset([string]$dbPath, [bool]$dryRun) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "python not found on PATH; needed to query/update sqlite state.vscdb."
    }

    $py = @'
import sqlite3
import sys

db = sys.argv[1]
dry_run = sys.argv[2].lower() == "true"

patterns = [
    ("secret://%mcp.atlassian.com/%", "Atlassian dynamic auth/secret rows"),
    ("mcp.config.%https://mcp.atlassian.com/%", "Atlassian MCP config rows"),
]

def count_for_pattern(cur, like_pattern):
    cur.execute("SELECT COUNT(*) FROM ItemTable WHERE key LIKE ?", (like_pattern,))
    row = cur.fetchone()
    return int(row[0] if row else 0)

conn = sqlite3.connect(db)
cur = conn.cursor()

before = []
for p, label in patterns:
    before.append((p, label, count_for_pattern(cur, p)))

would_delete = sum(n for _, _, n in before)

if dry_run:
    print(f"mode=dry-run")
    print(f"would_delete_total={would_delete}")
    for p, label, n in before:
        print(f"would_delete pattern={p} count={n} label={label}")
    conn.close()
    raise SystemExit(0)

for p, _ in patterns:
    cur.execute("DELETE FROM ItemTable WHERE key LIKE ?", (p,))

conn.commit()
deleted = conn.total_changes

after = []
for p, label in patterns:
    after.append((p, label, count_for_pattern(cur, p)))

print(f"mode=apply")
print(f"deleted_total={deleted}")
for p, label, n in after:
    print(f"remaining pattern={p} count={n} label={label}")

conn.close()
'@

    $tmpPyPath = Join-Path $env:TEMP ("atlassian_mcp_reset_{0}.py" -f ([guid]::NewGuid().ToString('N')))
    try {
        Set-Content -LiteralPath $tmpPyPath -Value $py -Encoding UTF8
        & $python.Source $tmpPyPath $dbPath ([string]$dryRun)
    }
    finally {
        Remove-Item -LiteralPath $tmpPyPath -Force -ErrorAction SilentlyContinue
    }
}

$dryRunOverride = $null
if ($PSBoundParameters.ContainsKey('DryRun')) {
    $dryRunOverride = ConvertTo-BooleanOrNull $DryRun
}

$effectiveDryRun = $true
if ($null -ne $dryRunOverride) {
    $effectiveDryRun = $dryRunOverride
}
elseif ($Apply) {
    $effectiveDryRun = $false
}

$profiles = Get-SelectedProfiles $VSCodeProfile

Write-Output "=== Atlassian MCP auth reset ==="
Write-Output "Profile selection: $VSCodeProfile"
Write-Output "DryRun: $effectiveDryRun"
if ($effectiveDryRun) {
    Write-Output "NOTE: Dry-run mode. No database writes will be made."
    Write-Output "Tip: re-run with -Apply after closing VS Code/Insiders."
}

if (-not $effectiveDryRun) {
    foreach ($p in $profiles) {
        Assert-HostNotRunning $p
    }
}

foreach ($profile in $profiles) {
    $profileRoot = Get-ProfileRoot $profile
    $dbPath = Join-Path (Join-Path $profileRoot 'globalStorage') 'state.vscdb'

    Write-Output ""
    Write-Output "--- Profile: $profile ---"
    Write-Output "DB: $dbPath"

    if (-not (Test-Path $dbPath)) {
        Write-Output "Skipping: state.vscdb not found."
        continue
    }

    if ((-not $effectiveDryRun) -and (-not $NoBackup)) {
        $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
        $backupPath = "$dbPath.bak_$stamp"
        Copy-Item -LiteralPath $dbPath -Destination $backupPath -Force
        Write-Output "Backup: $backupPath"
    }

    Invoke-DbReset $dbPath $effectiveDryRun
}

Write-Output ""
Write-Output "Done."
