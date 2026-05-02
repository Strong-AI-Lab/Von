"""Best-effort workspace idle assessment for local coding-agent coordination."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
from io import StringIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Iterable, Mapping, Sequence


_SERVICE_FRAGMENTS: tuple[str, ...] = (
    "src/backend/utilities/concept_index_worker.py",
    "src/backend/utilities/rag_indexing_worker.py",
    "src/workflows/von/main.py",
)

_AGENT_HELPER_FRAGMENTS: tuple[str, ...] = (
    "@playwright/mcp",
    "mcp_stdio_server.py",
    "node_repl.exe",
    "playwright-mcp",
    "rag_mcp_stdio_server.py",
    "vontology_mcp_stdio_server.py",
)

_ACTIVE_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpdm(?:\.exe)?\b",
        r"\bpytest(?:\.exe)?\b",
        r"\bpyright(?:\.exe)?\b",
        r"\bmypy(?:\.exe)?\b",
        r"\bruff(?:\.exe)?\b",
        r"\bblack(?:\.exe)?\b",
        r"\beslint(?:\.cmd|\.exe)?\b",
        r"\bjest(?:\.cmd|\.exe)?\b",
        r"\bvitest(?:\.cmd|\.exe)?\b",
        r"\bnpm(?:\.cmd|\.exe)?\s+run\b",
        r"\bnpx(?:\.cmd|\.exe)?\s+(?!@playwright/mcp\b)",
        r"\bplaywright(?:\.cmd|\.exe)?\s+test\b",
        r"\bprettier(?:\.cmd|\.exe)?\b",
        r"\btsc(?:\.cmd|\.exe)?\b",
        r"\bgit(?:\.exe)?\b",
        r"\brun\.ps1\b",
        r"\bscripts[/\\][^\s\"']+\.(?:py|ps1|js|cjs|ts)\b",
        r"\btests[/\\][^\s\"']+\.(?:py|js|ts)\b",
    )
)

_INTERACTIVE_SHELL_NAMES: set[str] = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "bash",
    "bash.exe",
    "zsh",
    "sh",
    "sh.exe",
}

_EXCLUDED_PIDS_ENV = "VON_WORKSPACE_IDLE_EXCLUDE_PIDS"
_WINDOWS_CIM_PROCESS_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    name: str
    cmdline: tuple[str, ...]
    cwd: str | None = None
    create_time: float | None = None


@dataclass(frozen=True)
class ProcessAssessment:
    pid: int
    name: str
    reason: str
    command: str
    age_seconds: float | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class RepoActivityAssessment:
    kind: str
    path: str
    reason: str
    age_seconds: float | None = None
    status: str | None = None


@dataclass(frozen=True)
class WorkspaceIdleAssessment:
    idle: bool
    answer: str
    workspace_root: str
    blockers: tuple[ProcessAssessment, ...]
    recent_repo_activity: tuple[RepoActivityAssessment, ...]
    recent_window_seconds: float | None
    ignored_services: int
    ignored_agent_helpers: int
    ignored_interactive_shells: int
    scanned_processes: int
    workspace_processes: int

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blockers"] = [asdict(item) for item in self.blockers]
        payload["recent_repo_activity"] = [
            asdict(item) for item in self.recent_repo_activity
        ]
        return payload


def _normalise_fragment(value: str) -> str:
    return os.path.normcase(value).replace("\\", "/").strip().lower()


def _cmdline_text(cmdline: Sequence[str]) -> str:
    return " ".join(str(part) for part in cmdline if part is not None)


def _command_contains_any(command: str, fragments: Sequence[str]) -> bool:
    normalised = _normalise_fragment(command)
    return any(_normalise_fragment(fragment) in normalised for fragment in fragments)


def _parse_pid_set(raw_value: str | None) -> set[int]:
    pids: set[int] = set()
    for raw_part in re.split(r"[,\s;]+", str(raw_value or "")):
        part = raw_part.strip()
        if not part:
            continue
        try:
            pid = int(part)
        except ValueError:
            continue
        if pid > 0:
            pids.add(pid)
    return pids


def configured_excluded_pids(env: Mapping[str, str] | None = None) -> set[int]:
    """Return wrapper-supplied process IDs that should not count as blockers."""

    source = os.environ if env is None else env
    return _parse_pid_set(source.get(_EXCLUDED_PIDS_ENV))


def _is_workspace_process(process: ProcessSnapshot, workspace_root: str) -> bool:
    workspace = _normalise_fragment(str(Path(workspace_root).resolve()))
    command = _normalise_fragment(_cmdline_text(process.cmdline))
    if workspace and workspace in command:
        return True

    if process.cwd:
        cwd = _normalise_fragment(process.cwd)
        if cwd == workspace or cwd.startswith(f"{workspace}/"):
            return True

    return False


def _has_active_command_marker(process: ProcessSnapshot) -> bool:
    command = _cmdline_text(process.cmdline)
    return any(pattern.search(command) for pattern in _ACTIVE_COMMAND_PATTERNS)


def _is_interactive_shell(process: ProcessSnapshot) -> bool:
    name = process.name.strip().lower()
    if name not in _INTERACTIVE_SHELL_NAMES:
        return False
    return not _has_active_command_marker(process)


def _is_bare_workspace_python_interpreter_path(
    process: ProcessSnapshot, workspace_root: str
) -> bool:
    """Return True for limited snapshots that only expose the venv Python path."""

    if process.name.strip().lower() not in {"python.exe", "python3.exe"}:
        return False
    command = _cmdline_text(process.cmdline).strip().strip('"')
    if not command:
        return False
    workspace = _normalise_fragment(str(Path(workspace_root).resolve()))
    normalised_command = _normalise_fragment(command).strip('"')
    return normalised_command in {
        f"{workspace}/.venv/scripts/python.exe",
        f"{workspace}/.venv/scripts/python3.exe",
    }


def _process_age_seconds(process: ProcessSnapshot, now: float | None) -> float | None:
    if now is None or process.create_time is None:
        return None
    try:
        return max(0.0, float(now) - float(process.create_time))
    except Exception:
        return None


def assess_workspace_idle(
    processes: Iterable[ProcessSnapshot],
    *,
    workspace_root: str,
    exclude_pids: set[int] | None = None,
    include_services: bool = False,
    include_agent_helpers: bool = False,
    recent_repo_activity: Iterable[RepoActivityAssessment] | None = None,
    recent_window_seconds: float | None = None,
    now: float | None = None,
) -> WorkspaceIdleAssessment:
    """Return a conservative YES/NO idle assessment for a workspace.

    Long-lived local services and agent helper daemons are ignored by default
    because their presence alone does not mean a coding agent is doing active
    work in the checkout. Test, lint, build, git, PDM, and workspace script
    processes count as blockers.
    """

    excluded = set(exclude_pids or set())
    blockers: list[ProcessAssessment] = []
    ignored_services = 0
    ignored_agent_helpers = 0
    ignored_interactive_shells = 0
    scanned = 0
    workspace_count = 0

    for process in processes:
        scanned += 1
        if process.pid in excluded:
            continue
        if not _is_workspace_process(process, workspace_root):
            continue
        workspace_count += 1

        command = _cmdline_text(process.cmdline)
        if _command_contains_any(command, _SERVICE_FRAGMENTS):
            if not include_services:
                ignored_services += 1
                continue
            reason = "workspace service process"
        elif _command_contains_any(command, _AGENT_HELPER_FRAGMENTS):
            if not include_agent_helpers:
                ignored_agent_helpers += 1
                continue
            reason = "agent helper process"
        elif _is_interactive_shell(process):
            ignored_interactive_shells += 1
            continue
        elif _is_bare_workspace_python_interpreter_path(process, workspace_root):
            ignored_agent_helpers += 1
            continue
        elif _has_active_command_marker(process):
            reason = "active workspace command"
        else:
            # A non-shell process with the workspace in its command line is more
            # likely active work than an idle terminal; classify conservatively.
            reason = "workspace-referencing process"

        blockers.append(
            ProcessAssessment(
                pid=process.pid,
                name=process.name,
                reason=reason,
                command=command,
                age_seconds=_process_age_seconds(process, now),
                cwd=process.cwd,
            )
        )

    repo_activity = tuple(recent_repo_activity or ())
    is_idle = not blockers and not repo_activity
    return WorkspaceIdleAssessment(
        idle=is_idle,
        answer="YES" if is_idle else "NO",
        workspace_root=str(Path(workspace_root).resolve()),
        blockers=tuple(blockers),
        recent_repo_activity=repo_activity,
        recent_window_seconds=recent_window_seconds,
        ignored_services=ignored_services,
        ignored_agent_helpers=ignored_agent_helpers,
        ignored_interactive_shells=ignored_interactive_shells,
        scanned_processes=scanned,
        workspace_processes=workspace_count,
    )


def _file_age_seconds(path: Path, now: float) -> float | None:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def _is_recent(path: Path, *, now: float, recent_seconds: float) -> float | None:
    age = _file_age_seconds(path, now)
    if age is None or age > recent_seconds:
        return None
    return age


def _resolve_git_dir(workspace_root: Path) -> Path | None:
    dot_git = workspace_root / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None

    try:
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.lower().startswith(prefix):
        return None
    raw_git_dir = text[len(prefix) :].strip()
    candidate = Path(raw_git_dir)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return candidate.resolve()


def _iter_git_metadata_paths(workspace_root: Path) -> Iterable[Path]:
    git_dir = _resolve_git_dir(workspace_root)
    if git_dir is None:
        return ()

    paths: list[Path] = [
        git_dir / "index",
        git_dir / "HEAD",
        git_dir / "FETCH_HEAD",
        git_dir / "ORIG_HEAD",
        git_dir / "MERGE_HEAD",
        git_dir / "CHERRY_PICK_HEAD",
        git_dir / "REBASE_HEAD",
        git_dir / "COMMIT_EDITMSG",
        git_dir / "logs" / "HEAD",
    ]
    try:
        head_text = (git_dir / "HEAD").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        head_text = ""
    if head_text.startswith("ref:"):
        ref_path = head_text.split(":", 1)[1].strip()
        if ref_path:
            paths.append(git_dir / "logs" / ref_path)
    return paths


def _parse_git_status_porcelain_z(raw_output: bytes | str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain=v1 -z`` into ``(status, path)`` rows."""

    if isinstance(raw_output, bytes):
        text = raw_output.decode("utf-8", errors="replace")
    else:
        text = str(raw_output or "")
    chunks = [chunk for chunk in text.split("\0") if chunk]
    rows: list[tuple[str, str]] = []
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        if len(chunk) < 4:
            index += 1
            continue
        status = chunk[:2]
        path = chunk[3:]
        if path:
            rows.append((status, path))
        index += 1
        # Rename/copy records include the other path as an extra NUL-delimited
        # field. The first path is enough for recency checks, so skip the second.
        if ("R" in status or "C" in status) and index < len(chunks):
            index += 1
    return rows


