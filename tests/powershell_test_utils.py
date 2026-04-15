from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest


POWERSHELL_EXE = shutil.which("powershell") or shutil.which("pwsh")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_powershell_result(
    *,
    repo_root: Path,
    script: str,
    result_variable: str = "result",
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    if not POWERSHELL_EXE:
        pytest.skip("PowerShell is required for run.ps1 launcher-path tests")

    with TemporaryDirectory(prefix="run_ps1_probe_") as temp_dir:
        temp_root = Path(temp_dir)
        result_path = temp_root / "result.json"
        script_path = temp_root / "probe.ps1"
        result_variable_name = result_variable.strip() or "result"
        wrapped_script = f"""
$ErrorActionPreference = 'Stop'
$script:TestResultPath = {ps_quote(str(result_path))}
function global:Write-TestResultJson {{
    param(
        [Parameter(Mandatory = $true)]$Value,
        [int]$Depth = 12
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $json = $Value | ConvertTo-Json -Depth $Depth -Compress
    [System.IO.File]::WriteAllText($script:TestResultPath, $json, $utf8NoBom)
}}
{script}
if (-not (Test-Path -LiteralPath $script:TestResultPath)) {{
    if (Get-Variable -Name {result_variable_name} -ErrorAction SilentlyContinue) {{
        Write-TestResultJson -Value (Get-Variable -Name {result_variable_name}).Value
    }}
    else {{
        throw "PowerShell test script did not populate `${result_variable_name}` or call Write-TestResultJson."
    }}
}}
""".strip()
        script_path.write_text(wrapped_script, encoding="utf-8")

        completed = subprocess.run(
            [
                POWERSHELL_EXE,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )

        if not result_path.exists():
            raise AssertionError(
                "PowerShell test script completed without writing a result file.\n"
                f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
            )

        return json.loads(result_path.read_text(encoding="utf-8")), completed
