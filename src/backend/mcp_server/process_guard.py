"""Process hygiene helpers for MCP stdio servers.

These servers may be launched under IDE-owned MCP hosts that legitimately keep
multiple sibling sessions alive at once. Aggressively killing same-parent
siblings can therefore drop active transports mid-tool-call. The duplicate
termination guard remains available for explicit recovery/debug sessions, but it
must be enabled intentionally rather than running by default.

The preferred support surface is now a lightweight helper-lease registry:

- each helper process writes a small lease file under a shared temp directory;
- leases carry helper kind, pid, parent pid, owner token, and heartbeat data;
- on startup, a helper may safely reclaim older same-owner duplicates of the
  same helper kind without touching helpers owned by other sessions;
- diagnostics can inspect the lease inventory without relying on ad-hoc shell
  probes alone.
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.backend.utilities.process_hygiene import (
    cmdline_matches_script as _cmdline_matches_script,
    process_cmdline as _process_cmdline,
    process_pid as _process_pid,
)

_LEASE_SCHEMA_VERSION = "mcp_helper_lease.v1"
_DEFAULT_HEARTBEAT_INTERVAL_SEC = 15.0
_ACTIVE_LEASE_HANDLES: list["MCPHelperLeaseHandle"] = []


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _helper_kind_for_script(script_path: str) -> str:
    stem = Path(script_path).stem.strip()
    return stem or "unknown_mcp_helper"


def _helper_registry_dir() -> Path:
    override = os.getenv("VON_MCP_HELPER_REGISTRY_DIR")
    if isinstance(override, str) and override.strip():
        return Path(override.strip())
    return Path(tempfile.gettempdir()) / "von_mcp_helper_leases"


def _helper_lease_path(registry_dir: Path, pid: int) -> Path:
    return registry_dir / f"{pid}.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _build_process_index() -> dict[int, Any]:
    try:
        import psutil
    except Exception:
        return {}

    index: dict[int, Any] = {}
    try:
        proc_iter = psutil.process_iter(["pid"])
    except Exception:
        return index

    for proc in proc_iter:
        try:
            pid = _process_pid(proc)
            if pid > 0:
                index[pid] = proc
        except Exception:
            continue
    return index


def _terminate_process(proc: Any, *, log_fn: Callable[[str], None] | None = None) -> str:
    try:
        import psutil
    except Exception:
        try:
            proc.terminate()
            return "terminated"
        except Exception:
            return "failed"

    try:
        proc.terminate()
        proc.wait(timeout=1.0)
        if log_fn is not None:
            log_fn(f"[mcp-guard] terminated helper pid={_process_pid(proc)}")
        return "terminated"
    except psutil.TimeoutExpired:
        pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "missing"
    except Exception:
        return "failed"

    try:
        proc.kill()
        if log_fn is not None:
            log_fn(f"[mcp-guard] killed helper pid={_process_pid(proc)}")
        return "killed"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "missing"
    except Exception:
        return "failed"


def _iter_helper_lease_records(registry_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not registry_dir.exists():
        return []

    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(registry_dir.glob("*.json")):
        payload = _read_json_file(path)
        if payload is None:
            continue
        records.append((path, payload))
    return records


def _remove_file(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except TypeError:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            return False
    except Exception:
        return False
    return True


class MCPHelperLeaseHandle:
    """Owns one helper lease file and keeps its heartbeat fresh."""

    def __init__(
        self,
        *,
        lease_path: Path,
        record: dict[str, Any],
        heartbeat_interval_sec: float,
    ) -> None:
        self.lease_path = lease_path
        self.record = dict(record)
        self._heartbeat_interval_sec = max(0.0, float(heartbeat_interval_sec))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        if self._heartbeat_interval_sec > 0:
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"mcp-helper-lease-{self.record.get('pid')}",
                daemon=True,
            )
            self._thread.start()

    def touch(self) -> None:
        heartbeat_at = _utc_now_iso()
        heartbeat_ts = time.time()
        self.record["last_heartbeat_utc"] = heartbeat_at
        self.record["last_heartbeat_epoch_sec"] = heartbeat_ts
        _write_json_atomic(self.lease_path, self.record)

    def close(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.2)
        _remove_file(self.lease_path)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(timeout=self._heartbeat_interval_sec):
            try:
                self.touch()
            except Exception:
                # Do not crash server startup or MCP tool handling because the
                # optional helper heartbeat could not be persisted.
                continue


def _close_all_active_leases() -> None:
    while _ACTIVE_LEASE_HANDLES:
        handle = _ACTIVE_LEASE_HANDLES.pop()
        try:
            handle.close()
        except Exception:
            continue


atexit.register(_close_all_active_leases)


def _build_current_helper_record(script_path: str) -> dict[str, Any]:
    now_epoch = time.time()
    explicit_owner_token = os.getenv("VON_MCP_HELPER_OWNER_TOKEN")
    owner_token_is_explicit = bool(
        isinstance(explicit_owner_token, str) and explicit_owner_token.strip()
    )
    owner_token = (
        explicit_owner_token.strip()
        if owner_token_is_explicit and explicit_owner_token is not None
        else f"parent:{os.getppid()}"
    )
    owner_label = (os.getenv("VON_MCP_HELPER_OWNER_LABEL") or "").strip()
    return {
        "schema_version": _LEASE_SCHEMA_VERSION,
        "helper_kind": _helper_kind_for_script(script_path),
        "script_path": str(Path(script_path).resolve()),
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "owner_token": owner_token,
        "owner_token_is_explicit": owner_token_is_explicit,
        "owner_label": owner_label,
        "hostname": socket.gethostname(),
        "started_at_utc": _utc_now_iso(),
        "started_at_epoch_sec": now_epoch,
        "last_heartbeat_utc": _utc_now_iso(),
        "last_heartbeat_epoch_sec": now_epoch,
    }


def _prune_dead_lease_files(
    registry_dir: Path,
    *,
    process_index: dict[int, Any],
) -> dict[str, Any]:
    removed_files = 0
    removed_pids: list[int] = []
    invalid_files = 0

    for path, payload in _iter_helper_lease_records(registry_dir):
        pid = int(payload.get("pid") or 0)
        if pid <= 0:
            if _remove_file(path):
                invalid_files += 1
            continue
        if pid in process_index:
            continue
        if _remove_file(path):
            removed_files += 1
            removed_pids.append(pid)

    return {
        "removed_dead_files": removed_files,
        "removed_dead_pids": removed_pids,
        "removed_invalid_files": invalid_files,
    }


def _harvest_same_owner_duplicates(
    current_record: dict[str, Any],
    *,
    registry_dir: Path,
    process_index: dict[int, Any],
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    helper_kind = str(current_record.get("helper_kind") or "")
    owner_token = str(current_record.get("owner_token") or "")
    current_pid = int(current_record.get("pid") or 0)
    current_started = _safe_float(current_record.get("started_at_epoch_sec")) or 0.0
    if not bool(current_record.get("owner_token_is_explicit")):
        return {
            "matched": 0,
            "terminated": 0,
            "killed": 0,
            "failed": 0,
            "harvested_pids": [],
            "skipped": "owner_token_not_explicit",
        }

    matched = 0
    terminated = 0
    killed = 0
    failed = 0
    harvested_pids: list[int] = []

    for path, payload in _iter_helper_lease_records(registry_dir):
        pid = int(payload.get("pid") or 0)
        if pid <= 0 or pid == current_pid:
            continue
        if str(payload.get("helper_kind") or "") != helper_kind:
            continue
        if str(payload.get("owner_token") or "") != owner_token:
            continue
        if not bool(payload.get("owner_token_is_explicit")):
            continue

        started = _safe_float(payload.get("started_at_epoch_sec")) or 0.0
        if started > current_started:
            # Keep the newest owner-matching helper if races occur.
            continue

        proc = process_index.get(pid)
        if proc is None:
            _remove_file(path)
            continue

        matched += 1
        outcome = _terminate_process(proc, log_fn=log_fn)
        if outcome == "terminated":
            terminated += 1
            harvested_pids.append(pid)
            _remove_file(path)
            continue
        if outcome == "killed":
            killed += 1
            harvested_pids.append(pid)
            _remove_file(path)
            continue
        if outcome == "missing":
            _remove_file(path)
            continue
        failed += 1

    return {
        "matched": matched,
        "terminated": terminated,
        "killed": killed,
        "failed": failed,
        "harvested_pids": harvested_pids,
    }


def activate_mcp_helper_lifecycle(
    script_path: str,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Register helper lease, harvest stale same-owner duplicates, optionally run
    the old debug sibling killer, and keep the lease heartbeat updated.

    This is the authoritative startup path for MCP stdio helpers.
    """

    registry_dir = _helper_registry_dir()
    process_index = _build_process_index()
    prune_report = _prune_dead_lease_files(registry_dir, process_index=process_index)

    current_record = _build_current_helper_record(script_path)
    harvest_report = _harvest_same_owner_duplicates(
        current_record,
        registry_dir=registry_dir,
        process_index=process_index,
        log_fn=log_fn,
    )

    lease_path = _helper_lease_path(registry_dir, int(current_record["pid"]))
    _write_json_atomic(lease_path, current_record)

    heartbeat_interval = _safe_float(os.getenv("VON_MCP_HELPER_HEARTBEAT_SEC"))
    if heartbeat_interval is None:
        heartbeat_interval = _DEFAULT_HEARTBEAT_INTERVAL_SEC
    handle = MCPHelperLeaseHandle(
        lease_path=lease_path,
        record=current_record,
        heartbeat_interval_sec=heartbeat_interval,
    )
    _ACTIVE_LEASE_HANDLES.append(handle)

    debug_guard_report = terminate_duplicate_sibling_servers(
        script_path,
        log_fn=log_fn,
    )

    return {
        "helper_kind": current_record["helper_kind"],
        "pid": current_record["pid"],
        "parent_pid": current_record["parent_pid"],
        "owner_token": current_record["owner_token"],
        "owner_token_is_explicit": current_record["owner_token_is_explicit"],
        "registry_dir": str(registry_dir),
        "lease_path": str(lease_path),
        "prune_report": prune_report,
        "harvest_report": harvest_report,
        "debug_guard_report": debug_guard_report,
    }


