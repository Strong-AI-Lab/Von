from __future__ import annotations

import json
import importlib
import sys
import types
from pathlib import Path

from src.backend.mcp_server import process_guard


class _DummyPsutilError(Exception):
    pass


class _DummyTimeout(_DummyPsutilError):
    pass


class _DummyProcess:
    def __init__(
        self,
        *,
        pid: int,
        ppid: int,
        cmdline: list[str],
        timeout_on_wait: bool = False,
    ) -> None:
        self.pid = pid
        self.info = {"pid": pid, "ppid": ppid, "cmdline": cmdline}
        self.terminated = False
        self.killed = False
        self._timeout_on_wait = timeout_on_wait

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> None:  # noqa: ARG002 - signature mirrors psutil
        if self._timeout_on_wait:
            raise _DummyTimeout("timeout")

    def kill(self) -> None:
        self.killed = True

    def cmdline(self) -> list[str]:
        return list(self.info.get("cmdline") or [])


class _DummyChildProcess:
    def __init__(
        self,
        *,
        pid: int,
        cmdline: list[str],
        timeout_on_wait: bool = False,
    ) -> None:
        self.pid = pid
        self._cmdline = list(cmdline)
        self.terminated = False
        self.killed = False
        self._timeout_on_wait = timeout_on_wait

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> None:  # noqa: ARG002 - signature mirrors psutil
        if self._timeout_on_wait:
            raise _DummyTimeout("timeout")

    def kill(self) -> None:
        self.killed = True

    def cmdline(self) -> list[str]:
        return list(self._cmdline)


class _DummyParentProcess:
    def __init__(
        self,
        pid: int,
        processes: list[object],
        *,
        direct_children: list[object] | None = None,
    ) -> None:
        self.pid = pid
        self._processes = processes
        self._direct_children = direct_children

    def children(self, recursive: bool = False):  # noqa: ARG002 - psutil signature
        if self._direct_children is not None:
            return list(self._direct_children)
        children: list[object] = []
        for proc in self._processes:
            info = getattr(proc, "info", None)
            if not isinstance(info, dict):
                continue
            if int(info.get("ppid") or 0) == self.pid:
                children.append(proc)
        return children


def _write_lease(
    path: Path,
    *,
    helper_kind: str,
    pid: int,
    parent_pid: int,
    owner_token: str,
    owner_token_is_explicit: bool = True,
    started_at_epoch_sec: float = 1.0,
) -> None:
    payload = {
        "schema_version": "mcp_helper_lease.v1",
        "helper_kind": helper_kind,
        "script_path": f"C:/repo/{helper_kind}.py",
        "pid": pid,
        "parent_pid": parent_pid,
        "owner_token": owner_token,
        "owner_token_is_explicit": owner_token_is_explicit,
        "started_at_utc": "2026-04-18T00:00:00+00:00",
        "started_at_epoch_sec": started_at_epoch_sec,
        "last_heartbeat_utc": "2026-04-18T00:00:00+00:00",
        "last_heartbeat_epoch_sec": started_at_epoch_sec,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _install_fake_psutil(
    monkeypatch,
    processes: list[object],
    *,
    parent_children: list[object] | None = None,
) -> None:
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda _attrs: processes,
        Process=lambda pid: _DummyParentProcess(
            int(pid),
            processes,
            direct_children=parent_children,
        ),
        TimeoutExpired=_DummyTimeout,
        NoSuchProcess=_DummyPsutilError,
        AccessDenied=_DummyPsutilError,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)


