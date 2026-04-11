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
. {_ps_quote(str(REPO_ROOT / 'run.ps1'))} help *> $null
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
$ForceBrowser = $true
$NoBrowser = $false
$Port = 5000
$PidFile = {_ps_quote(str(pid_file))}
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
. {_ps_quote(str(REPO_ROOT / 'run.ps1'))} help *> $null
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
$NoBrowser = $false
Invoke-VonHealthyStartFollowUps -TargetPort 5111
$result = [ordered]@{{
    call_order = @($script:CallOrder)
}}
$result | ConvertTo-Json -Depth 6 -Compress
""".strip()

    payload = _run_powershell_json(script)

    assert payload["call_order"] == ["browser:5111", "purity"]