def get_mcp_helper_inventory() -> dict[str, Any]:
    """Return a machine-readable snapshot of current helper leases."""

    registry_dir = _helper_registry_dir()
    process_index = _build_process_index()
    helpers: list[dict[str, Any]] = []

    for path, payload in _iter_helper_lease_records(registry_dir):
        pid = int(payload.get("pid") or 0)
        started = _safe_float(payload.get("started_at_epoch_sec"))
        heartbeat = _safe_float(payload.get("last_heartbeat_epoch_sec"))
        now_epoch = time.time()
        helpers.append(
            {
                "lease_file": str(path),
                "helper_kind": payload.get("helper_kind"),
                "script_path": payload.get("script_path"),
                "pid": pid,
                "parent_pid": int(payload.get("parent_pid") or 0),
                "owner_token": payload.get("owner_token"),
                "owner_token_is_explicit": bool(
                    payload.get("owner_token_is_explicit")
                ),
                "owner_label": payload.get("owner_label"),
                "hostname": payload.get("hostname"),
                "started_at_utc": payload.get("started_at_utc"),
                "last_heartbeat_utc": payload.get("last_heartbeat_utc"),
                "age_sec": round(max(0.0, now_epoch - started), 2)
                if started is not None
                else None,
                "heartbeat_age_sec": round(max(0.0, now_epoch - heartbeat), 2)
                if heartbeat is not None
                else None,
                "live": pid in process_index,
                "schema_version": payload.get("schema_version"),
            }
        )

    duplicate_groups: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for helper in helpers:
        key = (
            str(helper.get("helper_kind") or ""),
            str(helper.get("owner_token") or ""),
        )
        grouped.setdefault(key, []).append(helper)
    for (helper_kind, owner_token), group in grouped.items():
        if len(group) <= 1:
            continue
        duplicate_groups.append(
            {
                "helper_kind": helper_kind,
                "owner_token": owner_token,
                "pids": sorted(
                    int(item["pid"])
                    for item in group
                    if isinstance(item.get("pid"), int)
                ),
                "count": len(group),
            }
        )

    return {
        "success": True,
        "registry_dir": str(registry_dir),
        "helper_count": len(helpers),
        "live_helper_count": sum(1 for helper in helpers if helper.get("live")),
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_groups": duplicate_groups,
        "helpers": helpers,
    }


def terminate_duplicate_sibling_servers(
    script_path: str,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Terminate sibling server processes under the same parent process.

    This is intentionally conservative and is now an explicit recovery/debug
    path rather than the authoritative lifecycle policy.
    """

    enabled = os.getenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "0").strip().lower()
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
        """Collect direct sibling processes (same parent) efficiently."""
        try:
            parent_proc = psutil.Process(parent_pid)
            return list(parent_proc.children(recursive=False))
        except Exception:
            return []

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
            outcome = _terminate_process(proc, log_fn=log_fn)
            if outcome == "terminated":
                terminated += 1
            elif outcome == "killed":
                killed += 1
            elif outcome != "missing":
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
