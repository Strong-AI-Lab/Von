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
    Upgrade pip even when reusing an existing .venv. Fresh checkout-local venvs
    upgrade pip before installing PDM; fresh automation fallback venvs skip the
    network upgrade unless this switch is passed.

.PARAMETER SyncClean
    Use 'pdm sync --clean' instead of 'pdm install'. This may remove extra
    packages from the repo-local venv, so it is opt-in.

.PARAMETER InstallNode
    Also install JavaScript dependencies with npm ci/install.

.PARAMETER VerbosePdm
    Pass --verbose to PDM.

.PARAMETER AutomationVenvRoot
    Optional root for fallback automation virtual environments. Used only when
    the checkout-local .venv Python exists but cannot be executed.

.PARAMETER PipNetworkTimeoutSeconds
    Timeout passed to pip for package-index network operations.

.PARAMETER PipNetworkRetries
    Retry count passed to pip for package-index network operations.

.PARAMETER DependencyInstallTimeoutSeconds
    Wall-clock timeout for the PDM dependency installation step.

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
    [string]$AutomationVenvRoot,

    [Parameter(Mandatory = $false)]
    [string[]]$VerifyImports = @("flask", "pymongo", "pytest", "mcp"),

    [Parameter(Mandatory = $false)]
    [ValidateRange(1, 600)]
    [int]$PipNetworkTimeoutSeconds = 45,

    [Parameter(Mandatory = $false)]
    [ValidateRange(0, 10)]
    [int]$PipNetworkRetries = 2,

    [Parameter(Mandatory = $false)]
    [ValidateRange(30, 3600)]
    [int]$DependencyInstallTimeoutSeconds = 240
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

        $versionLine = [string](@($versionText)[0])
        $version = [Version]$versionLine
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

function Invoke-ExternalWithRetry {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @(),

        [Parameter(Mandatory = $true)]
        [string]$Description,

        [Parameter(Mandatory = $false)]
        [int]$Attempts = 3,

        [Parameter(Mandatory = $false)]
        [int]$DelaySeconds = 3
    )

    $lastOutput = @()
    $lastExitCode = $null
    $lastError = $null

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $output = & $FilePath @Arguments 2>&1
            $exitCode = $LASTEXITCODE
            if ($null -eq $exitCode) {
                $exitCode = 0
            }

            if ($exitCode -eq 0) {
                return @{
                    Succeeded = $true
                    Output = @($output)
                    ExitCode = $exitCode
                    Error = $null
                }
            }

            $lastOutput = @($output)
            $lastExitCode = $exitCode
            $lastError = "$Description exited with code $exitCode"
        }
        catch {
            $lastOutput = @()
            $lastExitCode = $null
            $lastError = $_.Exception.Message
        }

        if ($attempt -lt $Attempts) {
            Write-Warn "$Description failed on attempt $attempt/$Attempts; retrying in $DelaySeconds seconds. Last error: $lastError"
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    return @{
        Succeeded = $false
        Output = @($lastOutput)
        ExitCode = $lastExitCode
        Error = $lastError
    }
}

