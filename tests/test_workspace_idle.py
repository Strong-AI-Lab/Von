from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

import scripts.check_workspace_idle as check_workspace_idle
from src.backend.utilities import workspace_idle
from src.backend.utilities.workspace_idle import (
    ProcessSnapshot,
    RepoActivityAssessment,
    _parse_git_status_porcelain_z,
    _parse_windows_process_csv,
    assess_workspace_idle,
    configured_excluded_pids,
    current_lineage_pids,
    detect_recent_repo_activity,
)
from tests.powershell_test_utils import POWERSHELL_EXE


REPO_ROOT = Path(__file__).resolve().parents[1]


def _workspace(tmp_path: Path) -> str:
    return str(tmp_path / "Von")


def _proc(
    pid: int,
    *,
    name: str = "python.exe",
    cmdline: tuple[str, ...] = (),
    cwd: str | None = None,
) -> ProcessSnapshot:
    return ProcessSnapshot(pid=pid, name=name, cmdline=cmdline, cwd=cwd)


def test_pytest_in_workspace_marks_not_idle(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = assess_workspace_idle(
        [
            _proc(
                100,
                name="pytest.exe",
                cmdline=(
                    f"{workspace}\\.venv\\Scripts\\pytest.EXE",
                    "tests\\backend\\test_example.py",
                    "-q",
                ),
            )
        ],
        workspace_root=workspace,
    )

    assert result.answer == "NO"
    assert len(result.blockers) == 1
    assert result.blockers[0].reason == "active workspace command"


def test_known_von_services_are_ignored_by_default(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = assess_workspace_idle(
        [
            _proc(
                101,
                cmdline=(
                    f"{workspace}\\.venv\\Scripts\\python.exe",
                    "-u",
                    "src/workflows/von/main.py",
                    "--port",
                    "5000",
                ),
                cwd=workspace,
            )
        ],
        workspace_root=workspace,
    )

    assert result.answer == "YES"
    assert result.ignored_services == 1


def test_include_services_treats_von_server_as_blocker(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = assess_workspace_idle(
        [
            _proc(
                102,
                cmdline=(
                    f"{workspace}\\.venv\\Scripts\\python.exe",
                    "-u",
                    "src/workflows/von/main.py",
                    "--port",
                    "5000",
                ),
                cwd=workspace,
            )
        ],
        workspace_root=workspace,
        include_services=True,
    )

    assert result.answer == "NO"
    assert result.blockers[0].reason == "workspace service process"


def test_idle_interactive_shell_in_workspace_is_ignored(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = assess_workspace_idle(
        [_proc(103, name="pwsh.exe", cmdline=("pwsh.exe",), cwd=workspace)],
        workspace_root=workspace,
    )

    assert result.answer == "YES"
    assert result.ignored_interactive_shells == 1


def test_current_checker_lineage_is_excluded(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = assess_workspace_idle(
        [
            _proc(
                104,
                name="pdm.exe",
                cmdline=("pdm.exe", "run", "python", "scripts/check_workspace_idle.py"),
                cwd=workspace,
            )
        ],
        workspace_root=workspace,
        exclude_pids={104},
    )

    assert result.answer == "YES"
    assert not result.blockers


def test_configured_excluded_pids_are_added_to_lineage(monkeypatch) -> None:
    monkeypatch.setenv("VON_WORKSPACE_IDLE_EXCLUDE_PIDS", "17, bad; 23 17")

    assert configured_excluded_pids() == {17, 23}
    assert {17, 23}.issubset(current_lineage_pids())


def test_agent_helpers_are_ignored_by_default(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = assess_workspace_idle(
        [
            _proc(
                105,
                name="node.exe",
                cmdline=("node", "@playwright/mcp", f"--workspace={workspace}"),
            )
        ],
        workspace_root=workspace,
    )

    assert result.answer == "YES"
    assert result.ignored_agent_helpers == 1


def test_von_mcp_stdio_server_is_ignored_by_default(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    result = assess_workspace_idle(
        [
            _proc(
                106,
                name="python.exe",
                cmdline=(
                    f"{workspace}\\.venv\\Scripts\\python.exe",
                    f"{workspace}\\src\\backend\\mcp_server\\mcp_stdio_server.py",
                ),
            )
        ],
        workspace_root=workspace,
    )

    assert result.answer == "YES"
    assert result.ignored_agent_helpers == 1


def test_recent_repo_activity_marks_not_idle(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    activity = RepoActivityAssessment(
        kind="working_tree_path",
        path="scripts/check_workspace_idle.py",
        reason="recent changed or untracked workspace path",
        age_seconds=2.0,
        status="??",
    )

    result = assess_workspace_idle(
        [],
        workspace_root=workspace,
        recent_repo_activity=[activity],
        recent_window_seconds=300,
    )

    assert result.answer == "NO"
    assert result.recent_repo_activity == (activity,)


def test_detect_recent_repo_activity_reports_git_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "Von"
    git_dir = workspace / ".git"
    git_logs = git_dir / "logs"
    git_logs.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "index").write_text("index marker", encoding="utf-8")
    (git_logs / "HEAD").write_text("reflog marker", encoding="utf-8")
    now = time.time()

    result = detect_recent_repo_activity(
        str(workspace),
        now=now,
        recent_seconds=60,
        include_status_paths=False,
    )

    paths = {item.path for item in result}
    assert ".git\\index" in paths or ".git/index" in paths
    assert any(item.kind == "git_metadata" for item in result)


def test_parse_git_status_porcelain_z_handles_rename_extra_path() -> None:
    raw = b" M scripts/check_workspace_idle.py\x00R  new_name.py\x00old_name.py\x00?? tests/new_test.py\x00"

    assert _parse_git_status_porcelain_z(raw) == [
        (" M", "scripts/check_workspace_idle.py"),
        ("R ", "new_name.py"),
        ("??", "tests/new_test.py"),
    ]


def test_parse_windows_process_csv_builds_snapshots() -> None:
    raw = (
        '"ProcessId","Name","CommandLine","CreateTimeUnix"\n'
        '"123","pytest.exe","C:\\Users\\me\\Von\\.venv\\Scripts\\pytest.EXE tests\\x.py","1000"\n'
    )

    rows = _parse_windows_process_csv(raw)

    assert rows == [
        ProcessSnapshot(
            pid=123,
            name="pytest.exe",
            cmdline=("C:\\Users\\me\\Von\\.venv\\Scripts\\pytest.EXE tests\\x.py",),
            create_time=1000.0,
        )
    ]


def test_fast_windows_snapshot_default_timeout_is_generous(monkeypatch) -> None:
    captured: dict[str, float] = {}
    rows = [ProcessSnapshot(pid=123, name="python.exe", cmdline=("python",))]

    monkeypatch.setattr(workspace_idle.os, "name", "nt")

    def fake_cim_processes(*, timeout_seconds: float) -> list[ProcessSnapshot]:
        captured["timeout_seconds"] = timeout_seconds
        return rows

    def fail_full_scan(*, include_cwd: bool = False) -> list[ProcessSnapshot]:
        raise AssertionError("fast Windows process snapshot should not fall back")

    monkeypatch.setattr(workspace_idle, "iter_windows_cim_processes", fake_cim_processes)
    monkeypatch.setattr(workspace_idle, "iter_local_processes", fail_full_scan)

    assert workspace_idle.iter_fast_local_processes() == rows
    assert captured["timeout_seconds"] == 30.0


def test_fast_windows_snapshot_does_not_fall_back_to_unbounded_scan(
    monkeypatch,
) -> None:
    monkeypatch.setattr(workspace_idle.os, "name", "nt")
    monkeypatch.setattr(
        workspace_idle,
        "iter_windows_cim_processes",
        lambda *, timeout_seconds: [],
    )
    monkeypatch.setattr(
        workspace_idle,
        "iter_windows_wmi_processes",
        lambda *, timeout_seconds: [],
    )

    def fail_full_scan(*, include_cwd: bool = False) -> list[ProcessSnapshot]:
        raise AssertionError("default Windows fast snapshot must not run psutil")

    monkeypatch.setattr(workspace_idle, "iter_local_processes", fail_full_scan)

    assert workspace_idle.iter_fast_local_processes() == []


def test_fast_windows_snapshot_uses_bounded_wmi_fallback(monkeypatch) -> None:
    captured: dict[str, float] = {}
    rows = [ProcessSnapshot(pid=456, name="python.exe", cmdline=("python",))]

    monkeypatch.setattr(workspace_idle.os, "name", "nt")
    monkeypatch.setattr(
        workspace_idle,
        "iter_windows_cim_processes",
        lambda *, timeout_seconds: [],
    )

    def fake_wmi_processes(*, timeout_seconds: float) -> list[ProcessSnapshot]:
        captured["timeout_seconds"] = timeout_seconds
        return rows

    def fail_full_scan(*, include_cwd: bool = False) -> list[ProcessSnapshot]:
        raise AssertionError("bounded Windows fallback should not use psutil")

    monkeypatch.setattr(workspace_idle, "iter_windows_wmi_processes", fake_wmi_processes)
    monkeypatch.setattr(workspace_idle, "iter_local_processes", fail_full_scan)

    assert workspace_idle.iter_fast_local_processes() == rows
    assert captured["timeout_seconds"] == 30.0


def test_workspace_idle_main_fails_closed_when_fast_windows_snapshot_is_empty(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(check_workspace_idle.os, "name", "nt")
    monkeypatch.setattr(workspace_idle, "iter_fast_local_processes", lambda: [])
    monkeypatch.setattr(workspace_idle, "current_lineage_pids", lambda: set())

    code = check_workspace_idle.main(
        [
            "--workspace",
            str(tmp_path),
            "--ignore-recent-repo-activity",
            "--no-fail",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == ["NO"]
    assert captured.err == ""


def test_workspace_idle_main_reports_failure_diagnostics_without_no_fail(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(check_workspace_idle.os, "name", "nt")
    monkeypatch.setattr(workspace_idle, "iter_fast_local_processes", lambda: [])
    monkeypatch.setattr(workspace_idle, "current_lineage_pids", lambda: set())

    code = check_workspace_idle.main(
        [
            "--workspace",
            str(tmp_path),
            "--ignore-recent-repo-activity",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out.splitlines() == ["NO"]
    assert "bounded Windows process snapshot returned no rows" in captured.err


def test_workspace_idle_main_records_decision_telemetry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    telemetry_path = tmp_path / "idle" / "telemetry.jsonl"
    monkeypatch.setattr(
        workspace_idle,
        "iter_fast_local_processes",
        lambda: [ProcessSnapshot(pid=700, name="python.exe", cmdline=("python",))],
    )
    monkeypatch.setattr(workspace_idle, "current_lineage_pids", lambda: set())

    code = check_workspace_idle.main(
        [
            "--workspace",
            str(tmp_path),
            "--ignore-recent-repo-activity",
            "--idle-telemetry-path",
            str(telemetry_path),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == ["YES"]
    assert captured.err == ""

    records = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["answer"] == "YES"
    assert record["idle"] is True
    assert record["exit_code"] == 0
    assert record["decision_reason"] == "idle"
    assert record["assessment"]["scanned_processes"] == 1
    assert record["options"]["ignore_recent_repo_activity"] is True


def test_workspace_idle_main_records_recent_activity_telemetry(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    telemetry_path = tmp_path / "idle" / "telemetry.jsonl"
    activity = RepoActivityAssessment(
        kind="git_metadata",
        path=".git\\index",
        reason="recent Git metadata activity",
        age_seconds=12.0,
    )
    monkeypatch.setattr(
        workspace_idle,
        "detect_recent_repo_activity",
        lambda *args, **kwargs: (activity,),
    )
    monkeypatch.setattr(
        workspace_idle,
        "iter_fast_local_processes",
        lambda: pytest.fail("processes should not be scanned after repo activity"),
    )
    monkeypatch.setattr(workspace_idle, "current_lineage_pids", lambda: set())

    code = check_workspace_idle.main(
        [
            "--workspace",
            str(tmp_path),
            "--no-fail",
            "--idle-telemetry-path",
            str(telemetry_path),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == ["NO"]
    assert captured.err == ""

    record = json.loads(telemetry_path.read_text(encoding="utf-8").strip())
    assert record["answer"] == "NO"
    assert record["idle"] is False
    assert record["exit_code"] == 0
    assert record["decision_reason"] == "recent_repo_activity"
    assert record["assessment"]["recent_repo_activity"][0]["path"] == ".git\\index"


def test_workspace_idle_telemetry_write_failure_dumps_to_stderr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    telemetry_directory = tmp_path / "telemetry-directory"
    telemetry_directory.mkdir()
    monkeypatch.setattr(
        workspace_idle,
        "iter_fast_local_processes",
        lambda: [ProcessSnapshot(pid=701, name="python.exe", cmdline=("python",))],
    )
    monkeypatch.setattr(workspace_idle, "current_lineage_pids", lambda: set())

    code = check_workspace_idle.main(
        [
            "--workspace",
            str(tmp_path),
            "--ignore-recent-repo-activity",
            "--idle-telemetry-path",
            str(telemetry_directory),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == ["YES"]
    assert "workspace idle telemetry write failed" in captured.err
    diagnostic = json.loads(captured.err[captured.err.index("{") :])
    assert diagnostic["telemetry"]["answer"] == "YES"
    assert diagnostic["telemetry"]["decision_reason"] == "idle"
    assert diagnostic["telemetry_write_failures"][0]["path"] == str(
        telemetry_directory
    )


def test_workspace_idle_default_telemetry_falls_back_to_temp_when_blocked(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    blocked_codex_home = tmp_path / "blocked-codex-home"
    blocked_codex_home.write_text("not a directory", encoding="utf-8")
    temp_root = tmp_path / "temp"
    monkeypatch.setenv("CODEX_HOME", str(blocked_codex_home))
    monkeypatch.delenv("VON_WORKSPACE_IDLE_TELEMETRY_PATH", raising=False)
    monkeypatch.delenv("VON_WORKSPACE_IDLE_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setattr(check_workspace_idle.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(
        workspace_idle,
        "iter_fast_local_processes",
        lambda: [ProcessSnapshot(pid=702, name="python.exe", cmdline=("python",))],
    )
    monkeypatch.setattr(workspace_idle, "current_lineage_pids", lambda: set())

    code = check_workspace_idle.main(
        [
            "--workspace",
            str(tmp_path),
            "--ignore-recent-repo-activity",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.splitlines() == ["YES"]
    assert captured.err == ""

    fallback_path = temp_root / "von-workspace-idle" / "telemetry.jsonl"
    record = json.loads(fallback_path.read_text(encoding="utf-8").strip())
    assert record["answer"] == "YES"
    assert record["decision_reason"] == "idle"


def test_windows_cim_snapshot_default_timeout_is_generous(monkeypatch) -> None:
    captured: dict[str, float] = {}

    monkeypatch.setattr(
        workspace_idle.shutil,
        "which",
        lambda name: "powershell.exe" if name == "powershell.exe" else None,
    )

    def fake_run(args, **kwargs):
        captured["timeout_seconds"] = kwargs["timeout"]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='"ProcessId","Name","CommandLine","CreateTimeUnix"\n',
            stderr="",
        )

    monkeypatch.setattr(workspace_idle.subprocess, "run", fake_run)

    assert workspace_idle.iter_windows_cim_processes() == []
    assert captured["timeout_seconds"] == 30.0


def test_windows_wmi_snapshot_default_timeout_is_generous(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        workspace_idle.shutil,
        "which",
        lambda name: "powershell.exe" if name == "powershell.exe" else None,
    )

    def fake_run(args, **kwargs):
        captured["timeout_seconds"] = kwargs["timeout"]
        captured["args"] = args
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='"ProcessId","Name","CommandLine","CreateTimeUnix"\n',
            stderr="",
        )

    monkeypatch.setattr(workspace_idle.subprocess, "run", fake_run)

    assert workspace_idle.iter_windows_wmi_processes() == []
    assert captured["timeout_seconds"] == 30.0
    args = captured["args"]
    assert isinstance(args, list)
    assert "Get-WmiObject Win32_Process" in " ".join(args)


@pytest.mark.skipif(not POWERSHELL_EXE, reason="PowerShell is required")
def test_powershell_wrapper_falls_back_after_broken_python_candidate(
    tmp_path: Path,
) -> None:
    assert POWERSHELL_EXE is not None
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copyfile(
        REPO_ROOT / "scripts" / "check_workspace_idle.ps1",
        scripts_dir / "check_workspace_idle.ps1",
    )
    (scripts_dir / "check_workspace_idle.py").write_text(
        "print('YES')\n",
        encoding="utf-8",
    )
    broken_venv_scripts = tmp_path / "broken_venv" / "Scripts"
    broken_venv_scripts.mkdir(parents=True)
    (broken_venv_scripts / "python.exe").write_text(
        "not an executable",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(tmp_path / "broken_venv")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

    completed = subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "check_workspace_idle.ps1"),
            "--no-fail",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["YES"]
    assert completed.stderr == ""


@pytest.mark.skipif(not POWERSHELL_EXE, reason="PowerShell is required")
def test_powershell_wrapper_no_fail_missing_entrypoint_is_exact_no(
    tmp_path: Path,
) -> None:
    assert POWERSHELL_EXE is not None
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copyfile(
        REPO_ROOT / "scripts" / "check_workspace_idle.ps1",
        scripts_dir / "check_workspace_idle.ps1",
    )

    completed = subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "check_workspace_idle.ps1"),
            "--no-fail",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["NO"]
    assert completed.stderr == ""