def _run_git_status_porcelain(workspace_root: Path, *, timeout_seconds: float) -> bytes:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(workspace_root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        check=False,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=max(0.1, timeout_seconds),
    )
    if completed.returncode != 0:
        return b""
    return completed.stdout or b""


def detect_recent_repo_activity(
    workspace_root: str,
    *,
    now: float | None = None,
    recent_seconds: float = 300.0,
    include_git_metadata: bool = True,
    include_status_paths: bool = True,
    git_timeout_seconds: float = 3.0,
) -> tuple[RepoActivityAssessment, ...]:
    """Detect recent Git metadata or changed-file mtimes without walking the tree."""

    if recent_seconds <= 0:
        return ()
    resolved_now = time.time() if now is None else float(now)
    workspace = Path(workspace_root).resolve()
    activities: list[RepoActivityAssessment] = []
    seen: set[tuple[str, str]] = set()

    def _record(
        *,
        kind: str,
        path: Path,
        reason: str,
        age_seconds: float | None,
        status: str | None = None,
    ) -> None:
        try:
            display_path = str(path.resolve().relative_to(workspace))
        except Exception:
            display_path = str(path)
        key = (kind, display_path)
        if key in seen:
            return
        seen.add(key)
        activities.append(
            RepoActivityAssessment(
                kind=kind,
                path=display_path,
                reason=reason,
                age_seconds=age_seconds,
                status=status,
            )
        )

    if include_git_metadata:
        for path in _iter_git_metadata_paths(workspace):
            if not path.exists():
                continue
            age = _is_recent(path, now=resolved_now, recent_seconds=recent_seconds)
            if age is None:
                continue
            _record(
                kind="git_metadata",
                path=path,
                reason="recent Git metadata activity",
                age_seconds=age,
            )

    if include_status_paths:
        try:
            raw_status = _run_git_status_porcelain(
                workspace,
                timeout_seconds=git_timeout_seconds,
            )
        except Exception:
            raw_status = b""
        for status, raw_path in _parse_git_status_porcelain_z(raw_status):
            candidate = (workspace / raw_path).resolve()
            age = _is_recent(
                candidate,
                now=resolved_now,
                recent_seconds=recent_seconds,
            )
            if age is None:
                continue
            _record(
                kind="working_tree_path",
                path=candidate,
                reason="recent changed or untracked workspace path",
                age_seconds=age,
                status=status.strip() or None,
            )

    return tuple(
        sorted(
            activities,
            key=lambda item: (
                item.age_seconds if item.age_seconds is not None else float("inf"),
                item.kind,
                item.path,
            ),
        )
    )


