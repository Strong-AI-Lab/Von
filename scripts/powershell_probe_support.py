from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Final

from src.backend.utilities.process_hygiene import (
    DEFAULT_LAUNCHER_CLEANUP_PROCESS_NAMES,
    terminate_processes_matching_script,
)


DEFAULT_POWERSHELL_PROBE_PREFIXES: Final[tuple[str, ...]] = (
    "run_ps1_probe_",
    "ps_stage_probe_",
)
DEFAULT_POWERSHELL_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_STALE_PROBE_MIN_AGE_SECONDS: Final[float] = 15 * 60.0


def powershell_probe_temp_root() -> Path:
    return Path(tempfile.gettempdir())


def iter_powershell_probe_dirs(
    *,
    temp_root: Path | None = None,
    prefixes: tuple[str, ...] = DEFAULT_POWERSHELL_PROBE_PREFIXES,
) -> list[Path]:
    root = temp_root or powershell_probe_temp_root()
    if not root.exists():
        return []

    probe_dirs: list[Path] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(prefixes):
            probe_dirs.append(entry)

    return sorted(probe_dirs)


def probe_dir_age_seconds(probe_dir: Path, *, now: float | None = None) -> float:
    reference = now if now is not None else time.time()
    return max(0.0, reference - probe_dir.stat().st_mtime)


def cleanup_powershell_probe_dir(
    probe_dir: Path,
    *,
    timeout_seconds: float = 1.0,
    remove_dir: bool = True,
) -> dict[str, object]:
    probe_script = probe_dir / "probe.ps1"
    killed_pids: list[int] = []
    if probe_script.exists():
        killed_pids = terminate_processes_matching_script(
            str(probe_script),
            timeout_seconds=timeout_seconds,
            candidate_process_names=DEFAULT_LAUNCHER_CLEANUP_PROCESS_NAMES,
        )

    removed = False
    error: str | None = None
    if remove_dir:
        try:
            shutil.rmtree(probe_dir)
            removed = True
        except FileNotFoundError:
            removed = True
        except Exception as exc:
            error = str(exc)

    return {
        "probe_dir": str(probe_dir),
        "killed_pids": killed_pids,
        "removed": removed,
        "error": error,
    }


def cleanup_stale_powershell_probe_dirs(
    *,
    temp_root: Path | None = None,
    min_age_seconds: float = DEFAULT_STALE_PROBE_MIN_AGE_SECONDS,
    prefixes: tuple[str, ...] = DEFAULT_POWERSHELL_PROBE_PREFIXES,
    timeout_seconds: float = 1.0,
    remove_dir: bool = True,
) -> list[dict[str, object]]:
    root = temp_root or powershell_probe_temp_root()
    now = time.time()
    cleaned: list[dict[str, object]] = []
    for probe_dir in iter_powershell_probe_dirs(temp_root=root, prefixes=prefixes):
        try:
            age_seconds = probe_dir_age_seconds(probe_dir, now=now)
        except FileNotFoundError:
            continue
        if age_seconds < min_age_seconds:
            continue
        result = cleanup_powershell_probe_dir(
            probe_dir,
            timeout_seconds=timeout_seconds,
            remove_dir=remove_dir,
        )
        result["age_seconds"] = age_seconds
        cleaned.append(result)

    return cleaned
