from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_EXE = shutil.which("powershell") or shutil.which("pwsh")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_powershell_json(script: str) -> dict[str, Any]:
    if not POWERSHELL_EXE:
        pytest.skip("PowerShell is required for run.ps1 launcher-path tests")

    completed = subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_run_ps1_force_browser_reopens_for_existing_server(tmp_path: Path) -> None:
    pid_file = tmp_path / "von_existing.pid"

    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {_ps_quote(str(REPO_ROOT))}
. {_ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:Logs = New-Object System.Collections.Generic.List[string]
$script:BrowserTargets = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add($Msg) | Out-Null
}}
function global:Get-ExistingProcess {{
    [pscustomobject]@{{ Id = 4242 }}
}}
function global:Open-VonBrowserIfNeeded {{
    param([int]$TargetPort = $Port)
    $script:BrowserTargets.Add([string]$TargetPort) | Out-Null
    $true
}}
$script:ForceBrowser = $true
$script:NoBrowser = $false
$script:Port = 5000
$script:PidFile = {_ps_quote(str(pid_file))}
@(
    'PID=4242',
    'PORT=5123',
    'START=2026-04-12T00:00:00Z'
) | Set-Content -Path $PidFile
Start-VonServer
$result = [ordered]@{{
    logs = @($script:Logs)
    browser_targets = @($script:BrowserTargets)
}}
$result | ConvertTo-Json -Depth 6 -Compress
""".strip()

    payload = _run_powershell_json(script)

    assert payload["browser_targets"] == ["5123"]
    assert any("Already running (PID=4242)." in line for line in payload["logs"])


def test_run_ps1_healthy_start_follow_ups_open_browser_before_purity() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {_ps_quote(str(REPO_ROOT))}
. {_ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:CallOrder = New-Object System.Collections.Generic.List[string]
function global:Open-VonBrowserIfNeeded {{
    param([int]$TargetPort = $Port)
    $script:CallOrder.Add(\"browser:$TargetPort\") | Out-Null
    $true
}}
function global:Start-WorkflowPurityCheckNonBlocking {{
    $script:CallOrder.Add('purity') | Out-Null
    $true
}}
$script:NoBrowser = $false
Invoke-VonHealthyStartFollowUps -TargetPort 5111
$result = [ordered]@{{
    call_order = @($script:CallOrder)
}}
$result | ConvertTo-Json -Depth 6 -Compress
""".strip()

    payload = _run_powershell_json(script)

    assert payload["call_order"] == ["browser:5111", "purity"]


def test_run_ps1_apply_launcher_switch_compatibility_honours_double_dash_flags() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {_ps_quote(str(REPO_ROOT))}
. {_ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:ForceBrowser = $false
$script:NoBrowser = $false
$script:ChromeBeta = $false
Apply-LauncherSwitchCompatibility -RawInvocationLine './run.ps1 restart --ForceBrowser --ChromeBeta' -RemainingArgs @('--ForceBrowser', '--ChromeBeta')
$result = [ordered]@{{
    force_browser = [bool]$script:ForceBrowser
    no_browser = [bool]$script:NoBrowser
    chrome_beta = [bool]$script:ChromeBeta
}}
$result | ConvertTo-Json -Depth 6 -Compress
""".strip()

    payload = _run_powershell_json(script)

    assert payload["force_browser"] is True
    assert payload["no_browser"] is False
    assert payload["chrome_beta"] is True


def test_run_ps1_reports_previous_purity_failure_once(tmp_path: Path) -> None:
    run_dir = tmp_path / ".run"
    logs_dir = tmp_path / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    purity_log = logs_dir / "workflow_purity_prev.log"
    purity_err_log = logs_dir / "workflow_purity_prev.log.err"
    purity_log.write_text(
        "Workflow purity report generated.\nWorkflow purity gate failed.\n",
        encoding="utf-8",
    )
    purity_err_log.write_text("", encoding="utf-8")

    result_path = run_dir / "workflow_purity_check_last_result.json"
    result_path.write_text(
        json.dumps(
            {
                "started_at_utc": "2026-04-11T20:00:00Z",
                "completed_at_utc": "2026-04-11T20:00:03Z",
                "exit_code": 1,
                "status": "failed",
                "log_path": str(purity_log),
                "err_log_path": str(purity_err_log),
                "message": None,
            }
        ),
        encoding="utf-8",
    )

    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {_ps_quote(str(REPO_ROOT))}
. {_ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:RunDir = {_ps_quote(str(run_dir))}
$script:LogsDir = {_ps_quote(str(logs_dir))}
$script:WorkflowPurityPidFile = {_ps_quote(str(run_dir / 'workflow_purity_check.pid'))}
$script:WorkflowPurityResultFile = {_ps_quote(str(result_path))}
$script:WorkflowPurityReportedFile = {_ps_quote(str(run_dir / 'workflow_purity_check_last_reported.txt'))}
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add($Msg) | Out-Null
}}
Report-WorkflowPurityCheckStatus
Report-WorkflowPurityCheckStatus
$result = [ordered]@{{
    logs = @($script:Logs)
    reported_marker = Get-Content -Raw -Path $script:WorkflowPurityReportedFile
}}
$result | ConvertTo-Json -Depth 8 -Compress
""".strip()

    payload = _run_powershell_json(script)
    logs = payload["logs"]

    assert sum("Previous workflow purity check failed" in line for line in logs) == 1
    assert any("Workflow purity gate failed." in line for line in logs)
    assert "2026-04-11T20:00:03" in payload["reported_marker"]
    assert "|1|failed" in payload["reported_marker"]