def _parse_windows_process_csv(raw_output: str) -> list[ProcessSnapshot]:
    rows: list[ProcessSnapshot] = []
    reader = csv.DictReader(StringIO(raw_output or ""))
    for row in reader:
        try:
            pid = int(row.get("ProcessId") or 0)
        except Exception:
            continue
        if pid <= 0:
            continue
        command = str(row.get("CommandLine") or "")
        cmdline = (command,) if command else ()
        create_time: float | None = None
        try:
            raw_create_time = str(row.get("CreateTimeUnix") or "").strip()
            if raw_create_time:
                create_time = float(raw_create_time)
        except Exception:
            create_time = None
        rows.append(
            ProcessSnapshot(
                pid=pid,
                name=str(row.get("Name") or ""),
                cmdline=cmdline,
                create_time=create_time,
            )
        )
    return rows


def iter_windows_cim_processes(
    *, timeout_seconds: float = _WINDOWS_CIM_PROCESS_TIMEOUT_SECONDS
) -> list[ProcessSnapshot]:
    """Fast Windows process snapshot using scalar CIM fields.

    psutil command-line enumeration can be very slow on process-heavy Windows
    desktops. This path uses PowerShell only to read bounded scalar fields and
    returns an empty list when unavailable.
    """

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        return []

    command = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine,"
        "@{Name='CreateTimeUnix';Expression={"
        "if ($_.CreationDate) { "
        "try { ([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds() } "
        "catch { '' } "
        "} else { '' }"
        "}} | ConvertTo-Csv -NoTypeInformation"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.5, timeout_seconds),
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    return _parse_windows_process_csv(completed.stdout or "")


