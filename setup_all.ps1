<#
.SYNOPSIS
    Unified environment setup for Von (Python + JavaScript).

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

.PARAMETER WithOCR
    Require image and scanned-PDF OCR support. This validates Tesseract but
    does not install system software.

.PARAMETER InstallSystemDeps
    Explicitly authorise installation of required system dependencies. On
    Windows this currently installs Tesseract with winget and implies -WithOCR.

.EXAMPLES
    ./setup_all.ps1
    ./setup_all.ps1 -ConfigureVSCode
    ./setup_all.ps1 -SkipJS
    ./setup_all.ps1 -SkipPy -Force
    ./setup_all.ps1 -CI -PythonVersion 3.12
    ./setup_all.ps1 -SkipMongoDB -ConfigureVSCode
    ./setup_all.ps1 -WithOCR
    ./setup_all.ps1 -InstallSystemDeps

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
    [switch]$SkipMongoDB,
    [switch]$WithOCR,
    [switch]$InstallSystemDeps
)

$ErrorActionPreference = 'Stop'
$overallOk = $true
$root = $PSScriptRoot
$start = Get-Date

function Write-Phase { param([string]$Msg,[ConsoleColor]$Color=[ConsoleColor]::Cyan) Write-Host $Msg -ForegroundColor $Color }
function Write-Err   { param([string]$Msg) Write-Host $Msg -ForegroundColor Red }
function Duration($since) { return [int]((Get-Date) - $since).TotalSeconds }

function Write-OcrSummary {
    param(
        [ValidateSet('available', 'unavailable', 'failed')]
        [string]$State,
        [bool]$Required
    )

    $requiredText = if ($Required) { 'true' } else { 'false' }
    Write-Phase '=== OCR Capability Summary ==='
    switch ($State) {
        'available' { Write-Host 'OCR AVAILABLE: Tesseract, English language data, and a real OCR smoke test passed.' -ForegroundColor Green }
        'unavailable' { Write-Host 'OCR UNAVAILABLE: core setup can run, but image and scanned-PDF OCR is not available.' -ForegroundColor Yellow }
        'failed' { Write-Host 'OCR FAILED: OCR setup or validation did not complete successfully.' -ForegroundColor Red }
    }
    Write-Host ("VON_SETUP_CAPABILITY name=ocr state={0} required={1}" -f $State, $requiredText)
    if ($State -ne 'available') {
        Write-Host 'NEXT ACTION: .\setup_all.ps1 -InstallSystemDeps' -ForegroundColor Yellow
        Write-Host 'VON_SETUP_NEXT_ACTION command=".\setup_all.ps1 -InstallSystemDeps"'
    }
}

if ($InstallSystemDeps) {
    $WithOCR = $true
}

if ($SkipPy -and $WithOCR) {
    Write-Err 'Invalid options: OCR setup requires the Python phase; do not combine -SkipPy with -WithOCR or -InstallSystemDeps.'
    Write-OcrSummary -State 'failed' -Required $true
    Write-Err '=== SETUP: INCOMPLETE (invalid OCR options) ==='
    Write-Host 'VON_SETUP_RESULT state=incomplete scope=setup reason=invalid_ocr_options'
    exit 2
}

# setup_py.ps1 runs in this PowerShell process and publishes its final OCR state
# here so the unified summary can report optional as well as required outcomes.
$env:VON_SETUP_OCR_STATE = ''

Write-Phase "=== Von Unified Setup ==="
Write-Host  ("Root: {0}" -f $root)

# Python Phase
if (-not $SkipPy) {
    # Build parameter hashtable to avoid positional confusion
    $pyParams = @{}
    if ($ConfigureVSCode) { $pyParams['ConfigureVSCode'] = $true }
    if ($Force) { $pyParams['Reset'] = $true }
    if ($PythonVersion) { $pyParams['PythonVersion'] = $PythonVersion }
    if ($SkipMongoDB) { $pyParams['SkipMongoDB'] = $true }
    if ($WithOCR) { $pyParams['WithOCR'] = $true }
    if ($InstallSystemDeps) { $pyParams['InstallSystemDeps'] = $true }
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
        if ($env:VON_SETUP_OCR_STATE -eq 'available' -or -not $env:VON_SETUP_OCR_STATE) {
            $env:VON_SETUP_OCR_STATE = 'failed'
        }
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
$ocrState = $env:VON_SETUP_OCR_STATE
if ($ocrState -notin @('available', 'unavailable', 'failed')) {
    $ocrState = if ($SkipPy) { 'unavailable' } else { 'failed' }
}
Write-OcrSummary -State $ocrState -Required ([bool]$WithOCR)
if ($WithOCR -and $ocrState -ne 'available') {
    $overallOk = $false
}

if ($overallOk) {
    if ($ocrState -eq 'available') {
        Write-Phase ("=== FULL SETUP: COMPLETE in {0}s ===" -f $elapsed) 'Green'
        Write-Host 'VON_SETUP_RESULT state=complete scope=full'
    }
    else {
        Write-Phase ("=== CORE SETUP: COMPLETE (OCR unavailable) in {0}s ===" -f $elapsed) 'Yellow'
        Write-Host 'VON_SETUP_RESULT state=complete scope=core reason=ocr_unavailable'
    }
    exit 0
} else {
    Write-Err   ("=== SETUP: INCOMPLETE in {0}s (see above) ===" -f $elapsed)
    Write-Host 'VON_SETUP_RESULT state=incomplete scope=setup'
    exit 1
}
