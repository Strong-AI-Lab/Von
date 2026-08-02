#!/usr/bin/env python3
"""Install or inspect the standalone Atlas egress reconciler LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

DEFAULT_LABEL = "org.strongailab.von.atlas-egress-reconciler"
DEFAULT_INTERVAL_SECONDS = 3600


def _validate_label(label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
        raise ValueError("launchd label contains invalid characters")


def _private_existing_file(
    value: str,
    description: str,
    *,
    executable: bool = False,
    allow_root_owner: bool = False,
    allow_symlink: bool = False,
) -> Path:
    path = Path(value).expanduser()
    if (
        not path.is_absolute()
        or (path.is_symlink() and not allow_symlink)
        or not path.is_file()
    ):
        raise ValueError(f"{description} must be an absolute regular file")
    resolved = path.resolve(strict=True)
    stat_result = resolved.stat()
    allowed_owners = {os.getuid(), 0} if allow_root_owner else {os.getuid()}
    if stat_result.st_uid not in allowed_owners:
        raise ValueError(f"{description} must be owned by the current user")
    if stat_result.st_mode & 0o022:
        raise ValueError(f"{description} must not be group- or other-writable")
    if executable and not os.access(path, os.X_OK):
        raise ValueError(f"{description} must be executable")
    return path


def build_program_arguments(
    *, python_executable: Path, script_path: Path, config_path: Path
) -> list[str]:
    return [
        str(python_executable),
        str(script_path),
        "--config",
        str(config_path),
        "cycle",
        "--notify",
    ]


def build_launch_agent(
    *,
    label: str,
    program_arguments: Sequence[str],
    interval_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    if interval_seconds < 300:
        raise ValueError("interval must be at least 300 seconds")
    return {
        "Label": label,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def _service_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _run_launchctl(
    arguments: Sequence[str], *, check: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _service_is_loaded(label: str) -> bool:
    return (
        _run_launchctl(["print", _service_target(label)], check=False).returncode == 0
    )


def _service_pid(label: str) -> int | None:
    result = _run_launchctl(["print", _service_target(label)], check=False)
    if result.returncode != 0:
        return None
    match = re.search(r"\bpid = (\d+)", result.stdout)
    return int(match.group(1)) if match else None


def _write_private_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("LaunchAgent directory must have owner-only permissions")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_log_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("log directory must not be a symbolic link")
    if path.exists():
        if not path.is_dir() or path.stat().st_uid != os.getuid():
            raise ValueError("existing log directory must be owned by the current user")
        if path.stat().st_mode & 0o077:
            raise ValueError("existing log directory must have owner-only permissions")
        return
    if not path.parent.is_dir():
        raise ValueError("log directory parent must already exist")
    path.mkdir(mode=0o700)


def _validate_owned_plist(path: Path, label: str) -> None:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError) as exc:
        raise ValueError(f"refusing to modify invalid LaunchAgent: {path}") from exc
    if not isinstance(payload, dict) or payload.get("Label") != label:
        raise ValueError("refusing to modify a LaunchAgent owned by another label")


def _installed_status(path: Path, label: str) -> tuple[str, bool | None]:
    if not path.exists():
        return "absent", None
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return "invalid", False
    matches = isinstance(payload, dict) and payload.get("Label") == label
    return ("configured" if matches else "foreign_label"), matches


def _status(label: str, plist_path: Path) -> dict[str, object]:
    configuration_status, label_matches = _installed_status(plist_path, label)
    loaded = _service_is_loaded(label)
    return {
        "label": label,
        "installed": plist_path.is_file(),
        "loaded": loaded,
        "service_pid": _service_pid(label) if loaded else None,
        "plist_path": str(plist_path),
        "configuration_status": configuration_status,
        "label_matches": label_matches,
    }


def _run_preflight(
    python_executable: Path, script_path: Path, config_path: Path
) -> None:
    result = subprocess.run(
        [
            str(python_executable),
            str(script_path),
            "--config",
            str(config_path),
            "preflight",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "reconciler preflight failed; LaunchAgent was not installed: "
            + result.stdout.strip()[:500]
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("reconciler preflight returned malformed JSON") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError("reconciler preflight did not report success")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--python-executable", default=str(Path(sys.executable)))
    parser.add_argument(
        "--script-path",
        default=str(Path(__file__).with_name("atlas_egress_reconciler.py")),
    )
    parser.add_argument(
        "--config-path",
        default=str(Path.home() / ".config/von/atlas-egress-reconciler.json"),
    )
    parser.add_argument(
        "--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument("--plist-path")
    parser.add_argument("--log-directory")
    parser.add_argument("--confirm-uninstall", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _validate_label(args.label)
    plist_path = (
        Path(args.plist_path).expanduser()
        if args.plist_path
        else Path.home() / "Library/LaunchAgents" / f"{args.label}.plist"
    )
    log_directory = (
        Path(args.log_directory).expanduser()
        if args.log_directory
        else Path.home() / "Library/Logs/Von"
    )
    if not plist_path.is_absolute() or not log_directory.is_absolute():
        raise ValueError("plist and log paths must be absolute")
    if args.action == "status":
        print(json.dumps(_status(args.label, plist_path), sort_keys=True))
        return 0
    if args.action == "uninstall":
        if not args.confirm_uninstall:
            raise ValueError("uninstall requires --confirm-uninstall")
        if plist_path.exists():
            _validate_owned_plist(plist_path, args.label)
        loaded = _service_is_loaded(args.label)
        if loaded and not plist_path.exists():
            raise RuntimeError("refusing to remove a loaded service without its plist")
        if loaded:
            _run_launchctl(["bootout", _service_target(args.label)], check=True)
        plist_path.unlink(missing_ok=True)
        print(json.dumps(_status(args.label, plist_path), sort_keys=True))
        return 0

    python_executable = _private_existing_file(
        args.python_executable,
        "Python executable",
        executable=True,
        allow_root_owner=True,
        allow_symlink=True,
    )
    script_path = _private_existing_file(args.script_path, "reconciler script")
    config_path = _private_existing_file(args.config_path, "configuration")
    if not args.skip_preflight:
        _run_preflight(python_executable, script_path, config_path)
    _prepare_log_directory(log_directory)
    payload = build_launch_agent(
        label=args.label,
        program_arguments=build_program_arguments(
            python_executable=python_executable,
            script_path=script_path,
            config_path=config_path,
        ),
        interval_seconds=args.interval_seconds,
        stdout_path=log_directory / "atlas-egress-reconciler.stdout.log",
        stderr_path=log_directory / "atlas-egress-reconciler.stderr.log",
    )
    previous = plist_path.read_bytes() if plist_path.is_file() else None
    was_loaded = _service_is_loaded(args.label)
    if previous is not None:
        _validate_owned_plist(plist_path, args.label)
    if was_loaded and previous is None:
        raise RuntimeError("refusing to replace a loaded service without its plist")
    try:
        if was_loaded:
            _run_launchctl(["bootout", _service_target(args.label)], check=True)
        _write_private_bytes(
            plist_path,
            plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False),
        )
        _run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=True)
        if not _service_is_loaded(args.label):
            raise RuntimeError("launchd did not load the reconciler")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if _service_is_loaded(args.label):
            _run_launchctl(["bootout", _service_target(args.label)], check=False)
        if previous is None:
            plist_path.unlink(missing_ok=True)
        else:
            _write_private_bytes(plist_path, previous)
            if was_loaded:
                _run_launchctl(
                    ["bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                    check=True,
                )
        raise RuntimeError(
            f"install failed and prior state was restored: {exc}"
        ) from exc
    print(json.dumps(_status(args.label, plist_path), sort_keys=True))
    return 0


def main() -> int:
    try:
        return run()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