def iter_windows_wmi_processes(
    *, timeout_seconds: float = _WINDOWS_CIM_PROCESS_TIMEOUT_SECONDS
) -> list[ProcessSnapshot]:
    """Fallback Windows process snapshot using legacy WMI.

    Some sandboxed PowerShell contexts return no rows from ``Get-CimInstance``
    even though legacy WMI still exposes the same bounded scalar process fields.
    Keep this separate from the psutil fallback because psutil command-line
    enumeration has hung on process-heavy Windows hosts.
    """

    powershell = shutil.which("powershell.exe")
    if not powershell:
        return []

    command = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "Get-WmiObject Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine,"
        "@{Name='CreateTimeUnix';Expression={"
        "if ($_.CreationDate) { "
        "try { "
        "([DateTimeOffset]"
        "[Management.ManagementDateTimeConverter]::ToDateTime($_.CreationDate)"
        ").ToUnixTimeSeconds() "
        "} catch { '' } "
        "} else { '' }"
        "}} | ConvertTo-Csv -NoTypeInformation"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.5, timeout_seconds),
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    return _parse_windows_process_csv(completed.stdout or "")


def iter_windows_get_processes(
    *, timeout_seconds: float = _WINDOWS_CIM_PROCESS_TIMEOUT_SECONDS
) -> list[ProcessSnapshot]:
    """Last-resort bounded Windows process snapshot using ``Get-Process``.

    This source does not expose full command lines, but it is available in more
    restricted PowerShell contexts than CIM/WMI. It avoids reading slow process
    properties for every process, and only tries executable paths for direct
    tool process names where the executable name alone is meaningful. A bare
    workspace-local Python/Node/shell executable path is not enough to prove
    active work because long-lived Von services use the same interpreter path.
    """

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        return []

    command = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "$toolNames = @("
        "'git','pdm','pytest','ruff','npm','npx','pyright','mypy','black','eslint','jest',"
        "'vitest','playwright','prettier','tsc'"
        "); "
        "Get-Process | ForEach-Object { "
        "$processName = if ($_.ProcessName) { [string]$_.ProcessName } else { '' }; "
        "$name = if ($processName) { \"$processName.exe\" } else { '' }; "
        "$commandLine = ''; "
        "if ($toolNames -contains $processName.ToLowerInvariant()) { "
        "try { $commandLine = [string]$_.Path } catch { $commandLine = '' } "
        "} "
        "[pscustomobject]@{"
        "ProcessId=$_.Id;"
        "Name=$name;"
        "CommandLine=$commandLine;"
        "CreateTimeUnix=''"
        "} "
        "} | ConvertTo-Csv -NoTypeInformation"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.5, timeout_seconds),
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    return _parse_windows_process_csv(completed.stdout or "")


