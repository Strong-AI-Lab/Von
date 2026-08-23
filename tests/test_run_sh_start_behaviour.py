from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
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


def _run_bash_text_probe(script: str) -> str:
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
    return result.stdout


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def test_run_sh_default_health_timeout_covers_slow_startup() -> None:
    payload = _run_bash_probe(
        r"""
python3 - <<PY
import json
print(json.dumps({"health_timeout_seconds": int("$HEALTH_TIMEOUT_SEC")}))
PY
"""
    )

    assert payload == {"health_timeout_seconds": 180}


def test_run_sh_accepts_an_explicit_existing_launcher_root(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for run.sh launcher-path tests")
    env = os.environ.copy()
    env["VON_LAUNCHER_ROOT"] = str(tmp_path)
    result = subprocess.run(
        [bash, "-c", ". ./run.sh help -NoBackupMigrate >/dev/null; printf '%s' \"$ROOT\""],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == str(tmp_path.resolve())


def test_run_sh_rejects_a_missing_launcher_root(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for run.sh launcher-path tests")
    missing = tmp_path / "missing"
    env = os.environ.copy()
    env["VON_LAUNCHER_ROOT"] = str(missing)
    result = subprocess.run(
        [bash, "./run.sh", "help", "-NoBackupMigrate"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "VON_LAUNCHER_ROOT is not a directory" in result.stderr


def test_run_sh_labels_ssh_tunnel_fallback_as_remote_atlas() -> None:
    output = _run_bash_text_probe(r"""
curl() {
    printf '%s' '{"using_fallback":true,"fallback_kind":"ssh_tunnel","atlas_detected":true,"effective_host":"127.0.0.1:27019"}'
}
log_mongo_status
""")

    assert "Mongo: Atlas via SSH tunnel (127.0.0.1:27019)" in output
    assert "Mongo: local fallback" not in output


def test_run_sh_reports_listener_presence_without_claiming_mongo_readiness() -> None:
    output = _run_bash_text_probe(r"""
MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS="127.0.0.1:27018,127.0.0.1:27019"
lsof() {
    return 0
}
log_mongo_ssh_tunnel_status
""")

    assert "listeners present (2/2); Mongo route not probed" in output
    assert "ready" not in output


def test_run_sh_does_not_claim_direct_atlas_for_incomplete_listeners() -> None:
    output = _run_bash_text_probe(r"""
MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS="127.0.0.1:27018,127.0.0.1:27019"
lsof() {
    case "$*" in
        *27018*) return 0 ;;
        *) return 1 ;;
    esac
}
log_mongo_ssh_tunnel_status
""")

    assert "listener coverage incomplete (1/2); inspect live Mongo route" in output
    assert "direct Atlas remains primary" not in output


def _prepare_backup_launcher(
    tmp_path: Path,
    *,
    enable_daily_backup: bool,
    backup_exit: int = 0,
    backup_delay: float = 0.15,
    artifact_size: int = 16,
) -> dict[str, Path]:
    launcher_root = tmp_path / "launcher"
    launcher_root.mkdir()
    run_sh = launcher_root / "run.sh"
    shutil.copy2(REPO_ROOT / "run.sh", run_sh)

    backup_script = launcher_root / "scripts" / "backup_von_db.py"
    backup_script.parent.mkdir()
    backup_script.write_text("# fake backup entrypoint\n", encoding="utf-8")

    args_path = tmp_path / "backup_args.txt"
    calls_path = tmp_path / "backup_calls.txt"
    created_path = tmp_path / "created_by_backup.txt"
    fake_pdm = launcher_root / ".venv" / "bin" / "pdm"
    fake_pdm.parent.mkdir(parents=True)
    fake_pdm.write_text(
        f"""#!/usr/bin/env python3
import datetime
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
args_path = Path({str(args_path)!r})
calls_path = Path({str(calls_path)!r})
created_path = Path({str(created_path)!r})
args_path.write_text("\\n".join(args) + "\\n", encoding="utf-8")
with calls_path.open("a", encoding="utf-8") as handle:
    handle.write("call\\n")
created_path.write_text("created\\n", encoding="utf-8")
time.sleep({backup_delay!r})

if {backup_exit} == 0 and "--launcher-receipt-path" in args:
    def option(name):
        return args[args.index(name) + 1]

    out_dir = Path(option("--out-dir"))
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = out_dir / "fake_auto_daily.zip"
    artifact.write_bytes(b"x" * {artifact_size})
    completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    payload = {{
        "schema_version": "backup_success_receipt.v1",
        "completed_at_utc": completed_at,
        "db_name": "von_db",
        "tag": "auto-daily",
        "out_root": str(out_dir),
        "backup_root": str(artifact),
        "final_artifact_path": str(artifact),
        "artifact_kind": "zip",
        "compressed": True,
        "encrypted": False,
        "artifact_size_bytes": artifact.stat().st_size,
        "collection_count": 1,
    }}
    receipt_text = json.dumps(payload, indent=2) + "\\n"
    Path(option("--launcher-receipt-path")).write_text(
        receipt_text, encoding="utf-8"
    )
    Path(option("--legacy-sentinel-path")).write_text(
        completed_at + "\\n", encoding="utf-8"
    )
    artifact.with_name(artifact.name + ".backup_receipt.json").write_text(
        receipt_text, encoding="utf-8"
    )

raise SystemExit({backup_exit})
""",
        encoding="utf-8",
    )
    fake_pdm.chmod(0o755)

    backup_root = tmp_path / "external-backups"
    (launcher_root / ".env").write_text(
        "\n".join(
            [
                f"VON_ENABLE_DAILY_BACKUP={'1' if enable_daily_backup else '0'}",
                "VON_ENABLE_BACKUP_ACTION=1",
                "VON_DB_NAME=von_db",
                f'VON_BACKUP_ROOT="{backup_root}"',
                "VON_BACKUP_INTERVAL_HOURS=24",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "root": launcher_root,
        "run_sh": run_sh,
        "backup_script": backup_script,
        "backup_root": backup_root,
        "args": args_path,
        "calls": calls_path,
        "created": created_path,
    }


def _run_prepared_launcher(
    prepared: dict[str, Path], action: str
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for run.sh launcher-path tests")
    return subprocess.run(
        [bash, str(prepared["run_sh"]), action, "-NoBackupMigrate"],
        cwd=prepared["root"],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _write_test_backup_receipt(
    receipt_path: Path,
    *,
    artifact_path: Path,
    completed_at: datetime,
    tag: str = "auto-daily",
    schema_version: str = "backup_success_receipt.v1",
    db_name: str = "von_db",
    out_root: Path | None = None,
) -> None:
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": schema_version,
        "completed_at_utc": completed_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "db_name": db_name,
        "tag": tag,
        "out_root": str(out_root or artifact_path.parent),
        "backup_root": str(artifact_path),
        "final_artifact_path": str(artifact_path),
        "artifact_kind": "zip",
        "compressed": True,
        "encrypted": False,
        "artifact_size_bytes": artifact_path.stat().st_size,
        "collection_count": 1,
    }
    receipt_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _process_start_marker(pid: int) -> str:
    return subprocess.check_output(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        text=True,
    ).strip()


def _write_test_backup_lock(
    prepared: dict[str, Path],
    *,
    owner_pid: int,
    owner_start: str,
    include_owner_metadata: bool = True,
) -> Path:
    lock_dir = prepared["root"] / ".run" / "daily_backup.lock"
    lock_dir.mkdir(parents=True)
    if include_owner_metadata:
        (lock_dir / "owner").write_text("test-owner\n", encoding="utf-8")
        (lock_dir / "launcher.pid").write_text(
            f"{owner_pid}\n",
            encoding="utf-8",
        )
        (lock_dir / "launcher.start").write_text(
            owner_start + "\n",
            encoding="utf-8",
        )
        (lock_dir / "baseline_success").write_text("\n", encoding="utf-8")
    (lock_dir / "ready").write_text("", encoding="utf-8")
    return lock_dir


def _run_concurrent_scheduled_backups(
    prepared: dict[str, Path],
) -> tuple[tuple[int, str, str], tuple[int, str, str]]:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for run.sh launcher-path tests")
    command = [
        bash,
        str(prepared["run_sh"]),
        "scheduled-backup",
        "-NoBackupMigrate",
    ]
    first = subprocess.Popen(
        command,
        cwd=prepared["root"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ready_path = prepared["root"] / ".run" / "daily_backup.lock" / "ready"
    deadline = time.monotonic() + 5
    while (
        time.monotonic() < deadline
        and not ready_path.exists()
        and first.poll() is None
    ):
        time.sleep(0.01)
    assert ready_path.exists(), "first scheduled backup did not acquire its owner lock"
    lock_dir = ready_path.parent
    assert lock_dir.stat().st_mode & 0o777 == 0o700
    for private_name in (
        "owner",
        "launcher.pid",
        "launcher.start",
        "baseline_success",
        "ready",
    ):
        assert (lock_dir / private_name).stat().st_mode & 0o777 == 0o600

    second = subprocess.Popen(
        command,
        cwd=prepared["root"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_out, first_err = first.communicate(timeout=20)
    second_out, second_err = second.communicate(timeout=20)
    return (
        (first.returncode, first_out, first_err),
        (second.returncode, second_out, second_err),
    )


def test_run_sh_loads_dotenv_before_backup_path_globals(tmp_path: Path) -> None:
    launcher_root = tmp_path / "launcher"
    launcher_root.mkdir()
    run_sh = launcher_root / "run.sh"
    shutil.copy2(REPO_ROOT / "run.sh", run_sh)
    backup_root = tmp_path / "external-backups"
    (launcher_root / ".env").write_text(
        "\n".join(
            [
                f'VON_BACKUP_ROOT="{backup_root}"',
                "VON_ALLOW_BACKUP_IN_REPO=1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for run.sh launcher-path tests")
    result = subprocess.run(
        [
            bash,
            "-c",
            (
                f". {shlex_quote(str(run_sh))} help -NoBackupMigrate >/dev/null\n"
                "printf '%s\\n%s\\n' \"$BACKUP_ROOT\" \"$ALLOW_REPO_BACKUP_OUTPUT\""
            ),
        ],
        cwd=launcher_root,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )

    assert result.stdout.splitlines() == [str(backup_root), "1"]


def test_run_sh_scheduled_backup_requires_explicit_host_opt_in(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=False)

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0
    assert not prepared["args"].exists()
    assert "automatic backups disabled" in result.stdout


def test_run_sh_scheduled_backup_waits_and_passes_receipt_paths(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared["created"].exists()
    assert prepared["created"].stat().st_mode & 0o777 == 0o600
    launcher_receipt = (
        prepared["root"] / ".run" / "last_successful_backup_receipt.json"
    )
    artifact = prepared["backup_root"] / "fake_auto_daily.zip"
    sidecar = artifact.with_name(artifact.name + ".backup_receipt.json")
    assert launcher_receipt.stat().st_mode & 0o777 == 0o600
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert prepared["args"].read_text(encoding="utf-8").splitlines() == [
        "run",
        "python",
        str(prepared["backup_script"]),
        "--apply",
        "--out-dir",
        str(prepared["backup_root"]),
        "--tag",
        "auto-daily",
        "--launcher-receipt-path",
        str(prepared["root"] / ".run" / "last_successful_backup_receipt.json"),
        "--legacy-sentinel-path",
        str(prepared["root"] / ".run" / "last_backup_utc.txt"),
    ]


def test_run_sh_scheduled_backup_returns_backup_failure(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(
        tmp_path,
        enable_daily_backup=True,
        backup_exit=23,
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 23
    assert prepared["created"].exists()
    assert "[daily-backup] ERROR exit=23" in result.stdout


def test_run_sh_concurrent_scheduled_backups_launch_one_dump(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(
        tmp_path,
        enable_daily_backup=True,
        backup_delay=0.4,
    )

    first, second = _run_concurrent_scheduled_backups(prepared)

    assert first[0] == 0, first[1] + first[2]
    assert second[0] == 0, second[1] + second[2]
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]
    assert "Waiting for existing scheduled backup" in second[1]
    assert "validated success evidence" in second[1]


def test_run_sh_concurrent_waiter_fails_without_new_success_evidence(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(
        tmp_path,
        enable_daily_backup=True,
        backup_exit=23,
        backup_delay=0.4,
    )

    first, second = _run_concurrent_scheduled_backups(prepared)

    assert first[0] == 23, first[1] + first[2]
    assert second[0] == 1, second[1] + second[2]
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]
    assert "without newly validated success evidence" in second[1]


def test_run_sh_rejects_advanced_receipt_with_empty_artifact(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(
        tmp_path,
        enable_daily_backup=True,
        artifact_size=0,
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 1
    assert "without newly validated success evidence" in result.stdout
    assert "backup artefact is missing or empty" in result.stderr


def test_run_sh_repairs_launcher_receipt_from_valid_auto_daily_sidecar(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    artifact = prepared["backup_root"] / "existing_auto_daily.zip"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"existing validated backup")
    sidecar = artifact.with_name(artifact.name + ".backup_receipt.json")
    completed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    _write_test_backup_receipt(
        sidecar,
        artifact_path=artifact,
        completed_at=completed_at,
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    launcher_receipt = (
        prepared["root"] / ".run" / "last_successful_backup_receipt.json"
    )
    legacy_sentinel = prepared["root"] / ".run" / "last_backup_utc.txt"
    assert result.returncode == 0, result.stdout + result.stderr
    assert not prepared["calls"].exists()
    assert json.loads(launcher_receipt.read_text(encoding="utf-8"))[
        "final_artifact_path"
    ] == str(artifact)
    assert launcher_receipt.stat().st_mode & 0o777 == 0o600
    assert legacy_sentinel.stat().st_mode & 0o777 == 0o600
    assert "repairing from newest validated" in result.stderr


@pytest.mark.parametrize(
    ("receipt_overrides", "warning_fragment"),
    [
        ({"schema_version": "unknown.v1"}, "schema_version"),
        ({"tag": "manual"}, "tag must be"),
        ({"db_name": "other_db"}, "db_name must be"),
    ],
)
def test_run_sh_ignores_untrusted_launcher_receipt_identity(
    tmp_path: Path,
    receipt_overrides: dict[str, str],
    warning_fragment: str,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    artifact = prepared["backup_root"] / "untrusted.zip"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"not authoritative")
    launcher_receipt = (
        prepared["root"] / ".run" / "last_successful_backup_receipt.json"
    )
    _write_test_backup_receipt(
        launcher_receipt,
        artifact_path=artifact,
        completed_at=datetime.now(timezone.utc),
        **receipt_overrides,
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]
    assert warning_fragment in result.stderr


def test_run_sh_ignores_receipt_for_artifact_outside_backup_root(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    prepared["backup_root"].mkdir(parents=True)
    outside_artifact = tmp_path / "outside.zip"
    outside_artifact.write_bytes(b"outside configured root")
    launcher_receipt = (
        prepared["root"] / ".run" / "last_successful_backup_receipt.json"
    )
    _write_test_backup_receipt(
        launcher_receipt,
        artifact_path=outside_artifact,
        out_root=prepared["backup_root"],
        completed_at=datetime.now(timezone.utc),
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]
    assert "direct child of the configured backup root" in result.stderr


def test_run_sh_ignores_receipt_that_names_backup_root_as_artifact(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    prepared["backup_root"].mkdir(parents=True)
    (prepared["backup_root"] / "not-an-artifact.txt").write_text(
        "not a backup artefact",
        encoding="utf-8",
    )
    launcher_receipt = (
        prepared["root"] / ".run" / "last_successful_backup_receipt.json"
    )
    _write_test_backup_receipt(
        launcher_receipt,
        artifact_path=prepared["backup_root"],
        out_root=prepared["backup_root"],
        completed_at=datetime.now(timezone.utc),
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]
    assert "direct child of the configured backup root" in result.stderr


def test_run_sh_ignores_misnamed_artifact_sidecar(tmp_path: Path) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    artifact = prepared["backup_root"] / "existing.zip"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"existing backup")
    misnamed_sidecar = prepared["backup_root"] / "different.backup_receipt.json"
    _write_test_backup_receipt(
        misnamed_sidecar,
        artifact_path=artifact,
        completed_at=datetime.now(timezone.utc),
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]
    assert "sidecar name does not match" in result.stderr


def test_run_sh_future_receipt_cannot_suppress_backup(tmp_path: Path) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    artifact = prepared["backup_root"] / "future.zip"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"future dated backup")
    launcher_receipt = (
        prepared["root"] / ".run" / "last_successful_backup_receipt.json"
    )
    _write_test_backup_receipt(
        launcher_receipt,
        artifact_path=artifact,
        completed_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]
    assert "implausibly far in the future" in result.stderr


def test_run_sh_uses_legacy_sentinel_only_without_valid_receipt(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    sentinel = prepared["root"] / ".run" / "last_backup_utc.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text(
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") + "\n",
        encoding="utf-8",
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not prepared["calls"].exists()
    assert "falling back to legacy" in result.stderr


def test_run_sh_recovers_lock_missing_owner_metadata(tmp_path: Path) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    lock_dir = _write_test_backup_lock(
        prepared,
        owner_pid=os.getpid(),
        owner_start="",
        include_owner_metadata=False,
    )

    stale_result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert stale_result.returncode == 1
    assert "no valid owner metadata" in stale_result.stdout
    assert not lock_dir.exists()

    retry_result = _run_prepared_launcher(prepared, "scheduled-backup")
    assert retry_result.returncode == 0, retry_result.stdout + retry_result.stderr
    assert prepared["calls"].read_text(encoding="utf-8").splitlines() == ["call"]


def test_run_sh_rejects_reused_pid_with_different_process_start(
    tmp_path: Path,
) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    lock_dir = _write_test_backup_lock(
        prepared,
        owner_pid=os.getpid(),
        owner_start="not-the-current-process-start",
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 1
    assert "without newly validated success evidence" in result.stdout
    assert not lock_dir.exists()
    assert not prepared["calls"].exists()


def test_run_sh_bounds_wait_for_live_backup_owner(tmp_path: Path) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=True)
    env_path = prepared["root"] / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8")
        + "VON_BACKUP_WAIT_TIMEOUT_SECONDS=1\n",
        encoding="utf-8",
    )
    lock_dir = _write_test_backup_lock(
        prepared,
        owner_pid=os.getpid(),
        owner_start=_process_start_marker(os.getpid()),
    )

    result = _run_prepared_launcher(prepared, "scheduled-backup")

    assert result.returncode == 1
    assert "timed out after 1s" in result.stdout
    assert lock_dir.exists()
    assert not prepared["calls"].exists()
    shutil.rmtree(lock_dir)


def test_run_sh_manual_backup_uses_owner_only_umask(tmp_path: Path) -> None:
    prepared = _prepare_backup_launcher(tmp_path, enable_daily_backup=False)

    result = _run_prepared_launcher(prepared, "backup")

    assert result.returncode == 0, result.stdout + result.stderr
    assert prepared["created"].exists()
    assert prepared["created"].stat().st_mode & 0o777 == 0o600


def test_run_sh_background_backup_delegates_to_detached_public_action() -> None:
    payload = _run_bash_probe(
        r"""
tmpdir="$(mktemp -d)"
detach_args="$tmpdir/detach-args.txt"
logs=()
log() { logs+=("$*"); }
python_cmd() { command -v python3; }
resolve_daily_backup_success() { return 0; }
launch_detached_process() {
    printf '%s\n' "$*" > "$detach_args"
    printf '4242'
}

VON_ENABLE_DAILY_BACKUP=1
unset VON_DISABLE_DAILY_BACKUP
RUN_DIR="$tmpdir/.run"
BACKUP_ROOT="$tmpdir/backups"
LOCAL_BACKUPS="$tmpdir/local"
ALLOW_REPO_BACKUP_OUTPUT=0
mkdir -p "$RUN_DIR"

if run_daily_backup_if_due; then status=0; else status=$?; fi
args="$(cat "$detach_args")"
LOGS="$(printf '%s\n' "${logs[@]}")" ARGS="$args" python3 - <<PY
import json
import os
print(json.dumps({
    "status": $status,
    "args": os.environ["ARGS"],
    "logs": os.environ.get("LOGS", "").splitlines(),
}))
PY
rm -rf "$tmpdir"
"""
    )

    assert payload["status"] == 0
    assert "run.sh scheduled-backup -NoBackupMigrate" in payload["args"]
    assert any(
        "Launched detached scheduled-backup PID=4242" in line
        for line in payload["logs"]
    )


def test_run_sh_backup_actions_never_call_semantic_sync_helpers() -> None:
    payload = _run_bash_probe(
        r"""
tmpdir="$(mktemp -d)"
fake_pdm="$tmpdir/fake-pdm"
cat > "$fake_pdm" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$fake_pdm"

semantic_calls=()
log() { :; }
pdm_cmd() { printf '%s' "$fake_pdm"; }
run_code_mention_scan() { semantic_calls+=("mention:$1"); }
run_code_predicate_sync() { semantic_calls+=("predicate:$1"); }

VON_ENABLE_BACKUP_ACTION=1
BACKUP_OUT_DIR="$tmpdir/backups"
BACKUP_ROOT="$BACKUP_OUT_DIR"
LOCAL_BACKUPS="$tmpdir/local"
BACKUP_TAG="test"
ALLOW_REPO_BACKUP_OUTPUT=0
AGENT_TEST_INSTANCE=1
NO_BACKUP_MIGRATE=1

BACKUP_DRY_RUN=1
if run_backup; then dry_status=0; else dry_status=$?; fi
BACKUP_DRY_RUN=0
if run_backup; then apply_status=0; else apply_status=$?; fi

python3 - <<PY
import json
print(json.dumps({
    "dry_status": $dry_status,
    "apply_status": $apply_status,
    "semantic_calls": "$(printf '%s,' "${semantic_calls[@]}")",
}))
PY
rm -rf "$tmpdir"
"""
    )

    assert payload == {
        "dry_status": 0,
        "apply_status": 0,
        "semantic_calls": "",
    }


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


def test_run_sh_readiness_observation_is_pending_while_process_lives() -> None:
    payload = _run_bash_probe(
        r"""
logs=()
tail_calls=0
removed_pidfile=0
log() { logs+=("$*"); }
process_exists() { [ "$1" = "4242" ]; }
remove_pidfile() { removed_pidfile=$((removed_pidfile + 1)); }
show_server_start_failure_tail() { tail_calls=$((tail_calls + 1)); }

if report_server_readiness_observation 4242 "Health remains pending."; then alive_status=0; else alive_status=$?; fi
if report_server_readiness_observation 5252 "Health remains pending."; then dead_status=0; else dead_status=$?; fi

LOGS="$(printf '%s\n' "${logs[@]}")" python3 - <<PY
import json
import os
print(json.dumps({
    "alive_status": $alive_status,
    "dead_status": $dead_status,
    "tail_calls": $tail_calls,
    "removed_pidfile": $removed_pidfile,
    "logs": os.environ.get("LOGS", "").splitlines(),
}))
PY
"""
    )

    assert payload["alive_status"] == 0
    assert payload["dead_status"] == 1
    assert payload["tail_calls"] == 1
    assert payload["removed_pidfile"] == 1
    assert any("Returning with readiness pending" in line for line in payload["logs"])
    assert any("server process PID=5252 has exited" in line for line in payload["logs"])


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


def test_run_sh_exact_pid_stop_preserves_other_von_servers() -> None:
    payload = _run_bash_probe(
        r"""
logs=()
stopped_workers=()
process_exists_calls=0
log() { logs+=("$*"); }
process_exists() {
    process_exists_calls=$((process_exists_calls + 1))
    [ "$process_exists_calls" -eq 1 ]
}
is_von_main_process() { return 0; }
remove_pidfile() { :; }
stop_python_processes_by_script() { logs+=("broad sweep:$*"); }
stop_rag_worker() { stopped_workers+=("rag"); }
stop_concept_index_worker() { stopped_workers+=("concept"); }

TOKEN_FILE="/definitely/not/a/token/file"
EXTRA_ARGS=("4242")
stop_server

LOGS="$(printf '%s\n' "${logs[@]}")" \
STOPPED_WORKERS="$(printf '%s\n' "${stopped_workers[@]}")" \
python3 - <<PY
import json
import os
print(json.dumps({
    "logs": [line for line in os.environ.get("LOGS", "").splitlines() if line],
    "stopped_workers": [
        line for line in os.environ.get("STOPPED_WORKERS", "").splitlines() if line
    ],
}))
PY
"""
    )

    assert "Exact PID stop: preserving other Von server processes." in payload["logs"]
    assert not any(line.startswith("broad sweep:") for line in payload["logs"])
    assert payload["stopped_workers"] == ["rag", "concept"]


def test_stale_server_cleanup_does_not_match_deploy_local_main_by_basename(
    tmp_path: Path,
) -> None:
    target = tmp_path / "main.py"
    controller = tmp_path / "deploy_local_main.py"
    body = "import time\ntime.sleep(30)\n"
    target.write_text(body, encoding="utf-8")
    controller.write_text(body, encoding="utf-8")
    target_process = subprocess.Popen([sys.executable, str(target)])
    controller_process = subprocess.Popen([sys.executable, str(controller)])
    try:
        time.sleep(0.2)
        command = f"""
set -euo pipefail
cd {shlex_quote(str(REPO_ROOT))}
. ./run.sh help -NoBackupMigrate >/dev/null
ROOT={shlex_quote(str(tmp_path))}
python_cmd() {{ printf '%s' {shlex_quote(sys.executable)}; }}
stop_python_processes_by_script main.py test-server
""".strip()
        cleanup = subprocess.run(
            ["bash", "-c", command],
            text=True,
            capture_output=True,
            check=True,
        )
        assert "Stopped stale test-server process(es)" in cleanup.stdout
        target_process.wait(timeout=5)
        assert controller_process.poll() is None
    finally:
        for process in (target_process, controller_process):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


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
	log_mongo_ssh_tunnel_status() { :; }

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
