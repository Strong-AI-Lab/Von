"""Process hygiene helpers for MCP stdio servers.

These servers should be one-process-per-parent-host. In practice, IDE restarts
or wrapper failures can leave orphaned sibling processes. This guard best-effort
terminates older sibling instances running the same server script.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


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


def _process_pid(proc: Any) -> int:
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


def _process_cmdline(proc: Any) -> list[str]:
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

    def _collect_sibling_processes() -> list[Any]:
        """Collect direct sibling processes (same parent) efficiently.

        On Windows, iterating every process and reading cmdline metadata can
        take several seconds. Querying the parent process for its direct
        children is substantially cheaper and avoids MCP startup timeouts.
        """
        try:
            parent_proc = psutil.Process(parent_pid)
            return list(parent_proc.children(recursive=False))
        except Exception:
            # Conservative fallback when parent lookup fails.
            siblings: list[Any] = []
            try:
                proc_iter = psutil.process_iter(["pid", "ppid", "cmdline"])
            except Exception:
                return siblings

            for proc in proc_iter:
                try:
                    pid_value = int(proc.info.get("pid") or 0)
                    if pid_value <= 0 or pid_value == current_pid:
                        continue
                    ppid_value = proc.info.get("ppid")
                    if int(ppid_value or 0) != parent_pid:
                        continue
                    siblings.append(proc)
                except Exception:
                    continue
            return siblings

    try:
        sibling_processes = _collect_sibling_processes()
    except Exception:
        return {"matched": 0, "terminated": 0, "killed": 0, "failed": 0}

    for proc in sibling_processes:
        try:
            pid = _process_pid(proc)
            if pid <= 0 or pid == current_pid:
                continue
            cmdline_list = _process_cmdline(proc)
            if not cmdline_list:
                continue
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
