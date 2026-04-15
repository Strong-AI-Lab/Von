from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from scripts.powershell_probe_support import (
    cleanup_stale_powershell_probe_dirs,
    iter_powershell_probe_dirs,
)
from tests.powershell_test_utils import POWERSHELL_EXE, run_powershell_result


def _probe_processes_with_fragment(fragment: str) -> list[int]:
    try:
        import psutil
    except Exception:
        pytest.skip("psutil is required for probe cleanup assertions")

    matches: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any(fragment in str(part) for part in cmdline):
                matches.append(int(proc.info["pid"]))
        except Exception:
            continue
    return matches


@pytest.mark.skipif(not POWERSHELL_EXE, reason="PowerShell is required")
def test_cleanup_stale_powershell_probe_dirs_kills_matching_process_and_removes_dir(
    tmp_path: Path,
) -> None:
    assert POWERSHELL_EXE is not None
    probe_dir = tmp_path / "ps_stage_probe_manual"
    probe_dir.mkdir(parents=True, exist_ok=True)
    script_path = probe_dir / "probe.ps1"
    script_path.write_text("Start-Sleep -Seconds 300\n", encoding="utf-8")

    proc = subprocess.Popen(
        [
            POWERSHELL_EXE,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.0)

    cleaned = cleanup_stale_powershell_probe_dirs(
        temp_root=tmp_path,
        min_age_seconds=0.0,
        prefixes=("ps_stage_probe_",),
        timeout_seconds=0.5,
        remove_dir=True,
    )

    assert len(cleaned) == 1
    assert cleaned[0]["probe_dir"] == str(probe_dir)
    assert not probe_dir.exists()
    proc.wait(timeout=10)
    assert proc.returncode is not None


@pytest.mark.skipif(not POWERSHELL_EXE, reason="PowerShell is required")
def test_run_powershell_result_times_out_and_cleans_probe_artifacts() -> None:
    before_probe_pids = set(_probe_processes_with_fragment("run_ps1_probe_"))
    before_probe_dirs = {
        str(path)
        for path in iter_powershell_probe_dirs(
            temp_root=Path(tempfile.gettempdir()),
            prefixes=("run_ps1_probe_",),
        )
    }

    with pytest.raises(AssertionError, match="PowerShell probe timed out"):
        run_powershell_result(
            repo_root=Path(__file__).resolve().parents[1],
            script="Start-Sleep -Seconds 300",
            timeout_seconds=1.0,
        )

    after_probe_pids = set(_probe_processes_with_fragment("run_ps1_probe_"))
    after_probe_dirs = {
        str(path)
        for path in iter_powershell_probe_dirs(
            temp_root=Path(tempfile.gettempdir()),
            prefixes=("run_ps1_probe_",),
        )
    }
    assert after_probe_pids <= before_probe_pids
    assert after_probe_dirs <= before_probe_dirs


@pytest.mark.skipif(not POWERSHELL_EXE, reason="PowerShell is required")
def test_run_powershell_result_rejects_nested_structured_payloads() -> None:
    before_probe_dirs = {
        str(path)
        for path in iter_powershell_probe_dirs(
            temp_root=Path(tempfile.gettempdir()),
            prefixes=("run_ps1_probe_",),
        )
    }

    with pytest.raises(AssertionError, match="Unsupported test-result field 'receipt'"):
        run_powershell_result(
            repo_root=Path(__file__).resolve().parents[1],
            script="""
$result = [ordered]@{
    receipt = [pscustomobject]@{
        path = 'nested'
    }
}
""".strip(),
        )

    after_probe_dirs = {
        str(path)
        for path in iter_powershell_probe_dirs(
            temp_root=Path(tempfile.gettempdir()),
            prefixes=("run_ps1_probe_",),
        )
    }
    assert after_probe_dirs <= before_probe_dirs
