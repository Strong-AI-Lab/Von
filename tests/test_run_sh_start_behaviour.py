from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_bash_probe(script: str) -> dict[str, object]:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for run.sh launcher-path tests")

    wrapped = f"""
set -euo pipefail
cd {shlex_quote(str(REPO_ROOT))}
. ./run.sh help -NoBackupMigrate >/dev/null
{script}
""".strip()
    result = subprocess.run(
        [bash, "-c", wrapped],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    return json.loads(result.stdout)


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def test_run_sh_agent_test_health_requires_marker_and_listener_pid() -> None:
    payload = _run_bash_probe(
        r"""
logs=()
log() { logs+=("$*"); }
PORT=5010
AGENT_TEST_INSTANCE=1
python_cmd() { command -v python3; }
get_listening_pid_by_port() { printf '4242'; }

missing_marker='{"agent_test_instance": false, "pid": 4242}'
wrong_pid='{"agent_test_instance": true, "pid": 1111}'
ok='{"agent_test_instance": true, "pid": 4242}'

if agent_test_health_payload_valid "$missing_marker" 1; then missing_result=True; else missing_result=False; fi
if agent_test_health_payload_valid "$wrong_pid" 1; then wrong_pid_result=True; else wrong_pid_result=False; fi
if agent_test_health_payload_valid "$ok" 1; then ok_result=True; else ok_result=False; fi

LOGS="$(printf '%s\n' "${logs[@]}")" python3 - <<PY
import json
import os
print(json.dumps({
    "missing_marker": $missing_result,
    "wrong_pid": $wrong_pid_result,
    "ok": $ok_result,
    "logs": os.environ.get("LOGS", "").splitlines(),
}))
PY
"""
    )

    assert payload["missing_marker"] is False
    assert payload["wrong_pid"] is False
    assert payload["ok"] is True
    assert any(
        "agent_test_instance marker was not true" in line
        for line in payload["logs"]
    )
    assert any("did not match listener PID" in line for line in payload["logs"])


def test_run_sh_confirm_health_stable_rejects_later_probe_failure() -> None:
    payload = _run_bash_probe(
        r"""
calls=0
launcher_health_ready() {
    calls=$((calls + 1))
    [ "$calls" -lt 3 ]
}

if confirm_health_stable; then stable=True; else stable=False; fi

python3 - <<PY
import json
print(json.dumps({"stable": $stable, "calls": $calls}))
PY
"""
    )

    assert payload == {"stable": False, "calls": 3}


def test_run_sh_von_main_process_accepts_relative_script_from_repo_root() -> None:
    payload = _run_bash_probe(
        r"""
python_cmd() { command -v python3; }
get_process_commandline() {
    printf '%s' '/opt/python -u src/workflows/von/main.py --port 5001'
}
get_process_cwd() {
    printf '%s' "$1"
}

repo_cwd="$PWD"
other_cwd="/tmp/not-von"

if is_von_main_process "$repo_cwd"; then repo_result=True; else repo_result=False; fi
if is_von_main_process "$other_cwd"; then other_result=True; else other_result=False; fi

python3 - <<PY
import json
print(json.dumps({
    "repo_cwd": $repo_result,
    "other_cwd": $other_result,
}))
PY
"""
    )

    assert payload == {
        "repo_cwd": True,
        "other_cwd": False,
    }


def test_run_sh_restart_takeover_not_bypassed_by_existing_pid() -> None:
    payload = _run_bash_probe(
        r"""
logs=()
takeovers=()
log() { logs+=("$*"); }
get_pid() { printf '4242'; }
process_exists() { [ "${1:-}" = "4242" ]; }
restart_port_takeover() {
    takeovers+=("$1")
    return 1
}
open_browser_if_needed() {
    logs+=("browser opened")
}

if start_server 1; then status=0; else status=$?; fi

LOGS="$(printf '%s\n' "${logs[@]}")" \
TAKEOVERS="$(printf '%s\n' "${takeovers[@]}")" \
python3 - <<PY
import json
import os
print(json.dumps({
    "status": $status,
    "logs": [line for line in os.environ.get("LOGS", "").splitlines() if line],
    "takeovers": [line for line in os.environ.get("TAKEOVERS", "").splitlines() if line],
}))
PY
"""
    )

    assert payload["status"] == 1
    assert payload["takeovers"] == ["4242"]
    assert not any("Already running" in line for line in payload["logs"])
    assert "browser opened" not in payload["logs"]


def test_run_sh_workflow_purity_check_respects_daily_success_ttl() -> None:
    payload = _run_bash_probe(
        r"""
	tmpdir="$(mktemp -d)"
	fake_python="$tmpdir/fake-python"
	purity_calls="$tmpdir/purity-calls.log"
	cat > "$fake_python" <<SH
#!/usr/bin/env sh
printf 'called %s\n' "\$*" >> "$purity_calls"
exit 0
SH
	chmod +x "$fake_python"

	logs=()
	log() { logs+=("$*"); }
	get_pid() { return 0; }
	get_von_main_pid_by_port() { return 0; }
	get_listening_pid_by_port() { return 0; }
	set_admin_token_env() { :; }
	stop_python_processes_by_script() { :; }
	python_cmd() { printf '%s' "$fake_python"; }
	open_browser_if_needed() { :; }
	launch_detached_process() { printf '4242'; }
	rotate_logs() { :; }
	refine_pid_to_child() { :; }
	sync_pidfile_to_listener() { :; }
	run_daily_backup_if_due() { :; }
	run_test_db_refresh_if_due() { :; }
	start_rag_worker_bg() { :; }
	start_concept_index_worker_bg() { :; }
	log_von_version() { :; }

	PORT=5124
	AGENT_TEST_INSTANCE=0
	SKIP_HEALTH=1
	NO_BROWSER=1
	RUN_DIR="$tmpdir/.run"
	LOGS_DIR="$tmpdir/logs"
	mkdir -p "$RUN_DIR" "$LOGS_DIR"
	PID_FILE="$RUN_DIR/von_${PORT}.pid"
	NEW_LOG="$LOGS_DIR/von_${PORT}.log"
	SERVER_ERR_LOG="$NEW_LOG.err"
	CURRENT_LOG="$LOGS_DIR/von_${PORT}_current.log"
	TOKEN_FILE="$RUN_DIR/admin_token.txt"
	WORKFLOW_PURITY_SUCCESS_FILE="$RUN_DIR/workflow_purity_check_last_success.env"
	WORKFLOW_PURITY_TTL_SECONDS=86400

	: > "$WORKFLOW_PURITY_SUCCESS_FILE"
	start_server
	skip_logs="$(printf '%s\n' "${logs[@]}")"
	skip_call_count=0
	if [ -f "$purity_calls" ]; then
	    skip_call_count="$(wc -l < "$purity_calls" | tr -d ' ')"
	fi

	logs=()
	VON_FORCE_WORKFLOW_PURITY_CHECK=1 start_server
	force_logs="$(printf '%s\n' "${logs[@]}")"
	force_call_count=0
	if [ -f "$purity_calls" ]; then
	    force_call_count="$(wc -l < "$purity_calls" | tr -d ' ')"
	fi

	SKIP_LOGS="$skip_logs" FORCE_LOGS="$force_logs" python3 - <<PY
import json
import os
print(json.dumps({
    "skip_logs": [line for line in os.environ.get("SKIP_LOGS", "").splitlines() if line],
    "force_logs": [line for line in os.environ.get("FORCE_LOGS", "").splitlines() if line],
    "skip_call_count": int("$skip_call_count"),
    "force_call_count": int("$force_call_count"),
}))
PY
	rm -rf "$tmpdir"
	"""
    )

    assert payload["skip_call_count"] == 0
    assert any("Skipping workflow purity check" in line for line in payload["skip_logs"])
    assert not any(
        "Running Workflow Purity Check" in line for line in payload["skip_logs"]
    )
    assert payload["force_call_count"] == 1
    assert any("Workflow purity check forced" in line for line in payload["force_logs"])
    assert any(
        "Running Workflow Purity Check" in line for line in payload["force_logs"]
    )


def test_run_sh_detached_launcher_returns_live_child_pid() -> None:
    payload = _run_bash_probe(
        r"""
tmpdir="$(mktemp -d)"
launcher_py="$(command -v python3)"
pid="$(
    launch_detached_process \
        "$launcher_py" \
        "$tmpdir/out.log" \
        "$tmpdir/err.log" \
        "$PWD" \
        "$launcher_py" \
        -c 'import time; time.sleep(30)'
)"
if printf '%s' "$pid" | grep -qE '^[0-9]+$' && kill -0 "$pid" >/dev/null 2>&1; then alive=True; else alive=False; fi
kill "$pid" >/dev/null 2>&1 || true
rm -rf "$tmpdir"

python3 - <<PY
import json
print(json.dumps({"pid_is_numeric": bool("$pid".isdigit()), "alive": $alive}))
PY
"""
    )

    assert payload == {"pid_is_numeric": True, "alive": True}
