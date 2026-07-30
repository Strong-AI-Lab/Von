#!/usr/bin/env python3
"""Install or inspect a launchd-supervised loopback SSH tunnel for MongoDB.

The generated LaunchAgent contains SSH transport configuration only. MongoDB
credentials remain in Von's existing ``MONGO_URI`` or ``MONGO_URI_FILE`` and
must never be supplied to this command.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import socket
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LABEL = "org.strongailab.von.mongo-atlas-tunnel"
_SSH_DESTINATION_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9._-]*@"
    r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*|\[[0-9A-Fa-f:]+\])$"
)
_REMOTE_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])$")


@dataclass(frozen=True)
class Forward:
    local_port: int
    remote_host: str
    remote_port: int

    @property
    def ssh_argument(self) -> str:
        return f"127.0.0.1:{self.local_port}:" f"{self.remote_host}:{self.remote_port}"


def _tcp_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _forward(value: str) -> Forward:
    parts = value.rsplit(":", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "forward must be LOCAL_PORT:REMOTE_HOST:REMOTE_PORT"
        )
    local_port = _tcp_port(parts[0])
    remote_host = parts[1].strip()
    remote_port = _tcp_port(parts[2])
    if not _REMOTE_HOST_RE.fullmatch(remote_host):
        raise argparse.ArgumentTypeError("forward has an invalid remote host")
    return Forward(local_port, remote_host, remote_port)


def _absolute_existing_file(
    value: str,
    *,
    description: str,
    allow_public_read: bool = False,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{description} must be an absolute path")
    if not path.is_file():
        raise ValueError(f"{description} must be an existing regular file")
    forbidden_mode = 0o022 if allow_public_read else 0o077
    if path.stat().st_mode & forbidden_mode:
        raise ValueError(f"{description} has unsafe group or other permissions")
    return path


def _validate_unique_local_ports(forwards: Sequence[Forward]) -> None:
    ports = [forward.local_port for forward in forwards]
    if not ports:
        raise ValueError("at least one forward is required")
    if len(ports) != len(set(ports)):
        raise ValueError("each forward must use a distinct local port")


def _validate_label(label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        raise ValueError(
            "launchd label may contain only letters, numbers, dot, dash and underscore"
        )


def build_program_arguments(
    *,
    ssh_destination: str,
    identity_file: Path,
    known_hosts_file: Path,
    forwards: Sequence[Forward],
) -> list[str]:
    if not _SSH_DESTINATION_RE.fullmatch(ssh_destination):
        raise ValueError("SSH destination must be a simple USER@HOST value")
    _validate_unique_local_ports(forwards)
    arguments = [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "TCPKeepAlive=yes",
        "-o",
        "ControlMaster=no",
        "-i",
        str(identity_file),
    ]
    for forward in forwards:
        arguments.extend(["-L", forward.ssh_argument])
    arguments.append(ssh_destination)
    return arguments


def build_launch_agent(
    *,
    label: str,
    program_arguments: Sequence[str],
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    return {
        "Label": label,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def _service_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _run_launchctl(
    arguments: Sequence[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _service_is_loaded(label: str) -> bool:
    result = _run_launchctl(["print", _service_target(label)], check=False)
    return result.returncode == 0


def _service_pid(label: str) -> int | None:
    result = _run_launchctl(["print", _service_target(label)], check=False)
    if result.returncode != 0:
        return None
    match = re.search(r"\bpid = (\d+)", result.stdout)
    return int(match.group(1)) if match else None


def _listener_pids(port: int) -> set[int]:
    result = subprocess.run(
        [
            "/usr/sbin/lsof",
            "-nP",
            "-t",
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if result.returncode not in {0, 1}:
        return set()
    return {
        int(line)
        for line in result.stdout.splitlines()
        if line.strip().isdigit()
    }


def _ports_are_available(forwards: Sequence[Forward]) -> bool:
    probes: list[socket.socket] = []
    try:
        for forward in forwards:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", forward.local_port))
            probes.append(probe)
        return True
    except OSError:
        return False
    finally:
        for probe in probes:
            probe.close()


def _wait_for_listeners(
    label: str,
    forwards: Sequence[Forward],
    *,
    timeout_seconds: float = 15.0,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pid = _service_pid(label)
        if pid is not None and all(
            pid in _listener_pids(forward.local_port) for forward in forwards
        ):
            return True
        time.sleep(0.2)
    return False


def _write_private_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_plist(path: Path, payload: dict[str, object]) -> None:
    data = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)
    _write_private_bytes(path, data)


def _prepare_log_directory(path: Path) -> None:
    """Create a private log directory without mutating an existing path."""

    if path.is_symlink():
        raise ValueError("log directory must not be a symbolic link")
    if path.exists():
        if not path.is_dir():
            raise ValueError("log directory path must be a directory")
        stat_result = path.stat()
        if stat_result.st_uid != os.getuid():
            raise ValueError("existing log directory must be owned by the current user")
        if stat_result.st_mode & 0o077 or stat_result.st_mode & 0o700 != 0o700:
            raise ValueError(
                "existing log directory must have owner-only rwx permissions"
            )
        return
    if not path.parent.is_dir():
        raise ValueError("log directory parent must already exist")
    path.mkdir(mode=0o700)


def _bootstrap_service(label: str, plist_path: Path) -> None:
    del label
    _run_launchctl(
        ["bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        check=True,
    )


def _restore_previous_install(
    *,
    label: str,
    plist_path: Path,
    previous_plist: bytes | None,
    was_loaded: bool,
) -> None:
    rollback_errors: list[str] = []
    try:
        if _service_is_loaded(label):
            _run_launchctl(["bootout", _service_target(label)], check=True)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        rollback_errors.append(f"could not boot out failed replacement: {exc}")
    try:
        if previous_plist is None:
            plist_path.unlink(missing_ok=True)
        else:
            _write_private_bytes(plist_path, previous_plist)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        rollback_errors.append(f"could not restore previous plist: {exc}")
    if was_loaded and previous_plist is not None:
        try:
            _bootstrap_service(label, plist_path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            rollback_errors.append(f"could not reload previous service: {exc}")
    if rollback_errors:
        raise RuntimeError("; ".join(rollback_errors))


def _status_payload(
    *,
    label: str,
    plist_path: Path,
    forwards: Sequence[Forward],
) -> dict[str, object]:
    loaded = _service_is_loaded(label)
    pid = _service_pid(label) if loaded else None
    configuration_status, plist_valid, label_matches = _installed_plist_status(
        plist_path, label
    )
    listeners = {
        str(forward.local_port): bool(
            pid is not None and pid in _listener_pids(forward.local_port)
        )
        for forward in forwards
    }
    return {
        "label": label,
        "installed": plist_path.is_file(),
        "loaded": loaded,
        "plist_path": str(plist_path),
        "configuration_status": configuration_status,
        "plist_valid": plist_valid,
        "label_matches": label_matches,
        "service_pid": pid,
        "listeners_checked": bool(forwards),
        "listener_coverage_complete": bool(listeners) and all(listeners.values()),
        "listeners": listeners,
        "mongo_route_probed": False,
    }


def _installed_plist_status(
    plist_path: Path,
    expected_label: str,
) -> tuple[str, bool | None, bool | None]:
    if not plist_path.exists():
        return ("absent", None, None)
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return ("invalid", False, None)
    if not isinstance(payload, dict) or not isinstance(payload.get("Label"), str):
        return ("invalid", False, None)
    label_matches = payload["Label"] == expected_label
    return (
        "configured" if label_matches else "foreign_label",
        True,
        label_matches,
    )


def _forwards_from_plist(plist_path: Path) -> list[Forward]:
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list):
        return []
    forwards: list[Forward] = []
    for index, argument in enumerate(arguments[:-1]):
        if argument != "-L" or not isinstance(arguments[index + 1], str):
            continue
        specification = arguments[index + 1]
        prefix = "127.0.0.1:"
        if not specification.startswith(prefix):
            continue
        try:
            local_port, remote = specification[len(prefix) :].split(":", 1)
            remote_host, remote_port = remote.rsplit(":", 1)
            forwards.append(
                _forward(f"{local_port}:{remote_host}:{remote_port}")
            )
        except (ValueError, argparse.ArgumentTypeError):
            return []
    return forwards


def _validate_installed_plist_label(plist_path: Path, expected_label: str) -> None:
    """Refuse to replace or remove a file that is not this LaunchAgent."""

    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError) as exc:
        raise ValueError(
            f"refusing to modify invalid LaunchAgent plist: {plist_path}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("Label"), str):
        raise TypeError(
            f"refusing to modify invalid LaunchAgent plist: {plist_path}"
        )
    if payload["Label"] != expected_label:
        raise ValueError(
            "refusing to modify a LaunchAgent plist owned by a different label"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--ssh-destination")
    parser.add_argument("--identity-file")
    parser.add_argument(
        "--known-hosts-file",
        default=str(Path.home() / ".ssh" / "known_hosts"),
    )
    parser.add_argument(
        "--forward",
        action="append",
        type=_forward,
        default=[],
        metavar="LOCAL_PORT:REMOTE_HOST:REMOTE_PORT",
    )
    parser.add_argument("--plist-path")
    parser.add_argument("--log-directory")
    parser.add_argument("--confirm-uninstall", action="store_true")
    return parser


def _paths_from_args(args: argparse.Namespace) -> tuple[Path, Path]:
    plist_path = (
        Path(args.plist_path).expanduser()
        if args.plist_path
        else Path.home() / "Library" / "LaunchAgents" / f"{args.label}.plist"
    )
    log_directory = (
        Path(args.log_directory).expanduser()
        if args.log_directory
        else Path.home() / "Library" / "Logs" / "Von"
    )
    if not plist_path.is_absolute() or not log_directory.is_absolute():
        raise ValueError("plist and log paths must be absolute")
    return plist_path, log_directory


def run(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _validate_label(args.label)
    plist_path, log_directory = _paths_from_args(args)
    forwards: list[Forward] = list(args.forward)

    if args.action == "status":
        if not forwards:
            forwards = _forwards_from_plist(plist_path)
        if forwards:
            _validate_unique_local_ports(forwards)
        print(
            json.dumps(
                _status_payload(
                    label=args.label,
                    plist_path=plist_path,
                    forwards=forwards,
                ),
                sort_keys=True,
            )
        )
        return 0

    if args.action == "uninstall":
        if not args.confirm_uninstall:
            raise ValueError("uninstall requires --confirm-uninstall")
        if plist_path.exists():
            _validate_installed_plist_label(plist_path, args.label)
        loaded = _service_is_loaded(args.label)
        if loaded and not plist_path.exists():
            raise RuntimeError(
                "refusing to boot out a loaded service without its matching plist"
            )
        if loaded:
            _run_launchctl(["bootout", _service_target(args.label)], check=True)
        plist_path.unlink(missing_ok=True)
        print(
            json.dumps(
                {"label": args.label, "installed": False, "loaded": False},
                sort_keys=True,
            )
        )
        return 0

    if not args.ssh_destination or not args.identity_file:
        raise ValueError("install requires --ssh-destination and --identity-file")
    _validate_unique_local_ports(forwards)
    identity_file = _absolute_existing_file(
        args.identity_file,
        description="identity file",
    )
    known_hosts_file = _absolute_existing_file(
        args.known_hosts_file,
        description="known-hosts file",
        allow_public_read=True,
    )
    program_arguments = build_program_arguments(
        ssh_destination=args.ssh_destination,
        identity_file=identity_file,
        known_hosts_file=known_hosts_file,
        forwards=forwards,
    )
    _prepare_log_directory(log_directory)
    payload = build_launch_agent(
        label=args.label,
        program_arguments=program_arguments,
        stdout_path=log_directory / "mongo-atlas-tunnel.stdout.log",
        stderr_path=log_directory / "mongo-atlas-tunnel.stderr.log",
    )

    was_loaded = _service_is_loaded(args.label)
    previous_plist = plist_path.read_bytes() if plist_path.is_file() else None
    if previous_plist is not None:
        _validate_installed_plist_label(plist_path, args.label)
    if was_loaded and previous_plist is None:
        raise RuntimeError(
            "refusing to replace a loaded service whose plist cannot be recovered"
        )
    try:
        if was_loaded:
            _run_launchctl(["bootout", _service_target(args.label)], check=True)
        if not _ports_are_available(forwards):
            raise RuntimeError(
                "one or more loopback ports are already owned; no process was killed"
            )
        _write_plist(plist_path, payload)
        _bootstrap_service(args.label, plist_path)
        if not _wait_for_listeners(args.label, forwards):
            raise RuntimeError(
                "launchd loaded the tunnel but not all loopback listeners became ready"
            )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        try:
            _restore_previous_install(
                label=args.label,
                plist_path=plist_path,
                previous_plist=previous_plist,
                was_loaded=was_loaded,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as rollback_exc:
            raise RuntimeError(
                f"{exc}; rollback also failed: {rollback_exc}"
            ) from exc
        raise
    print(
        json.dumps(
            _status_payload(
                label=args.label,
                plist_path=plist_path,
                forwards=forwards,
            ),
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    try:
        return run()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
