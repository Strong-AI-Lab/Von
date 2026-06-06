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
