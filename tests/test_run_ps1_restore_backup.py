from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_EXE = shutil.which("powershell") or shutil.which("pwsh")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def test_run_ps1_restore_backup_invokes_restore_script_with_expected_args(
    tmp_path: Path,
) -> None:
    if not POWERSHELL_EXE:
        pytest.skip("PowerShell is required for run.ps1 restore tests")

    backup_root = tmp_path / "backup_root"
    db_dir = backup_root / "von_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "papers.bson").write_bytes(b"paper-data")
    (db_dir / "papers.metadata.json").write_text("{}", encoding="utf-8")

    shim_dir = tmp_path / "shim_bin"
    shim_dir.mkdir(parents=True, exist_ok=True)

    pdm_cmd = shim_dir / "pdm.cmd"
    pdm_shim = shim_dir / "pdm_shim.ps1"

    pdm_cmd.write_text(
        "@echo off\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0pdm_shim.ps1\" %*\n"
        "exit /b %ERRORLEVEL%\n",
        encoding="ascii",
    )
    pdm_shim.write_text(
        "param([Parameter(ValueFromRemainingArguments = $true)][string[]]$RemainingArgs)\n"
        "$forwarded = @()\n"
        "if ($RemainingArgs.Length -gt 2) { $forwarded = $RemainingArgs[2..($RemainingArgs.Length - 1)] }\n"
        "& python @forwarded\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )

    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {_ps_quote(str(REPO_ROOT))}
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add($Msg) | Out-Null
}}
$env:PATH = {_ps_quote(str(shim_dir))} + ';' + $env:PATH
$env:VON_ENABLE_RESTORE_ACTION = '1'
. {_ps_quote(str(REPO_ROOT / 'run.ps1'))} help *> $null
$RestoreBackupPath = {_ps_quote(str(backup_root))}
$RestoreTargetDbName = 'von_db_restore_probe'
$RestoreApply = $false
$RestoreDropTarget = $false
Invoke-RestoreBackupNow
$result = [ordered]@{{
    logs = @($script:Logs)
}}
$result | ConvertTo-Json -Depth 6 -Compress
""".strip()

    completed = subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert any("Starting restore" in line for line in payload["logs"])
    assert any("[restore-backup] OK" in line for line in payload["logs"])
    assert "[restore] DRY-RUN" in completed.stdout
    assert "[restore] Target DB: von_db_restore_probe" in completed.stdout
