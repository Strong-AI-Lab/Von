from __future__ import annotations

import json
import platform
import shlex
from pathlib import Path

from tests.powershell_test_utils import ps_quote, run_powershell_result


REPO_ROOT = Path(__file__).resolve().parents[1]


def _host_default_port() -> int:
    return 5001 if platform.system() == "Darwin" else 5000


def test_run_ps1_force_browser_reopens_for_existing_server(tmp_path: Path) -> None:
    pid_file = tmp_path / "von_existing.pid"

    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:Logs = New-Object System.Collections.Generic.List[string]
$script:BrowserTargets = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add([string]$Msg) | Out-Null
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
$script:PidFile = {ps_quote(str(pid_file))}
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
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["browser_targets"] == ["5123"]
    assert any("Already running (PID=4242)." in line for line in payload["logs"])


def test_run_ps1_healthy_start_follow_ups_open_browser_before_purity() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:CallOrder = New-Object System.Collections.Generic.List[string]
function global:Open-VonBrowserIfNeeded {{
    param([int]$TargetPort = $Port)
    $script:CallOrder.Add("browser:$TargetPort") | Out-Null
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
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["call_order"] == ["browser:5111", "purity"]


def test_run_ps1_agent_test_follow_ups_skip_purity() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:CallOrder = New-Object System.Collections.Generic.List[string]
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add([string]$Msg) | Out-Null
}}
function global:Open-VonBrowserIfNeeded {{
    param([int]$TargetPort = $Port)
    $script:CallOrder.Add("browser:$TargetPort") | Out-Null
    $true
}}
function global:Start-WorkflowPurityCheckNonBlocking {{
    $script:CallOrder.Add('purity') | Out-Null
    $true
}}
$script:AgentTest = $true
$script:IsolatedTestInstance = $false
$script:ForceBrowser = $false
$script:NoBrowser = $false
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$true
$script:NoBrowser = $false
Invoke-VonHealthyStartFollowUps -TargetPort 5111
$result = [ordered]@{{
    call_order = @($script:CallOrder)
    logs = @($script:Logs)
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["call_order"] == ["browser:5111"]
    assert any("skipping workflow purity" in line for line in payload["logs"])


def test_run_ps1_agent_test_health_requires_marker_and_listener_pid() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add([string]$Msg) | Out-Null
}}
function global:Get-ListeningProcessByPort {{
    param([int]$Port)
    [pscustomobject]@{{ Id = 4242 }}
}}
$missingMarker = [pscustomobject]@{{
    Healthy = $true
    Payload = [pscustomobject]@{{ agent_test_instance = $false; pid = 4242 }}
    StatusCode = 200
    Error = $null
}}
$wrongPid = [pscustomobject]@{{
    Healthy = $true
    Payload = [pscustomobject]@{{ agent_test_instance = $true; pid = 1111 }}
    StatusCode = 200
    Error = $null
}}
$ok = [pscustomobject]@{{
    Healthy = $true
    Payload = [pscustomobject]@{{ agent_test_instance = $true; pid = 4242 }}
    StatusCode = 200
    Error = $null
}}
$result = [ordered]@{{
    missing_marker = [bool](Test-AgentTestHealthProbe -Probe $missingMarker -Port 5010 -LogFailure)
    wrong_pid = [bool](Test-AgentTestHealthProbe -Probe $wrongPid -Port 5010 -LogFailure)
    ok = [bool](Test-AgentTestHealthProbe -Probe $ok -Port 5010 -LogFailure)
    logs = @($script:Logs)
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["missing_marker"] is False
    assert payload["wrong_pid"] is False
    assert payload["ok"] is True
    assert any("agent_test_instance marker was not true" in line for line in payload["logs"])
    assert any("did not match listener PID" in line for line in payload["logs"])


def test_run_ps1_agent_test_disables_log_ready_shortcut() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:AgentTest = $true
$script:IsolatedTestInstance = $false
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$true
$agentAllowed = [bool](Test-VonLogReadyShortcutAllowed)
$script:AgentTest = $false
$script:IsolatedTestInstance = $false
$script:AgentTestInstance = $false
$normalAllowed = [bool](Test-VonLogReadyShortcutAllowed)
$result = [ordered]@{{
    agent_allowed = $agentAllowed
    normal_allowed = $normalAllowed
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload == {
        "agent_allowed": False,
        "normal_allowed": True,
    }


def test_run_ps1_apply_launcher_switch_compatibility_honours_double_dash_flags() -> (
    None
):
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:ForceBrowser = $false
$script:NoBrowser = $false
$script:ChromeBeta = $false
$script:AgentTest = $false
$script:IsolatedTestInstance = $false
Apply-LauncherSwitchCompatibility -RawInvocationLine './run.ps1 restart --ForceBrowser --ChromeBeta --AgentTest' -RemainingArgs @('--ForceBrowser', '--ChromeBeta', '--AgentTest')
$result = [ordered]@{{
    force_browser = [bool]$script:ForceBrowser
    no_browser = [bool]$script:NoBrowser
    chrome_beta = [bool]$script:ChromeBeta
    agent_test = [bool]$script:AgentTest
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["force_browser"] is True
    assert payload["no_browser"] is False
    assert payload["chrome_beta"] is True
    assert payload["agent_test"] is True


def test_run_ps1_agent_test_defaults_to_isolated_port_and_no_browser() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
[Environment]::SetEnvironmentVariable('VON_AGENT_TEST_INSTANCE', $null, 'Process')
$script:AgentTest = $true
$script:IsolatedTestInstance = $false
$script:ForceBrowser = $false
$script:NoBrowser = $false
$script:Port = 5000
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$false
$result = [ordered]@{{
    port = [int]$script:Port
    no_browser = [bool]$script:NoBrowser
    port_defaulted = [bool]$script:AgentTestPortDefaulted
    env_marker = [Environment]::GetEnvironmentVariable('VON_AGENT_TEST_INSTANCE', 'Process')
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload == {
        "port": 5010,
        "no_browser": True,
        "port_defaulted": True,
        "env_marker": "1",
    }


def test_run_ps1_standard_default_port_is_host_aware() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:AgentTest = $false
$script:IsolatedTestInstance = $false
$script:Port = 4999
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$false
$result = [ordered]@{{
    port = [int]$script:Port
    agent_test = [bool]$script:AgentTestInstance
    env_marker_cleared = -not [Environment]::GetEnvironmentVariable('VON_AGENT_TEST_INSTANCE', 'Process')
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload == {
        "port": _host_default_port(),
        "agent_test": False,
        "env_marker_cleared": True,
    }


def test_run_ps1_standard_default_respects_explicit_port() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:AgentTest = $false
$script:IsolatedTestInstance = $false
$script:Port = 5000
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$true
$result = [ordered]@{{
    port = [int]$script:Port
    agent_test = [bool]$script:AgentTestInstance
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload == {
        "port": 5000,
        "agent_test": False,
    }


def test_run_ps1_agent_test_respects_explicit_port_and_force_browser() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:AgentTest = $true
$script:IsolatedTestInstance = $false
$script:ForceBrowser = $true
$script:NoBrowser = $false
$script:Port = 5012
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$true
$result = [ordered]@{{
    port = [int]$script:Port
    no_browser = [bool]$script:NoBrowser
    port_defaulted = [bool]$script:AgentTestPortDefaulted
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload == {
        "port": 5012,
        "no_browser": False,
        "port_defaulted": False,
    }


def test_run_ps1_agent_test_preserves_global_server_processes() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:CleanupCalls = New-Object System.Collections.Generic.List[string]
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add([string]$Msg) | Out-Null
}}
function global:Stop-PythonProcessesByScript {{
    param([string]$ScriptRelativePath, [string]$Label, [int]$ExcludePid = 0)
    $script:CleanupCalls.Add("$Label|$ScriptRelativePath") | Out-Null
}}
$script:AgentTest = $true
$script:IsolatedTestInstance = $false
$script:ForceBrowser = $false
$script:NoBrowser = $false
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$true
Invoke-VonServerStartProcessCleanup
$agentCalls = @($script:CleanupCalls)
$agentLogs = @($script:Logs)
$script:AgentTest = $false
$script:IsolatedTestInstance = $false
$script:AgentTestInstance = $false
Invoke-VonServerStartProcessCleanup
$result = [ordered]@{{
    agent_calls = @($agentCalls)
    normal_calls = @($script:CleanupCalls)
    agent_logs = @($agentLogs)
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["agent_calls"] == []
    assert payload["normal_calls"] == ["Von Server|src/workflows/von/main.py"]
    assert any("preserving other Von server" in line for line in payload["agent_logs"])


def test_run_ps1_project_python_prefers_verified_pdm_python(
    tmp_path: Path,
) -> None:
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    fake_pdm_python = tmp_path / "fake_pdm_python.cmd"
    fake_pdm_python.write_text(
        "\r\n".join(
            [
                "@echo off",
                "exit /b 0",
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )
    (fake_root / ".pdm-python").write_text(str(fake_pdm_python), encoding="utf-8")

    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:Root = {ps_quote(str(fake_root))}
$resolved = Get-ProjectPythonExecutable
$result = [ordered]@{{
    resolved = [string]$resolved
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["resolved"] == str(fake_pdm_python)


def test_run_ps1_agent_test_skips_shared_startup_background_services() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:Calls = New-Object System.Collections.Generic.List[string]
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add([string]$Msg) | Out-Null
}}
function global:Invoke-DailyBackupIfDue {{ $script:Calls.Add('backup') | Out-Null }}
function global:Invoke-TestDbRefreshIfDue {{ $script:Calls.Add('test-db-refresh') | Out-Null }}
function global:Invoke-AiChatSessionSyncIfDue {{ $script:Calls.Add('ai-chat-sync') | Out-Null }}
function global:Start-RagWorker {{ $script:Calls.Add('rag') | Out-Null }}
function global:Start-ConceptIndexWorker {{ $script:Calls.Add('concept-index') | Out-Null }}
$script:AgentTest = $true
$script:IsolatedTestInstance = $false
Apply-AgentTestLauncherDefaults -PortWasExplicitlyBound:$true
Invoke-VonStartupBackgroundServices
$agentCalls = @($script:Calls)
$agentLogs = @($script:Logs)
$script:AgentTest = $false
$script:IsolatedTestInstance = $false
$script:AgentTestInstance = $false
Invoke-VonStartupBackgroundServices
$result = [ordered]@{{
    agent_calls = @($agentCalls)
    normal_calls = @($script:Calls)
    agent_logs = @($agentLogs)
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload["agent_calls"] == []
    assert payload["normal_calls"] == [
        "backup",
        "test-db-refresh",
        "ai-chat-sync",
        "rag",
        "concept-index",
    ]
    assert any("skipping startup maintenance" in line for line in payload["agent_logs"])


def test_run_ps1_help_documents_agent_test_mode() -> None:
    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
$helpText = (& {{ . {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate }} *>&1) -join "`n"
$result = [ordered]@{{
    has_agent_test = $helpText.Contains('-AgentTest')
    has_default_port = $helpText.Contains('5010')
    has_example = $helpText.Contains('.\\run.ps1 restart -AgentTest -HealthTimeoutSec 180')
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload == {
        "has_agent_test": True,
        "has_default_port": True,
        "has_example": True,
    }


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
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:RunDir = {ps_quote(str(run_dir))}
$script:LogsDir = {ps_quote(str(logs_dir))}
$script:WorkflowPurityPidFile = {ps_quote(str(run_dir / 'workflow_purity_check.pid'))}
$script:WorkflowPurityResultFile = {ps_quote(str(result_path))}
$script:WorkflowPurityReportedFile = {ps_quote(str(run_dir / 'workflow_purity_check_last_reported.txt'))}
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    if ($script:Logs.Count -lt 100) {{
        $script:Logs.Add([string]$Msg) | Out-Null
    }}
}}
Report-WorkflowPurityCheckStatus
Report-WorkflowPurityCheckStatus
$result = [ordered]@{{
    logs = @($script:Logs)
    reported_marker = [string](Get-Content -Raw -Path $script:WorkflowPurityReportedFile)
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)
    logs = payload["logs"]

    assert sum("Previous workflow purity check failed" in line for line in logs) == 1
    assert any("Workflow purity gate failed." in line for line in logs)
    assert "2026-04-11T20:00:03" in payload["reported_marker"]
    assert "|1|failed" in payload["reported_marker"]


def test_run_ps1_file_backed_result_transport_ignores_stdout_json() -> None:
    script = """
$result = [ordered]@{
    mode = 'file-backed'
    count = 1
}
Write-Output '{"misleading":"stdout-json"}'
""".strip()

    payload, completed = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert payload == {"mode": "file-backed", "count": 1}
    assert '{"misleading":"stdout-json"}' in completed.stdout


def test_run_ps1_stale_process_cleanup_uses_file_backed_helper(tmp_path: Path) -> None:
    arg_log = tmp_path / "cleanup_args.txt"
    if platform.system() == "Windows":
        fake_python = tmp_path / "fake_python.cmd"
        fake_python.write_text(
            "\r\n".join(
                [
                    "@echo off",
                    f'> "{arg_log}" echo %*',
                    "echo []",
                ]
            )
            + "\r\n",
            encoding="utf-8",
        )
    else:
        fake_python = tmp_path / "fake_python"
        fake_python.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    f"printf '%s\\n' \"$*\" > {shlex.quote(str(arg_log))}",
                    "printf '[]\\n'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

    script = f"""
$ErrorActionPreference = 'Stop'
Set-Location {ps_quote(str(REPO_ROOT))}
. {ps_quote(str(REPO_ROOT / 'run.ps1'))} help -NoBackupMigrate *> $null
$script:Logs = New-Object System.Collections.Generic.List[string]
function global:Write-LauncherLog {{
    param([string]$Msg)
    $script:Logs.Add([string]$Msg) | Out-Null
}}
function global:Get-ProjectPythonExecutable {{
    {ps_quote(str(fake_python))}
}}
Stop-PythonProcessesByScript -ScriptRelativePath 'src/backend/utilities/rag_indexing_worker.py' -Label 'RAG Worker'
$result = [ordered]@{{
    logs = @($script:Logs)
    arg_log = (Get-Content -Raw -Path {ps_quote(str(arg_log))}).Trim()
}}
""".strip()

    payload, _ = run_powershell_result(repo_root=REPO_ROOT, script=script)

    assert "cleanup_stale_python_processes.py" in payload["arg_log"]
    assert "rag_indexing_worker.py" in payload["arg_log"]
    assert "-c" not in payload["arg_log"]
    assert not any(
        "WARN: Failed stale-process cleanup" in line for line in payload["logs"]
    )
