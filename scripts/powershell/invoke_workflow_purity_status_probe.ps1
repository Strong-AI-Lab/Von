param(
    [string]$RunDir,
    [string]$LogsDir,
    [switch]$SecondPass,
    [int]$MaxLogLines = 100
)

$ErrorActionPreference = 'Stop'
$requestedRunDir = $RunDir
$requestedLogsDir = $LogsDir
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot
. (Join-Path $repoRoot 'run.ps1') help -NoBackupMigrate *> $null

if ($requestedRunDir) {
    $RunDir = $requestedRunDir
    $script:RunDir = $requestedRunDir
}
if ($requestedLogsDir) {
    $LogsDir = $requestedLogsDir
    $script:LogsDir = $requestedLogsDir
}

$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {
    param([string]$Msg)
    if ($script:Logs.Count -lt $MaxLogLines) {
        $script:Logs.Add([string]$Msg) | Out-Null
    }
}

Report-WorkflowPurityCheckStatus
if ($SecondPass) {
    Report-WorkflowPurityCheckStatus
}

$reportedMarker = ''
if (Test-Path -LiteralPath $script:WorkflowPurityReportedFile) {
    $reportedMarker = [string](Get-Content -Raw -Path $script:WorkflowPurityReportedFile)
}

Write-Host '== Workflow Purity Status Probe ==' -ForegroundColor Cyan
Write-Host "Repo root: $repoRoot"
Write-Host "Run dir: $script:RunDir"
Write-Host "Logs dir: $script:LogsDir"
if ($reportedMarker) {
    Write-Host "Reported marker: $reportedMarker"
}
else {
    Write-Host 'Reported marker: <none>'
}

Write-Host ''
Write-Host 'Launcher log lines:' -ForegroundColor Cyan
foreach ($line in $script:Logs) {
    Write-Output $line
}