def test_build_process_index_does_not_request_cmdline_from_broad_scan(
    monkeypatch,
) -> None:
    proc = _DummyProcess(
        pid=101,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    requested_attrs: list[list[str]] = []

    def _process_iter(attrs):
        requested_attrs.append(list(attrs))
        if "cmdline" in attrs:
            raise AssertionError("broad process index must not request cmdline")
        return [proc]

    fake_psutil = types.SimpleNamespace(process_iter=_process_iter)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    index = process_guard._build_process_index()

    assert requested_attrs == [["pid"]]
    assert index == {101: proc}


def test_cmdline_matches_script_by_full_path() -> None:
    script_path = "C:/repo/src/backend/mcp_server/mcp_stdio_server.py"
    cmdline = ["python", "C:/repo/src/backend/mcp_server/mcp_stdio_server.py"]
    assert process_guard._cmdline_matches_script(cmdline, script_path)


def test_cmdline_matches_script_by_filename() -> None:
    script_path = "C:/repo/src/backend/mcp_server/rag_mcp_stdio_server.py"
    cmdline = ["python", "-m", "pdm", "run", "python", "rag_mcp_stdio_server.py"]
    assert process_guard._cmdline_matches_script(cmdline, script_path)


def test_terminate_duplicate_sibling_servers_targets_only_siblings(
    monkeypatch,
) -> None:
    sibling_match = _DummyProcess(
        pid=101,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    different_parent = _DummyProcess(
        pid=102,
        ppid=99,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    different_script = _DummyProcess(
        pid=103,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/rag_mcp_stdio_server.py"],
    )
    timeout_then_kill = _DummyProcess(
        pid=104,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
        timeout_on_wait=True,
    )
    _install_fake_psutil(
        monkeypatch,
        [sibling_match, different_parent, different_script, timeout_then_kill],
    )

    monkeypatch.setattr(process_guard.os, "getpid", lambda: 200)
    monkeypatch.setattr(process_guard.os, "getppid", lambda: 50)
    monkeypatch.setenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "1")

    result = process_guard.terminate_duplicate_sibling_servers(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    assert result["matched"] == 2
    assert result["terminated"] == 1
    assert result["killed"] == 1
    assert result["failed"] == 0
    assert sibling_match.terminated is True
    assert timeout_then_kill.killed is True
    assert different_parent.terminated is False
    assert different_script.terminated is False


def test_terminate_duplicate_sibling_servers_skips_broad_fallback_scan(
    monkeypatch,
) -> None:
    requested_attrs: list[list[str]] = []

    def _process_iter(attrs):
        requested_attrs.append(list(attrs))
        raise AssertionError("sibling recovery must not broad-scan processes")

    fake_psutil = types.SimpleNamespace(
        process_iter=_process_iter,
        Process=lambda _pid: (_ for _ in ()).throw(_DummyPsutilError("parent")),
        TimeoutExpired=_DummyTimeout,
        NoSuchProcess=_DummyPsutilError,
        AccessDenied=_DummyPsutilError,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(process_guard.os, "getpid", lambda: 200)
    monkeypatch.setattr(process_guard.os, "getppid", lambda: 50)
    monkeypatch.setenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "1")

    result = process_guard.terminate_duplicate_sibling_servers(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    assert requested_attrs == []
    assert result == {"matched": 0, "terminated": 0, "killed": 0, "failed": 0}


def test_terminate_duplicate_sibling_servers_handles_real_child_process_objects(
    monkeypatch,
) -> None:
    sibling_match = _DummyChildProcess(
        pid=101,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    timeout_then_kill = _DummyChildProcess(
        pid=104,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
        timeout_on_wait=True,
    )
    different_script = _DummyChildProcess(
        pid=103,
        cmdline=["python", "src/backend/mcp_server/rag_mcp_stdio_server.py"],
    )
    _install_fake_psutil(
        monkeypatch,
        [],
        parent_children=[sibling_match, timeout_then_kill, different_script],
    )

    monkeypatch.setattr(process_guard.os, "getpid", lambda: 200)
    monkeypatch.setattr(process_guard.os, "getppid", lambda: 50)
    monkeypatch.setenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "1")

    result = process_guard.terminate_duplicate_sibling_servers(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    assert result["matched"] == 2
    assert result["terminated"] == 1
    assert result["killed"] == 1
    assert result["failed"] == 0
    assert sibling_match.terminated is True
    assert timeout_then_kill.killed is True
    assert different_script.terminated is False


def test_terminate_duplicate_sibling_servers_can_be_disabled(monkeypatch) -> None:
    proc = _DummyProcess(
        pid=101,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    _install_fake_psutil(monkeypatch, [proc])
    monkeypatch.setenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "0")

    result = process_guard.terminate_duplicate_sibling_servers(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    assert result["matched"] == 0
    assert proc.terminated is False


def test_terminate_duplicate_sibling_servers_defaults_to_disabled(monkeypatch) -> None:
    proc = _DummyProcess(
        pid=101,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    _install_fake_psutil(monkeypatch, [proc])
    monkeypatch.delenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", raising=False)

    result = process_guard.terminate_duplicate_sibling_servers(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    assert result == {"matched": 0, "terminated": 0, "killed": 0, "failed": 0}
    assert proc.terminated is False


def test_activate_mcp_helper_lifecycle_reclaims_older_same_owner_duplicate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    older_same_owner = _DummyProcess(
        pid=101,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    other_owner = _DummyProcess(
        pid=102,
        ppid=51,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    current_proc = _DummyProcess(
        pid=200,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    _install_fake_psutil(monkeypatch, [older_same_owner, other_owner, current_proc])

    registry_dir = tmp_path / "leases"
    registry_dir.mkdir()
    _write_lease(
        registry_dir / "101.json",
        helper_kind="mcp_stdio_server",
        pid=101,
        parent_pid=50,
        owner_token="owner-a",
        started_at_epoch_sec=10.0,
    )
    _write_lease(
        registry_dir / "102.json",
        helper_kind="mcp_stdio_server",
        pid=102,
        parent_pid=51,
        owner_token="owner-b",
        started_at_epoch_sec=11.0,
    )

    monkeypatch.setattr(process_guard.os, "getpid", lambda: 200)
    monkeypatch.setattr(process_guard.os, "getppid", lambda: 50)
    monkeypatch.setenv("VON_MCP_HELPER_REGISTRY_DIR", str(registry_dir))
    monkeypatch.setenv("VON_MCP_HELPER_OWNER_TOKEN", "owner-a")
    monkeypatch.setenv("VON_MCP_HELPER_HEARTBEAT_SEC", "0")
    monkeypatch.setenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "0")

    result = process_guard.activate_mcp_helper_lifecycle(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    harvest = result["harvest_report"]
    assert harvest["matched"] == 1
    assert harvest["terminated"] == 1
    assert harvest["killed"] == 0
    assert harvest["harvested_pids"] == [101]
    assert older_same_owner.terminated is True
    assert other_owner.terminated is False

    inventory = process_guard.get_mcp_helper_inventory()
    live_pids = sorted(helper["pid"] for helper in inventory["helpers"])
    assert 101 not in live_pids
    assert 102 in live_pids
    assert 200 in live_pids


def test_activate_mcp_helper_lifecycle_does_not_reclaim_parent_fallback_owner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    older_same_parent = _DummyProcess(
        pid=101,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    current_proc = _DummyProcess(
        pid=200,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    _install_fake_psutil(monkeypatch, [older_same_parent, current_proc])

    registry_dir = tmp_path / "leases"
    registry_dir.mkdir()
    _write_lease(
        registry_dir / "101.json",
        helper_kind="mcp_stdio_server",
        pid=101,
        parent_pid=50,
        owner_token="parent:50",
        owner_token_is_explicit=False,
        started_at_epoch_sec=10.0,
    )

    monkeypatch.setattr(process_guard.os, "getpid", lambda: 200)
    monkeypatch.setattr(process_guard.os, "getppid", lambda: 50)
    monkeypatch.setenv("VON_MCP_HELPER_REGISTRY_DIR", str(registry_dir))
    monkeypatch.delenv("VON_MCP_HELPER_OWNER_TOKEN", raising=False)
    monkeypatch.setenv("VON_MCP_HELPER_HEARTBEAT_SEC", "0")
    monkeypatch.setenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "0")

    result = process_guard.activate_mcp_helper_lifecycle(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    harvest = result["harvest_report"]
    assert harvest["matched"] == 0
    assert harvest["terminated"] == 0
    assert harvest["skipped"] == "owner_token_not_explicit"
    assert older_same_parent.terminated is False

    inventory = process_guard.get_mcp_helper_inventory()
    live_pids = sorted(helper["pid"] for helper in inventory["helpers"])
    assert live_pids == [101, 200]


def test_activate_mcp_helper_lifecycle_prunes_dead_lease_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current_proc = _DummyProcess(
        pid=200,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    _install_fake_psutil(monkeypatch, [current_proc])

    registry_dir = tmp_path / "leases"
    registry_dir.mkdir()
    _write_lease(
        registry_dir / "999.json",
        helper_kind="mcp_stdio_server",
        pid=999,
        parent_pid=77,
        owner_token="owner-old",
        started_at_epoch_sec=1.0,
    )

    monkeypatch.setattr(process_guard.os, "getpid", lambda: 200)
    monkeypatch.setattr(process_guard.os, "getppid", lambda: 50)
    monkeypatch.setenv("VON_MCP_HELPER_REGISTRY_DIR", str(registry_dir))
    monkeypatch.setenv("VON_MCP_HELPER_OWNER_TOKEN", "owner-a")
    monkeypatch.setenv("VON_MCP_HELPER_HEARTBEAT_SEC", "0")
    monkeypatch.setenv("VON_MCP_TERMINATE_DUPLICATE_SIBLINGS", "0")

    result = process_guard.activate_mcp_helper_lifecycle(
        "src/backend/mcp_server/mcp_stdio_server.py"
    )

    assert result["prune_report"]["removed_dead_files"] == 1
    assert result["prune_report"]["removed_dead_pids"] == [999]
    assert not (registry_dir / "999.json").exists()


def test_get_mcp_helper_inventory_reports_duplicate_groups(
    monkeypatch,
    tmp_path: Path,
) -> None:
    proc_a = _DummyProcess(
        pid=101,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    proc_b = _DummyProcess(
        pid=102,
        ppid=50,
        cmdline=["python", "src/backend/mcp_server/mcp_stdio_server.py"],
    )
    _install_fake_psutil(monkeypatch, [proc_a, proc_b])

    registry_dir = tmp_path / "leases"
    registry_dir.mkdir()
    _write_lease(
        registry_dir / "101.json",
        helper_kind="mcp_stdio_server",
        pid=101,
        parent_pid=50,
        owner_token="owner-a",
        started_at_epoch_sec=1.0,
    )
    _write_lease(
        registry_dir / "102.json",
        helper_kind="mcp_stdio_server",
        pid=102,
        parent_pid=50,
        owner_token="owner-a",
        started_at_epoch_sec=2.0,
    )

    monkeypatch.setenv("VON_MCP_HELPER_REGISTRY_DIR", str(registry_dir))

    inventory = process_guard.get_mcp_helper_inventory()

    assert inventory["helper_count"] == 2
    assert inventory["duplicate_group_count"] == 1
    duplicate_group = inventory["duplicate_groups"][0]
    assert duplicate_group["helper_kind"] == "mcp_stdio_server"
    assert duplicate_group["owner_token"] == "owner-a"
    assert duplicate_group["pids"] == [101, 102]


def test_rag_mcp_stdio_server_activates_helper_lifecycle_on_import(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        process_guard,
        "activate_mcp_helper_lifecycle",
        lambda script_path, log_fn=None: calls.append(str(script_path)) or {"ok": True},
    )

    import src.backend.mcp_server.rag_mcp_stdio_server as rag_mcp_stdio_server

    importlib.reload(rag_mcp_stdio_server)

    assert any(path.endswith("rag_mcp_stdio_server.py") for path in calls)
