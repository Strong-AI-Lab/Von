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


def _install_fake_psutil(monkeypatch, processes: list[_DummyProcess]) -> None:
    fake_psutil = types.SimpleNamespace(
        process_iter=lambda _attrs: processes,
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

