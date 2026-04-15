from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts import backup_von_db
from tests.powershell_test_utils import ps_quote, run_powershell_result


REPO_ROOT = Path(__file__).resolve().parents[1]


def _receipt_payload(
    *,
    completed_at_utc: str,
    artifact_path: Path,
    tag: str = "auto-daily",
) -> dict[str, Any]:
    return {
        "schema_version": backup_von_db.BACKUP_SUCCESS_RECEIPT_SCHEMA_VERSION,
        "completed_at_utc": completed_at_utc,
        "db_name": "von_db",
        "tag": tag,
        "out_root": str(artifact_path.parent.resolve()),
        "backup_root": str(artifact_path.resolve()),
        "final_artifact_path": str(artifact_path.resolve()),
        "artifact_kind": "zip",
        "compressed": True,
        "encrypted": False,
    }


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _run_daily_backup_probe(tmp_path: Path, setup_script: str) -> dict[str, Any]:
    run_dir = tmp_path / ".run"
    backup_root = tmp_path / "backups"
    receipt_path = run_dir / "last_successful_backup_receipt.json"
    legacy_path = run_dir / "last_backup_utc.txt"

    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help *> $null
$script:RunDir = {ps_quote(str(run_dir))}
$script:BackupRoot = {ps_quote(str(backup_root))}
New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null
New-Item -ItemType Directory -Force -Path $script:BackupRoot | Out-Null
Remove-Item Env:VON_BACKUP_SCHEDULE -ErrorAction SilentlyContinue
Remove-Item Env:VON_BACKUP_INTERVAL_HOURS -ErrorAction SilentlyContinue
Remove-Item Env:VON_DISABLE_DAILY_BACKUP -ErrorAction SilentlyContinue
$script:Logs = New-Object System.Collections.Generic.List[string]
$script:StartJobCalls = 0
$script:LastStartJobArgs = @()
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add($Msg) | Out-Null
}}
function global:Get-Job {{
    param([string]$Name, $ErrorAction)
    @()
}}
function global:Start-Job {{
    param([string]$Name, [scriptblock]$ScriptBlock, [object[]]$ArgumentList)
    $script:StartJobCalls = $script:StartJobCalls + 1
    $script:LastStartJobArgs = @($ArgumentList)
    [pscustomobject]@{{ Name = $Name; State = 'Mocked' }}
}}
$receiptPath = {ps_quote(str(receipt_path))}
$legacyPath = {ps_quote(str(legacy_path))}
{setup_script}
Invoke-DailyBackupIfDue
$result = [ordered]@{{
    start_job_calls = $script:StartJobCalls
    logs = @($script:Logs)
    receipt_json = if (Test-Path -LiteralPath $receiptPath) {{ Get-Content -LiteralPath $receiptPath -Raw }} else {{ $null }}
    legacy = if (Test-Path -LiteralPath $legacyPath) {{ (Get-Content -LiteralPath $legacyPath -Raw).Trim() }} else {{ $null }}
    start_job_args = @($script:LastStartJobArgs | ForEach-Object {{ [string]$_ }})
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)
    payload["receipt"] = (
        json.loads(payload["receipt_json"])
        if payload["receipt_json"] is not None
        else None
    )
    return payload


def test_run_ps1_repairs_launcher_receipt_from_newer_validated_artifact_receipt(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / ".run"
    backup_root = tmp_path / "backups"
    receipt_path = run_dir / "last_successful_backup_receipt.json"

    stale_artifact = backup_root / "von_db_20260404_010203Z_auto-daily.zip"
    newest_artifact = backup_root / "von_db_20260406_010203Z_auto-daily.zip"
    stale_artifact.parent.mkdir(parents=True, exist_ok=True)
    stale_artifact.write_bytes(b"stale")
    newest_artifact.write_bytes(b"newest")

    now = datetime.now(timezone.utc)
    stale_completed = (now - timedelta(hours=36)).isoformat().replace("+00:00", "Z")
    newest_completed = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    _write_receipt(
        receipt_path,
        _receipt_payload(completed_at_utc=stale_completed, artifact_path=stale_artifact),
    )
    _write_receipt(
        backup_von_db._backup_receipt_sidecar_path(newest_artifact),
        _receipt_payload(completed_at_utc=newest_completed, artifact_path=newest_artifact),
    )

    result = _run_daily_backup_probe(
        tmp_path,
        "$env:VON_BACKUP_INTERVAL_HOURS = '24'",
    )

    assert result["start_job_calls"] == 0
    assert result["legacy"] == newest_completed
    assert result["receipt"]["final_artifact_path"] == str(newest_artifact.resolve())
    assert result["receipt"]["completed_at_utc"] == newest_completed
    assert any("repairing from newest validated backup artefact receipt" in line for line in result["logs"])
    assert any("Skip:" in line for line in result["logs"])


def test_run_ps1_interval_due_launches_background_job_with_receipt_paths(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / ".run"
    backup_root = tmp_path / "backups"
    receipt_path = run_dir / "last_successful_backup_receipt.json"
    legacy_path = run_dir / "last_backup_utc.txt"

    artifact = backup_root / "von_db_20260404_010203Z_auto-daily.zip"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"old")
    completed_at = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat().replace(
        "+00:00", "Z"
    )
    _write_receipt(
        receipt_path,
        _receipt_payload(completed_at_utc=completed_at, artifact_path=artifact),
    )

    result = _run_daily_backup_probe(
        tmp_path,
        "$env:VON_BACKUP_INTERVAL_HOURS = '24'",
    )

    assert result["start_job_calls"] == 1
    assert result["start_job_args"][3] == str(receipt_path)
    assert result["start_job_args"][4] == str(legacy_path)
    assert result["start_job_args"][5] == str(backup_root)
    assert any("Launching background backup" in line for line in result["logs"])


def test_run_ps1_cron_skip_uses_launcher_receipt(tmp_path: Path) -> None:
    run_dir = tmp_path / ".run"
    receipt_path = run_dir / "last_successful_backup_receipt.json"
    artifact = (tmp_path / "backups") / "von_db_20260406_090000Z_auto-daily.zip"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"cron")

    now = datetime.now(timezone.utc)
    completed_at = now.isoformat().replace("+00:00", "Z")
    _write_receipt(
        receipt_path,
        _receipt_payload(completed_at_utc=completed_at, artifact_path=artifact),
    )

    schedule = f"{now.minute} {now.hour} * * *"
    result = _run_daily_backup_probe(
        tmp_path,
        f"$env:VON_BACKUP_SCHEDULE = {ps_quote(schedule)}",
    )

    assert result["start_job_calls"] == 0
    assert any("Skip:" in line and "next-due=" in line for line in result["logs"])
    assert result["receipt"]["completed_at_utc"] == completed_at
