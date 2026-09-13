"""Deterministic per-instance systemd user units; no model-authored commands."""

import json
import subprocess
from pathlib import Path


def _quote(value, *, command=False):
    value = str(value)
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError("Invalid systemd argument")
    if command:
        value = value.replace("$", "$$")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    )


def install(spec):
    settings = spec["systemd"]
    root = Path(spec["instance_root"])
    unit_dir = Path(settings["unit_directory"])
    unit_dir.mkdir(parents=True, exist_ok=True)
    name = "von-coding-" + spec["instance_id"]
    marker = "# Managed coding instance " + spec["instance_id"] + " at " + str(root)
    memory = int(settings["memory_max_bytes"])
    interval = int(settings.get("poll_interval_seconds", 300))
    if memory <= 0 or interval < 30:
        raise ValueError("Invalid systemd resource/cadence binding")
    current = root / "releases/current"
    argv = [
        Path(spec["worker_config"]["python_environment"]) / "bin/python",
        current / "scripts/codex_von_worker.py",
        "--backend-root",
        current,
        "--config",
        root / "worker.json",
    ]
    service = [
        marker,
        "[Unit]",
        "Description=Von coding instance " + spec["instance_id"],
        "[Service]",
        "Type=oneshot",
        "UMask=0077",
        "KillMode=control-group",
        "MemoryMax=" + str(memory),
        "MemorySwapMax=0",
        "TimeoutStartSec=infinity",
        "ExecStart=" + " ".join(_quote(value, command=True) for value in argv),
    ]
    for path in settings.get("environment_files", []):
        if not Path(path).is_file():
            raise ValueError("Enrolled environment file is not prepared")
        service.append("EnvironmentFile=" + _quote(path))
    timer = [
        marker,
        "[Unit]",
        "Description=Poll Von coding instance " + spec["instance_id"],
        "[Timer]",
        "OnActiveSec=60",
        "OnUnitInactiveSec=" + str(interval),
        "Unit=" + name + ".service",
        "[Install]",
        "WantedBy=timers.target",
    ]
    for suffix, lines in (("service", service), ("timer", timer)):
        path = unit_dir / (name + "." + suffix)
        if path.exists() and not path.read_text().startswith(marker + "\n"):
            raise PermissionError("Refusing to replace an unenrolled service unit")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n")
        temporary.replace(path)
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=True, capture_output=True
    )


def control(spec, action):
    name = "von-coding-" + spec["instance_id"] + ".timer"
    if action in {"enable", "disable"}:
        subprocess.run(
            ["systemctl", "--user", action, "--now", name],
            check=True,
            capture_output=True,
        )
        return ""
    enabled = subprocess.run(
        ["systemctl", "--user", "is-enabled", "--quiet", name],
        capture_output=True,
        check=False,
    )
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", name],
        capture_output=True,
        check=False,
    )
    return json.dumps(
        {
            "enabled": enabled.returncode == 0,
            "active": active.returncode == 0,
            "unit": name,
        }
    )
