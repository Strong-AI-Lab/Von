from __future__ import annotations

import sys
import types

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

