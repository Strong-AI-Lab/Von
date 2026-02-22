"""Process hygiene helpers for MCP stdio servers.

These servers should be one-process-per-parent-host. In practice, IDE restarts
or wrapper failures can leave orphaned sibling processes. This guard best-effort
terminates older sibling instances running the same server script.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Sequence


def _normalise_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _cmdline_matches_script(cmdline: Sequence[str], script_path: str) -> bool:
    if not cmdline:
        return False
    script_norm = _normalise_path(script_path)
    script_name = Path(script_norm).name.lower()
    script_fragment = script_norm.lower().replace("\\", "/")

    for token in cmdline:
        token_text = str(token)
        token_norm = _normalise_path(token_text)
        if token_norm == script_norm:
            return True

        token_fragment = token_text.lower().replace("\\", "/")
        if token_fragment.endswith(f"/{script_name}") or token_fragment == script_name:
            return True
        if token_fragment == script_fragment:
            return True

    return False


def terminate_duplicate_sibling_servers(
    script_path: str,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Terminate sibling server processes under the same parent process.

    This is intentionally conservative:
    - only processes with the same parent PID are considered
    - only processes whose command line matches this server script are targeted
    - this process is never targeted
    """

    enabled = os.getenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "1").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return {"matched": 0, "terminated": 0, "killed": 0, "failed": 0}

    try:
        import psutil
    except Exception:
        return {"matched": 0, "terminated": 0, "killed": 0, "failed": 0}

    current_pid = os.getpid()
    parent_pid = os.getppid()
    matched = 0
    terminated = 0
    killed = 0
    failed = 0

    try:
        proc_iter = psutil.process_iter(["pid", "cmdline"])
    except Exception:
        return {"matched": 0, "terminated": 0, "killed": 0, "failed": 0}

    for proc in proc_iter:
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid == current_pid:
                continue
            try:
                ppid = int(proc.ppid())
            except Exception:
                try:
                    ppid = int(proc.info.get("ppid") or 0)
                except Exception:
                    continue
            if ppid != parent_pid:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not isinstance(cmdline, Iterable):
                continue
            cmdline_list = [str(part) for part in cmdline]
            if not _cmdline_matches_script(cmdline_list, script_path):
                continue

            matched += 1
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
                terminated += 1
                if log_fn is not None:
                    log_fn(f"[mcp-guard] terminated stale sibling pid={pid}")
                continue
            except psutil.TimeoutExpired:
                pass

            try:
                proc.kill()
                killed += 1
                if log_fn is not None:
                    log_fn(f"[mcp-guard] killed stale sibling pid={pid}")
            except Exception:
                failed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            failed += 1

    return {
        "matched": matched,
        "terminated": terminated,
        "killed": killed,
        "failed": failed,
    }
