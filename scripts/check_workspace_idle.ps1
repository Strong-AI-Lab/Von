$ErrorActionPreference = "Stop"

$ForwardedArgs = @($args)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$PythonScript = Join-Path $ScriptDir "check_workspace_idle.py"

function Add-WorkspaceIdleExcludedPid {
    $pids = @()

    if ($env:VON_WORKSPACE_IDLE_EXCLUDE_PIDS) {
        foreach ($part in ($env:VON_WORKSPACE_IDLE_EXCLUDE_PIDS -split '[,\s;]+')) {
            if ($part -match '^\d+$') {
                $pids += $part
            }
        }
    }

    $currentPid = [System.Diagnostics.Process]::GetCurrentProcess().Id
    $pids += [string]$currentPid

    try {
        $currentProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $currentPid" -ErrorAction SilentlyContinue
        if ($null -ne $currentProcess -and $currentProcess.ParentProcessId) {
            $pids += [string]$currentProcess.ParentProcessId
        }
    } catch {
        # Parent PID exclusion is an optimisation; the Python checker still
        # excludes its own process when this host lookup is unavailable.
    }

    $env:VON_WORKSPACE_IDLE_EXCLUDE_PIDS = ($pids | Select-Object -Unique) -join ","
}

function Invoke-WorkspaceIdlePython {
    param(
        [Parameter(Mandatory = $true)]
        [string] $PythonCommand,
        [string[]] $PythonPrefixArgs = @()
    )

    Add-WorkspaceIdleExcludedPid
    & $PythonCommand @PythonPrefixArgs $PythonScript @ForwardedArgs
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $PythonScript -PathType Leaf)) {
    Write-Output "NO"
    [Console]::Error.WriteLine("workspace idle check failed: Python entry point not found at $PythonScript")
    exit 2
}

$candidatePaths = @()

if ($env:VIRTUAL_ENV) {
    $candidatePaths += (Join-Path $env:VIRTUAL_ENV "Scripts\python.exe")
    $candidatePaths += (Join-Path $env:VIRTUAL_ENV "bin\python")
}

$candidatePaths += (Join-Path $RepoRoot ".venv\Scripts\python.exe")
$candidatePaths += (Join-Path $RepoRoot ".venv\bin\python")

foreach ($candidate in $candidatePaths) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        Invoke-WorkspaceIdlePython -PythonCommand $candidate
    }
}

$pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($pyLauncher) {
    Invoke-WorkspaceIdlePython -PythonCommand $pyLauncher.Source -PythonPrefixArgs @("-3")
}

foreach ($commandName in @("python.exe", "python3.exe", "python", "python3")) {
    $command = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($command) {
        Invoke-WorkspaceIdlePython -PythonCommand $command.Source
    }
}

Write-Output "NO"
[Console]::Error.WriteLine("workspace idle check failed: no Python interpreter was found")
exit 2
