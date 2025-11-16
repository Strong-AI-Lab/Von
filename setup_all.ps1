<#
.SYNOPSIS
    Unified environment setup for Von-Private (Python + JavaScript).

.DESCRIPTION
    Orchestrates Python (pdm/venv) and JavaScript (npm) setup using existing
    scripts: setup_py.ps1 and setup_js.ps1. Adds flags for selective steps,
    force reinstall, CI-friendly mode, and Python version selection.

.PARAMETER SkipPy
    Skip Python environment setup.

.PARAMETER SkipJS
    Skip JavaScript dependency setup.

.PARAMETER Force
    Force reinstall: passes -Reset to setup_py.ps1 and -Force to setup_js.ps1.

.PARAMETER CI
    CI mode: quieter JS install, no browser opening side-effects.

.PARAMETER ConfigureVSCode
    Forwarded to setup_py.ps1 to configure VS Code settings.

.PARAMETER PythonVersion
    Forwarded to setup_py.ps1 to target a specific Python minor version.

.PARAMETER SkipMongoDB
    Skip MongoDB installation checks (forwarded to setup_py.ps1).

.EXAMPLES
    ./setup_all.ps1
    ./setup_all.ps1 -ConfigureVSCode
    ./setup_all.ps1 -SkipJS
    ./setup_all.ps1 -SkipPy -Force
    ./setup_all.ps1 -CI -PythonVersion 3.12
    ./setup_all.ps1 -SkipMongoDB -ConfigureVSCode

.NOTES
    Exit codes: 0 success, non-zero on any failed phase.
#>
param(
    [switch]$SkipPy,
    [switch]$SkipJS,
    [switch]$Force,
    [switch]$CI,
    [switch]$ConfigureVSCode,
    [string]$PythonVersion,
    [switch]$SkipMongoDB
)

$ErrorActionPreference = 'Stop'
$overallOk = $true
$root = $PSScriptRoot
$start = Get-Date

function Write-Phase { param([string]$Msg,[ConsoleColor]$Color=[ConsoleColor]::Cyan) Write-Host $Msg -ForegroundColor $Color }
function Write-Err   { param([string]$Msg) Write-Host $Msg -ForegroundColor Red }
function Duration($since) { return [int]((Get-Date) - $since).TotalSeconds }

Write-Phase "=== Von-Private Unified Setup ==="
Write-Host  ("Root: {0}" -f $root)

# Python Phase
if (-not $SkipPy) {
    # Build parameter hashtable to avoid positional confusion
    $pyParams = @{}
    if ($ConfigureVSCode) { $pyParams['ConfigureVSCode'] = $true }
    if ($Force) { $pyParams['Reset'] = $true }
    if ($PythonVersion) { $pyParams['PythonVersion'] = $PythonVersion }
    if ($SkipMongoDB) { $pyParams['SkipMongoDB'] = $true }
    $pyArgsDisplay = ($pyParams.GetEnumerator() | ForEach-Object {
        $arg = '-' + $_.Key
        if ($_.Value -is [string]) { $arg += ' ' + $_.Value }
        $arg
    }) -join ' '
    Write-Phase ("[1/2] Python setup starting (args: {0})" -f $pyArgsDisplay)
    $pyStart = Get-Date
    try {
    & "$root/setup_py.ps1" @pyParams
        if ($LASTEXITCODE -ne 0) { throw "setup_py.ps1 exited $LASTEXITCODE" }
        Write-Phase ("Python setup completed in {0}s" -f (Duration $pyStart)) 'Green'
    } catch {
        Write-Err "Python setup failed: $($_.Exception.Message)"
        $overallOk = $false
    }
} else {
    Write-Phase '[1/2] Python setup skipped (-SkipPy)' 'Yellow'
}

# JavaScript Phase
if (-not $SkipJS) {
    $jsArgs = @()
    if ($Force) { $jsArgs += '-Force' }
    if ($CI) { $jsArgs += '-CI' }
    Write-Phase ("[2/2] JavaScript setup starting (args: {0})" -f ($jsArgs -join ' '))
    $jsStart = Get-Date
    try {
        & "$root/setup_js.ps1" @jsArgs
        if ($LASTEXITCODE -ne 0) { throw "setup_js.ps1 exited $LASTEXITCODE" }
        Write-Phase ("JavaScript setup completed in {0}s" -f (Duration $jsStart)) 'Green'
    } catch {
        Write-Err "JavaScript setup failed: $($_.Exception.Message)"
        $overallOk = $false
    }
} else {
    Write-Phase '[2/2] JavaScript setup skipped (-SkipJS)' 'Yellow'
}

$elapsed = Duration $start
if ($overallOk) {
    Write-Phase ("=== Unified setup SUCCESS in {0}s ===" -f $elapsed) 'Green'
    exit 0
} else {
    Write-Err   ("=== Unified setup FAILED in {0}s (see above) ===" -f $elapsed)
    exit 1
}