def iter_fast_local_processes(
    *, timeout_seconds: float = _WINDOWS_CIM_PROCESS_TIMEOUT_SECONDS
) -> list[ProcessSnapshot]:
    """Return a fast local process snapshot where the host supports one."""

    if os.name == "nt":
        rows = iter_windows_cim_processes(timeout_seconds=timeout_seconds)
        if rows:
            return rows
        rows = iter_windows_wmi_processes(timeout_seconds=timeout_seconds)
        if rows:
            return rows
        return iter_windows_get_processes(timeout_seconds=timeout_seconds)
    return iter_local_processes(include_cwd=False)


def current_lineage_pids() -> set[int]:
    """Return current process and parent PIDs so the checker does not count itself."""

    pids = {os.getpid(), *configured_excluded_pids()}
    try:
        import psutil

        process = psutil.Process()
        for parent in process.parents():
            try:
                pids.add(int(parent.pid))
            except Exception:
                continue
    except Exception:
        pass
    return pids


def iter_local_processes(*, include_cwd: bool = False) -> list[ProcessSnapshot]:
    """Snapshot local processes with best-effort access-denied handling."""

    try:
        import psutil
    except Exception as exc:  # pragma: no cover - dependency is present in repo env
        raise RuntimeError("psutil is required for workspace idle checks") from exc

    rows: list[ProcessSnapshot] = []
    try:
        attrs = ["pid", "name", "cmdline", "create_time"]
        if include_cwd:
            attrs.append("cwd")
        iterator = psutil.process_iter(attrs)
    except Exception as exc:  # pragma: no cover - psutil defensive path
        raise RuntimeError(f"failed to enumerate processes: {exc}") from exc

    for proc in iterator:
        try:
            info: Mapping[str, Any] = getattr(proc, "info", {}) or {}
            pid = int(info.get("pid") or getattr(proc, "pid", 0) or 0)
            if pid <= 0:
                continue
            raw_cmdline = info.get("cmdline")
            if isinstance(raw_cmdline, Iterable) and not isinstance(
                raw_cmdline, (str, bytes)
            ):
                cmdline = tuple(str(part) for part in raw_cmdline)
            else:
                cmdline = ()
            rows.append(
                ProcessSnapshot(
                    pid=pid,
                    name=str(info.get("name") or ""),
                    cmdline=cmdline,
                    cwd=str(info["cwd"]) if include_cwd and info.get("cwd") else None,
                    create_time=(
                        float(info["create_time"])
                        if info.get("create_time") is not None
                        else None
                    ),
                )
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        except Exception:
            continue
    return rows