function Test-VenvPythonUsable {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    if (-not (Test-Path -LiteralPath $PythonPath)) {
        return $false
    }

    $probe = Invoke-ExternalWithRetry `
        -FilePath $PythonPath `
        -Arguments @("-c", "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')") `
        -Description "Checking virtualenv Python at $PythonPath"

    if ($probe.Succeeded) {
        return $true
    }

    Write-Warn "Virtualenv Python is not usable at $PythonPath. Last error: $($probe.Error)"
    return $false
}

function Test-WritableDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Directory
    )

    try {
        New-Item -ItemType Directory -Force -Path $Directory | Out-Null
        $probePath = Join-Path $Directory ".von_write_probe_$PID_$([guid]::NewGuid().ToString('N')).tmp"
        Set-Content -LiteralPath $probePath -NoNewline -Encoding ascii -Value "ok"
        Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
        return $true
    }
    catch {
        Write-Warn "Automation venv root is not writable: $Directory. Last error: $($_.Exception.Message)"
        return $false
    }
}

function Resolve-AutomationVenvBase {
    $candidates = New-Object System.Collections.Generic.List[string]

    if ($AutomationVenvRoot) {
        [void]$candidates.Add($AutomationVenvRoot)
    }
    if ($env:VON_CODEX_AUTOMATION_VENV_ROOT) {
        [void]$candidates.Add($env:VON_CODEX_AUTOMATION_VENV_ROOT)
    }
    if ($env:CODEX_HOME) {
        [void]$candidates.Add((Join-Path $env:CODEX_HOME "automations\python-envs"))
    }
    if ($env:USERPROFILE) {
        [void]$candidates.Add((Join-Path $env:USERPROFILE ".codex\automations\python-envs"))
    }
    [void]$candidates.Add((Join-Path ([System.IO.Path]::GetTempPath()) "codex-automation-python-envs"))

    $seen = New-Object System.Collections.Generic.HashSet[string]
    foreach ($candidate in $candidates) {
        if (-not $candidate) {
            continue
        }
        try {
            $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
            $fullPath = [System.IO.Path]::GetFullPath($expanded)
        }
        catch {
            Write-Warn "Skipping invalid automation venv root candidate: $candidate. Last error: $($_.Exception.Message)"
            continue
        }

        if (-not $seen.Add($fullPath.ToLowerInvariant())) {
            continue
        }

        if (Test-WritableDirectory -Directory $fullPath) {
            return $fullPath
        }
    }

    throw "No writable automation virtualenv root was found. Checked explicit root, CODEX_HOME, user .codex, and temp fallback."
}

function Resolve-AutomationVenvDir {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ResolvedRepoRoot
    )

    $base = Resolve-AutomationVenvBase

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($ResolvedRepoRoot.ToLowerInvariant())
        $hashBytes = $sha.ComputeHash($bytes)
    }
    finally {
        $sha.Dispose()
    }
    $shortHash = -join ($hashBytes[0..5] | ForEach-Object { $_.ToString("x2") })
    $repoName = Split-Path -Leaf $ResolvedRepoRoot
    $safeRepoName = ($repoName -replace '[^A-Za-z0-9._-]+', '_').Trim('_')
    if (-not $safeRepoName) {
        $safeRepoName = "repo"
    }

    return (Join-Path $base "$safeRepoName-$shortHash\.venv")
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[object]

    if ($PythonVersion) {
        [void]$candidates.Add(@{ Command = "py"; Arguments = @("-$PythonVersion") })
    }

    foreach ($command in @("python", "python3")) {
        [void]$candidates.Add(@{ Command = $command; Arguments = @() })
    }

    foreach ($minor in @("3.13", "3.12", "3.11")) {
        [void]$candidates.Add(@{ Command = "py"; Arguments = @("-$minor") })
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

function ConvertTo-WindowsProcessArgument {
    param(
        [Parameter(Mandatory = $false)]
        [AllowEmptyString()]
        [string]$Argument
    )

    if ($null -eq $Argument -or $Argument.Length -eq 0) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }

    $result = '"'
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq [char]92) {
            $backslashes += 1
        }
        elseif ($character -eq [char]34) {
            $result += ('\' * (($backslashes * 2) + 1))
            $result += '"'
            $backslashes = 0
        }
        else {
            if ($backslashes -gt 0) {
                $result += ('\' * $backslashes)
                $backslashes = 0
            }
            $result += $character
        }
    }
    if ($backslashes -gt 0) {
        $result += ('\' * ($backslashes * 2))
    }
    $result += '"'
    return $result
}

function Join-WindowsProcessArguments {
    param(
        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @()
    )

    return (@($Arguments) | ForEach-Object { ConvertTo-WindowsProcessArgument -Argument $_ }) -join " "
}

function Invoke-CheckedExternalWithTimeout {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,

        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @(),

        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds
    )

    Write-Step "$Description (timeout ${TimeoutSeconds}s)"

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo.FileName = $FilePath
    $process.StartInfo.Arguments = Join-WindowsProcessArguments -Arguments $Arguments
    $process.StartInfo.WorkingDirectory = (Get-Location).Path
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true

    try {
        if (-not $process.Start()) {
            throw "$Description failed to start"
        }

        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()

        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw "$Description timed out after $TimeoutSeconds seconds"
        }
        $process.WaitForExit()

        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()

        if ($stdout) {
            Write-Host $stdout.TrimEnd()
        }
        if ($stderr) {
            Write-Host $stderr.TrimEnd()
        }

        if ($process.ExitCode -ne 0) {
            throw "$Description failed with exit code $($process.ExitCode)"
        }
    }
    finally {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        $process.Dispose()
    }
}

function Invoke-PipInstall {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,

        [Parameter(Mandatory = $true)]
        [string[]]$Packages,

        [Parameter(Mandatory = $false)]
        [switch]$Upgrade
    )

    $pipArgs = @(
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--timeout",
        [string]$PipNetworkTimeoutSeconds,
        "--retries",
        [string]$PipNetworkRetries
    )
    if ($Upgrade) {
        $pipArgs += "--upgrade"
    }
    $pipArgs += $Packages

    $pipInstallTimeoutSeconds = [Math]::Max(30, (($PipNetworkRetries + 1) * ($PipNetworkTimeoutSeconds + 15)))
    Invoke-CheckedExternalWithTimeout `
        -Description $Description `
        -FilePath $venvPython `
        -Arguments $pipArgs `
        -TimeoutSeconds $pipInstallTimeoutSeconds
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
$venvIsAutomationFallback = $false

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
elseif (-not (Test-VenvPythonUsable -PythonPath $venvPython)) {
    $fallbackVenvDir = Resolve-AutomationVenvDir -ResolvedRepoRoot $root
    Write-Warn "Existing checkout-local .venv Python could not be executed; using automation fallback venv at $fallbackVenvDir"
    $venvDir = $fallbackVenvDir
    $venvScriptsDir = Join-Path $venvDir "Scripts"
    $venvPython = Join-Path $venvScriptsDir "python.exe"
    $venvPdm = Join-Path $venvScriptsDir "pdm.exe"
    $venvIsAutomationFallback = $true

    if (-not (Test-VenvPythonUsable -PythonPath $venvPython)) {
        $python = Find-Python
        $displayArgs = if ($python.Arguments.Count -gt 0) { " $($python.Arguments -join ' ')" } else { "" }
        Write-Step "Creating automation fallback venv with $($python.Command)$displayArgs (Python $($python.Version))"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $venvDir) | Out-Null
        & $python.Command @($python.Arguments) -m venv $venvDir
        if ($LASTEXITCODE -ne 0 -or -not (Test-VenvPythonUsable -PythonPath $venvPython)) {
            throw "Failed to create usable automation fallback virtual environment at $venvDir"
        }
        $venvCreated = $true
    }
    else {
        Write-Ok "Using existing automation fallback venv"
    }
}
else {
    Write-Ok "Using existing .venv"
}

$pipProbe = Invoke-ExternalWithRetry `
    -FilePath $venvPython `
    -Arguments @("-m", "pip", "--version") `
    -Description "Checking pip inside selected virtualenv"
if (-not $pipProbe.Succeeded -or -not $pipProbe.Output) {
    Invoke-Checked "Ensuring pip is available" {
        & $venvPython -m ensurepip --upgrade
    }
}
else {
    $pipVersion = $pipProbe.Output
    Write-Ok "pip is available: $($pipVersion[0])"
}

$shouldUpgradePip = $UpgradePip -or ($venvCreated -and -not $venvIsAutomationFallback)
if ($shouldUpgradePip) {
    Invoke-PipInstall -Description "Upgrading pip" -Packages @("pip") -Upgrade
}
else {
    if ($venvIsAutomationFallback -and $venvCreated) {
        Write-Ok "Skipping pip upgrade for fresh automation fallback venv"
    }
    else {
        Write-Ok "Skipping pip upgrade for existing selected virtualenv"
    }
}

$pdmProbe = Invoke-ExternalWithRetry `
    -FilePath $venvPython `
    -Arguments @("-m", "pip", "show", "pdm") `
    -Description "Checking PDM inside selected virtualenv" `
    -Attempts 1
if (-not $pdmProbe.Succeeded -or -not $pdmProbe.Output) {
    Invoke-PipInstall -Description "Installing PDM inside selected virtualenv" -Packages @("pdm")
}
else {
    Write-Ok "PDM is already installed inside selected virtualenv"
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
if ($venvIsAutomationFallback) {
    $env:VON_CODEX_AUTOMATION_USING_FALLBACK_VENV = "1"
}

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

    Invoke-CheckedExternalWithTimeout `
        -Description "Installing Python dependencies with PDM" `
        -FilePath $venvPdm `
        -Arguments (@($pdmPrefixArgs) + @($pdmArgs)) `
        -TimeoutSeconds $DependencyInstallTimeoutSeconds
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
