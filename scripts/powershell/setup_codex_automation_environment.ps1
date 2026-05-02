<#
.SYNOPSIS
    Prepare a Von checkout or Codex automation worktree for Python validation.

.DESCRIPTION
    This is a narrow bootstrap for unattended Codex automation runs. It creates
    or repairs the repo-local .venv, installs PDM inside that venv, verifies
    core test imports, and runs PDM dependency installation only when the
    checkout-local environment is new, incomplete, or explicitly forced.

    Unlike setup_py.ps1, this script intentionally avoids machine-global setup:
    it does not install MongoDB, Tesseract, uv tools, VS Code settings, or read
    .env. It is safe to call from Codex local-environment setup scripts and from
    automation prompts before running pytest or repo helper commands.

.PARAMETER RepoRoot
    Repository root to prepare. Defaults to the root containing this script.

.PARAMETER PythonVersion
    Optional Python minor version for the py launcher, for example 3.13.

.PARAMETER SkipDependencyInstall
    Create/repair the venv and PDM only; do not run PDM install even if imports
    are missing.

.PARAMETER ForceDependencyInstall
    Run PDM dependency installation even when the existing .venv already
    satisfies the verification imports.

.PARAMETER UpgradePip
    Upgrade pip even when reusing an existing .venv. Fresh venvs always upgrade
    pip before installing PDM.

.PARAMETER SyncClean
    Use 'pdm sync --clean' instead of 'pdm install'. This may remove extra
    packages from the repo-local venv, so it is opt-in.

.PARAMETER InstallNode
    Also install JavaScript dependencies with npm ci/install.

.PARAMETER VerbosePdm
    Pass --verbose to PDM.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\setup_codex_automation_environment.ps1

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\setup_codex_automation_environment.ps1 -PythonVersion 3.13
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot,

    [Parameter(Mandatory = $false)]
    [string]$PythonVersion,

    [Parameter(Mandatory = $false)]
    [switch]$SkipDependencyInstall,

    [Parameter(Mandatory = $false)]
    [switch]$ForceDependencyInstall,

    [Parameter(Mandatory = $false)]
    [switch]$UpgradePip,

    [Parameter(Mandatory = $false)]
    [switch]$SyncClean,

    [Parameter(Mandatory = $false)]
    [switch]$InstallNode,

    [Parameter(Mandatory = $false)]
    [switch]$VerbosePdm,

    [Parameter(Mandatory = $false)]
    [string[]]$VerifyImports = @("flask", "pymongo", "pytest", "mcp")
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[codex-env] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[codex-env] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[codex-env] $Message" -ForegroundColor Yellow
}

function Resolve-RepoRoot {
    if ($RepoRoot) {
        return (Resolve-Path -LiteralPath $RepoRoot).Path
    }

    $candidate = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
    if (Test-Path -LiteralPath (Join-Path $candidate "pyproject.toml")) {
        return $candidate
    }

    throw "Could not resolve Von repository root from script path: $PSScriptRoot"
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @()
    )

    try {
        $versionText = & $Command @Arguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $versionText) {
            return $null
        }

        $version = [Version]([string]$versionText[0])
        if ($version.Major -eq 3 -and $version.Minor -ge 11) {
            return @{
                Command = $Command
                Arguments = $Arguments
                Version = $version
            }
        }
    }
    catch {
        return $null
    }

    return $null
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[object]

    if ($PythonVersion) {
        [void]$candidates.Add(@{ Command = "py"; Arguments = @("-$PythonVersion") })
    }

    foreach ($minor in @("3.13", "3.12", "3.11")) {
        [void]$candidates.Add(@{ Command = "py"; Arguments = @("-$minor") })
    }

    foreach ($command in @("python", "python3")) {
        [void]$candidates.Add(@{ Command = $command; Arguments = @() })
    }

    foreach ($candidate in $candidates) {
        $result = Test-PythonCandidate -Command $candidate.Command -Arguments $candidate.Arguments
        if ($result) {
            return $result
        }
    }

    throw "Python >=3.11 was not found. Install Python 3.11+ or pass -PythonVersion with an installed py launcher version."
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Step $Description
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

$root = Resolve-RepoRoot
Set-Location -LiteralPath $root

if (-not (Test-Path -LiteralPath "pyproject.toml")) {
    throw "pyproject.toml not found in repo root: $root"
}

Write-Step "Preparing repo root: $root"

$venvDir = Join-Path $root ".venv"
$venvScriptsDir = Join-Path $venvDir "Scripts"
$venvPython = Join-Path $venvScriptsDir "python.exe"
$venvPdm = Join-Path $venvScriptsDir "pdm.exe"
$venvCreated = $false

if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Find-Python
    $displayArgs = if ($python.Arguments.Count -gt 0) { " $($python.Arguments -join ' ')" } else { "" }
    Write-Step "Creating .venv with $($python.Command)$displayArgs (Python $($python.Version))"
    & $python.Command @($python.Arguments) -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw "Failed to create virtual environment at $venvDir"
    }
    $venvCreated = $true
}
else {
    Write-Ok "Using existing .venv"
}

