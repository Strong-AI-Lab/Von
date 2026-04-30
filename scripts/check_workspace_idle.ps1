$ErrorActionPreference = "Stop"

$ForwardedArgs = @($args)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$PythonScript = Join-Path $ScriptDir "check_workspace_idle.py"
$PythonCandidateFailures = New-Object System.Collections.Generic.List[string]

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

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $launchError = $null
    $exitCode = 0
    try {
        & $PythonCommand @PythonPrefixArgs $PythonScript @ForwardedArgs 1> $stdoutPath 2> $stderrPath
        if ($null -ne $LASTEXITCODE) {
            $exitCode = [int]$LASTEXITCODE
        }
    } catch {
        $launchError = $_.Exception.Message
        $exitCode = 1
    }

    $stdout = ""
    $stderr = ""
    try {
        if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
            $stdout = [System.IO.File]::ReadAllText($stdoutPath)
        }
        if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
            $stderr = [System.IO.File]::ReadAllText($stderrPath)
        }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }

    $trimmedStdout = $stdout.Trim()
    $hasAuthoritativeOutput = $trimmedStdout -eq "YES" -or $trimmedStdout -eq "NO"

    if ($hasAuthoritativeOutput -or $exitCode -eq 0) {
        [Console]::Out.Write($stdout)
        if ($stderr) {
            [Console]::Error.Write($stderr)
        }
        exit $exitCode
    }

    $candidateLabel = @($PythonCommand) + @($PythonPrefixArgs) -join " "
    $detail = $launchError
    if (-not $detail -and $stderr.Trim()) {
        $detail = ($stderr.Trim() -split "\r?\n")[0]
    }
    if (-not $detail -and $stdout.Trim()) {
        $detail = ($stdout.Trim() -split "\r?\n")[0]
    }
    if (-not $detail) {
        $detail = "exited $exitCode without YES/NO output"
    }
    $PythonCandidateFailures.Add("candidate '$candidateLabel' failed: $detail") | Out-Null
    return
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
if ($PythonCandidateFailures.Count -gt 0) {
    [Console]::Error.WriteLine("workspace idle check failed: no Python interpreter could run $PythonScript")
    foreach ($failure in $PythonCandidateFailures) {
        [Console]::Error.WriteLine("workspace idle check failed: $failure")
    }
} else {
    [Console]::Error.WriteLine("workspace idle check failed: no Python interpreter was found")
}
exit 2
