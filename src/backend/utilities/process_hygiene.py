"""Shared process-matching helpers for launcher and MCP process hygiene."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_LAUNCHER_CLEANUP_PROCESS_NAMES = {
    "python",
    "python.exe",
    "pythonw",
    "pythonw.exe",
    "pdm",
    "pdm.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
}


def normalise_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def cmdline_matches_script(cmdline: Sequence[str], script_path: str) -> bool:
    if not cmdline:
        return False
    script_norm = normalise_path(script_path)
    script_name = Path(script_norm).name.lower()
    script_fragment = script_norm.lower().replace("\\", "/")

    for token in cmdline:
        token_text = str(token)
        token_norm = normalise_path(token_text)
        if token_norm == script_norm:
            return True

        token_fragment = token_text.lower().replace("\\", "/")
        if token_fragment.endswith(f"/{script_name}") or token_fragment == script_name:
            return True
        if token_fragment == script_fragment:
            return True

    return False


def process_pid(proc: Any) -> int:
    info = getattr(proc, "info", None)
    if isinstance(info, dict):
        try:
            return int(info.get("pid") or 0)
        except Exception:
            return 0

    try:
        return int(getattr(proc, "pid", 0) or 0)
    except Exception:
        return 0


def process_name(proc: Any) -> str:
    info = getattr(proc, "info", None)
    if isinstance(info, dict):
        try:
            name = info.get("name")
            if name:
                return str(name)
        except Exception:
            pass

    try:
        name_getter = getattr(proc, "name", None)
        if callable(name_getter):
            name = name_getter()
            if name:
                return str(name)
    except Exception:
        return ""

    return ""


def process_cmdline(proc: Any) -> list[str]:
    info = getattr(proc, "info", None)
    if isinstance(info, dict):
        cmdline = info.get("cmdline")
        if isinstance(cmdline, Iterable) and not isinstance(cmdline, (str, bytes)):
            return [str(part) for part in cmdline]

    try:
        cmdline = proc.cmdline()
    except Exception:
        return []

    if not isinstance(cmdline, Iterable) or isinstance(cmdline, (str, bytes)):
        return []
    return [str(part) for part in cmdline]


def is_likely_launcher_cleanup_candidate(
    proc: Any,
    *,
    candidate_process_names: set[str] | None = None,
) -> bool:
    if not candidate_process_names:
        return True

    name = process_name(proc).strip().lower()
    if not name:
        # Fall back to cmdline inspection when process metadata omitted the name.
        return True

    return name in {entry.strip().lower() for entry in candidate_process_names}


def terminate_processes_matching_script(
    script_path: str,
    *,
    exclude_pid: int = 0,
    timeout_seconds: float = 1.0,
    candidate_process_names: set[str] | None = None,
) -> list[int]:
    """Best-effort termination of running processes whose cmdline matches a script path."""

    try:
        import psutil
    except Exception:
        return []

    current_pid = os.getpid()
    killed: list[int] = []
    process_names = candidate_process_names or DEFAULT_LAUNCHER_CLEANUP_PROCESS_NAMES

    try:
        proc_iter = psutil.process_iter(["pid", "name"])
    except Exception:
        return killed

    for proc in proc_iter:
        try:
            pid = process_pid(proc)
            if pid <= 0 or pid == current_pid or pid == exclude_pid:
                continue
            if not is_likely_launcher_cleanup_candidate(
                proc,
                candidate_process_names=process_names,
            ):
                continue
            cmdline_list = process_cmdline(proc)
            if not cmdline_list:
                continue
            if not cmdline_matches_script(cmdline_list, script_path):
                continue

            try:
                proc.terminate()
                proc.wait(timeout=timeout_seconds)
            except psutil.TimeoutExpired:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    return killed