$pipVersion = & $venvPython -m pip --version 2>$null
if ($LASTEXITCODE -ne 0 -or -not $pipVersion) {
    Invoke-Checked "Ensuring pip is available" {
        & $venvPython -m ensurepip --upgrade
    }
}
else {
    Write-Ok "pip is available: $($pipVersion[0])"
}

if ($venvCreated -or $UpgradePip) {
    Invoke-Checked "Upgrading pip" {
        & $venvPython -m pip install --upgrade pip
    }
}
else {
    Write-Ok "Skipping pip upgrade for existing .venv"
}

$pdmInstalled = & $venvPython -m pip show pdm 2>$null
if ($LASTEXITCODE -ne 0 -or -not $pdmInstalled) {
    Invoke-Checked "Installing PDM inside .venv" {
        & $venvPython -m pip install pdm
    }
}
else {
    Write-Ok "PDM is already installed inside .venv"
}

if (-not (Test-Path -LiteralPath $venvPdm)) {
    $venvPdm = $venvPython
    $pdmPrefixArgs = @("-m", "pdm")
}
else {
    $pdmPrefixArgs = @()
}

$resolvedVenvPython = (Resolve-Path -LiteralPath $venvPython).Path.Replace("\", "/")
$pdmPythonPath = Join-Path $root ".pdm-python"
$currentPdmPython = if (Test-Path -LiteralPath $pdmPythonPath) {
    (Get-Content -LiteralPath $pdmPythonPath -Raw).Trim()
}
else {
    ""
}

if ($currentPdmPython -ne $resolvedVenvPython) {
    Write-Step "Recording repo-local PDM interpreter in .pdm-python"
    Set-Content -LiteralPath $pdmPythonPath -NoNewline -Encoding ascii -Value $resolvedVenvPython
}

$env:VIRTUAL_ENV = $venvDir
$env:Path = "$venvScriptsDir;$env:Path"
$env:PDM_CHECK_UPDATE = "false"

function Test-VerifiedImports {
    param(
        [Parameter(Mandatory = $false)]
        [switch]$ThrowOnFailure
    )

    if ($VerifyImports.Count -eq 0) {
        return $true
    }

    $verifyCode = @'
import importlib
import sys

missing = []
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}: {exc}")

if missing:
    print("Missing or broken imports:")
    for item in missing:
        print(f"  {item}")
    raise SystemExit(1)

print("Verified imports: " + ", ".join(sys.argv[1:]))
'@

    $verifyScriptPath = Join-Path ([System.IO.Path]::GetTempPath()) "von_codex_verify_imports_$PID.py"
    try {
        Set-Content -LiteralPath $verifyScriptPath -Encoding utf8 -Value $verifyCode
        Write-Step "Verifying Python imports"
        $verifyOutput = & $venvPython $verifyScriptPath @VerifyImports 2>&1
        $verifyExitCode = $LASTEXITCODE
        foreach ($line in $verifyOutput) {
            Write-Host $line
        }

        if ($verifyExitCode -eq 0) {
            return $true
        }

        if ($ThrowOnFailure) {
            throw "Python import verification failed with exit code $verifyExitCode"
        }
        return $false
    }
    finally {
        Remove-Item -LiteralPath $verifyScriptPath -Force -ErrorAction SilentlyContinue
    }
}

$importsAlreadySatisfied = $false
if (-not $venvCreated -and -not $ForceDependencyInstall -and -not $SkipDependencyInstall) {
    $importsAlreadySatisfied = Test-VerifiedImports
    if ($importsAlreadySatisfied) {
        Write-Ok "Existing .venv satisfies verification imports; skipping PDM dependency installation"
    }
}

$shouldInstallDependencies = -not $SkipDependencyInstall -and ($ForceDependencyInstall -or $venvCreated -or -not $importsAlreadySatisfied)

if ($shouldInstallDependencies) {
    if ($SyncClean) {
        $pdmArgs = @("sync", "--clean")
    }
    else {
        $pdmArgs = @("install")
    }

    if ($VerbosePdm) {
        $pdmArgs += "--verbose"
    }

    Invoke-Checked "Installing Python dependencies with PDM" {
        & $venvPdm @pdmPrefixArgs @pdmArgs
    }
}
elseif ($SkipDependencyInstall) {
    Write-Warn "Skipping PDM dependency installation by request"
}

if ($InstallNode) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "npm was not found on PATH; cannot install JavaScript dependencies."
    }

    if (Test-Path -LiteralPath "package-lock.json") {
        Invoke-Checked "Installing JavaScript dependencies with npm ci" {
            npm ci
        }
    }
    else {
        Invoke-Checked "Installing JavaScript dependencies with npm install" {
            npm install
        }
    }
}

if (-not $importsAlreadySatisfied) {
    Test-VerifiedImports -ThrowOnFailure | Out-Null
}

Write-Ok "Codex automation environment is ready"
