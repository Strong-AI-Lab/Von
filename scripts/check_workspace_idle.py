from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TELEMETRY_PATH_ENV = "VON_WORKSPACE_IDLE_TELEMETRY_PATH"
_TELEMETRY_DISABLED_ENV = "VON_WORKSPACE_IDLE_TELEMETRY_DISABLED"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Print YES when no active coding-agent-style work appears to be "
            "running in this workspace, otherwise print NO."
        )
    )
    parser.add_argument(
        "--workspace",
        default=str(ROOT),
        help="Workspace root to inspect. Defaults to the repository root.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable assessment details instead of only YES/NO.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="With text output, include blocker details after the YES/NO line.",
    )
    parser.add_argument(
        "--include-services",
        action="store_true",
        help=(
            "Treat known Von server/indexing service processes as blockers. "
            "By default they are ignored as background services."
        ),
    )
    parser.add_argument(
        "--include-agent-helpers",
        action="store_true",
        help=(
            "Treat long-lived MCP/Playwright/node helper daemons as blockers. "
            "By default they are ignored because presence alone does not prove "
            "active work."
        ),
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always exit 0; useful when callers only want to parse stdout.",
    )
    parser.add_argument(
        "--recent-seconds",
        type=float,
        default=300.0,
        help=(
            "Treat Git metadata activity or changed/untracked file mtimes within "
            "this many seconds as not idle. Default: 300."
        ),
    )
    parser.add_argument(
        "--ignore-recent-repo-activity",
        action="store_true",
        help="Only assess running processes; ignore recent file and Git activity.",
    )
    parser.add_argument(
        "--full-process-scan",
        action="store_true",
        help=(
            "After the fast process snapshot finds no blockers, run the slower "
            "psutil process scan for extra certainty. Disabled by default so "
            "automation pre-flight checks stay quick."
        ),
    )
    parser.add_argument(
        "--max-command-chars",
        type=int,
        default=220,
        help="Maximum command length shown in verbose text output.",
    )
    parser.add_argument(
        "--idle-telemetry-path",
        default=None,
        help=(
            "Append idle decision telemetry as JSONL to this file. Defaults to "
            f"{_TELEMETRY_PATH_ENV}, then automation storage, with a temp "
            "directory fallback if the default location is write-blocked."
        ),
    )
    parser.add_argument(
        "--no-idle-telemetry",
        action="store_true",
        help="Disable non-fatal idle decision telemetry writes.",
    )
    return parser


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truthy_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _automation_telemetry_path(base: Path) -> Path:
    return base / "automations" / "workspace-idle" / "telemetry.jsonl"


def _default_telemetry_paths() -> list[Path]:
    codex_home = os.environ.get("CODEX_HOME")
    primary = (
        _automation_telemetry_path(Path(codex_home).expanduser())
        if codex_home
        else _automation_telemetry_path(Path.home() / ".codex")
    )
    temp_fallback = (
        Path(tempfile.gettempdir()) / "von-workspace-idle" / "telemetry.jsonl"
    )
    return [primary, temp_fallback]


def _telemetry_candidate_paths(args: argparse.Namespace) -> list[Path]:
    if args.no_idle_telemetry or _truthy_env(os.environ.get(_TELEMETRY_DISABLED_ENV)):
        return []
    configured = args.idle_telemetry_path or os.environ.get(_TELEMETRY_PATH_ENV)
    if configured:
        return [Path(configured).expanduser()]
    candidates: list[Path] = []
    seen: set[str] = set()
    for path in _default_telemetry_paths():
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)
    return candidates


def _decision_reason(assessment, *, error: str | None = None) -> str:
    if error:
        return "error"
    if assessment is None:
        return "unknown"
    if assessment.recent_repo_activity:
        return "recent_repo_activity"
    if assessment.blockers:
        return "process_blockers"
    if assessment.idle:
        return "idle"
    return "not_idle"


def _dump_telemetry_failure_to_stderr(
    *,
    payload: dict[str, object],
    failures: list[dict[str, str]],
) -> None:
    print(
        "workspace idle telemetry write failed; dumping telemetry to stderr",
        file=sys.stderr,
    )
    print(
        json.dumps(
            {
                "telemetry_write_failures": failures,
                "telemetry": payload,
            },
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _record_idle_telemetry(
    args: argparse.Namespace,
    *,
    workspace: str,
    started_at: float,
    exit_code: int,
    assessment=None,
    error: str | None = None,
) -> None:
    """Best-effort JSONL decision telemetry; never affects idle results."""

    try:
        paths = _telemetry_candidate_paths(args)
        if not paths:
            return
        completed_at = time.time()
        answer = assessment.answer if assessment is not None else "NO"
        payload = {
            "schema_version": 1,
            "recorded_at": _utc_now_iso(),
            "workspace_root": workspace,
            "answer": answer,
            "idle": answer == "YES",
            "exit_code": exit_code,
            "decision_reason": _decision_reason(assessment, error=error),
            "duration_ms": round((completed_at - started_at) * 1000, 3),
            "options": {
                "json": bool(args.json),
                "verbose": bool(args.verbose),
                "no_fail": bool(args.no_fail),
                "include_services": bool(args.include_services),
                "include_agent_helpers": bool(args.include_agent_helpers),
                "ignore_recent_repo_activity": bool(args.ignore_recent_repo_activity),
                "full_process_scan": bool(args.full_process_scan),
                "recent_seconds": float(args.recent_seconds),
            },
        }
        if assessment is not None:
            payload["assessment"] = assessment.to_json_dict()
        if error:
            payload["error"] = error

        encoded_payload = json.dumps(payload, sort_keys=True)
        failures: list[dict[str, str]] = []
        for path in paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(encoded_payload)
                    handle.write("\n")
                return
            except Exception as exc:
                failures.append(
                    {
                        "path": str(path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
        _dump_telemetry_failure_to_stderr(payload=payload, failures=failures)
    except Exception as exc:
        print(
            "workspace idle telemetry construction failed; no telemetry file was written",
            file=sys.stderr,
        )
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return


def _trim(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 3)] + "..."


def _print_assessment(assessment, *, json_output: bool, verbose: bool, max_command_chars: int) -> None:
    if json_output:
        print(json.dumps(assessment.to_json_dict(), indent=2, sort_keys=True))
        return

    print(assessment.answer)
    if not verbose:
        return

    if assessment.blockers:
        print("Blockers:")
        for blocker in assessment.blockers:
            age = (
                f", age={blocker.age_seconds:.1f}s"
                if blocker.age_seconds is not None
                else ""
            )
            print(
                f"- pid={blocker.pid}, name={blocker.name}, "
                f"reason={blocker.reason}{age}: "
                f"{_trim(blocker.command, max_command_chars)}"
            )
    if assessment.recent_repo_activity:
        print("Recent repo activity:")
        for activity in assessment.recent_repo_activity:
            age = (
                f", age={activity.age_seconds:.1f}s"
                if activity.age_seconds is not None
                else ""
            )
            status = f", status={activity.status}" if activity.status else ""
            print(
                f"- kind={activity.kind}{status}{age}: "
                f"{activity.path} ({activity.reason})"
            )
    elif not assessment.blockers:
        print("No active workspace commands or recent repo activity found.")
    print(
        "Ignored: "
        f"services={assessment.ignored_services}, "
        f"agent_helpers={assessment.ignored_agent_helpers}, "
        f"interactive_shells={assessment.ignored_interactive_shells}"
    )


def main(argv: list[str] | None = None) -> int:
    from src.backend.utilities.workspace_idle import (
        assess_workspace_idle,
        current_lineage_pids,
        detect_recent_repo_activity,
        iter_fast_local_processes,
        iter_local_processes,
    )

    parser = _build_parser()
    args = parser.parse_args(argv)
    started_at = time.time()
    workspace = str(Path(args.workspace).resolve())

    try:
        recent_activity = (
            ()
            if args.ignore_recent_repo_activity
            else detect_recent_repo_activity(
                workspace,
                recent_seconds=float(args.recent_seconds),
            )
        )
        if recent_activity:
            assessment = assess_workspace_idle(
                [],
                workspace_root=workspace,
                exclude_pids=current_lineage_pids(),
                include_services=bool(args.include_services),
                include_agent_helpers=bool(args.include_agent_helpers),
                recent_repo_activity=recent_activity,
                recent_window_seconds=(
                    None
                    if args.ignore_recent_repo_activity
                    else float(args.recent_seconds)
                ),
                now=time.time(),
            )
            _print_assessment(
                assessment,
                json_output=bool(args.json),
                verbose=bool(args.verbose),
                max_command_chars=int(args.max_command_chars),
            )
            exit_code = 0 if args.no_fail else 1
            _record_idle_telemetry(
                args,
                workspace=workspace,
                started_at=started_at,
                exit_code=exit_code,
                assessment=assessment,
            )
            return exit_code

        process_now = time.time()
        fast_processes = iter_fast_local_processes()
        assessment = assess_workspace_idle(
            fast_processes,
            workspace_root=workspace,
            exclude_pids=current_lineage_pids(),
            include_services=bool(args.include_services),
            include_agent_helpers=bool(args.include_agent_helpers),
            recent_repo_activity=recent_activity,
            recent_window_seconds=(
                None
                if args.ignore_recent_repo_activity
                else float(args.recent_seconds)
            ),
            now=process_now,
        )
        if assessment.blockers:
            _print_assessment(
                assessment,
                json_output=bool(args.json),
                verbose=bool(args.verbose),
                max_command_chars=int(args.max_command_chars),
            )
            exit_code = 0 if args.no_fail else 1
            _record_idle_telemetry(
                args,
                workspace=workspace,
                started_at=started_at,
                exit_code=exit_code,
                assessment=assessment,
            )
            return exit_code

        if not fast_processes and os.name == "nt" and not args.full_process_scan:
            raise RuntimeError("bounded Windows process snapshot returned no rows")

        if args.full_process_scan:
            try:
                full_processes = iter_local_processes()
            except RuntimeError:
                if fast_processes:
                    _print_assessment(
                        assessment,
                        json_output=bool(args.json),
                        verbose=bool(args.verbose),
                        max_command_chars=int(args.max_command_chars),
                    )
                    exit_code = 0 if args.no_fail or assessment.idle else 1
                    _record_idle_telemetry(
                        args,
                        workspace=workspace,
                        started_at=started_at,
                        exit_code=exit_code,
                        assessment=assessment,
                    )
                    return exit_code
                raise

            assessment = assess_workspace_idle(
                full_processes,
                workspace_root=workspace,
                exclude_pids=current_lineage_pids(),
                include_services=bool(args.include_services),
                include_agent_helpers=bool(args.include_agent_helpers),
                recent_repo_activity=recent_activity,
                recent_window_seconds=(
                    None
                    if args.ignore_recent_repo_activity
                    else float(args.recent_seconds)
                ),
                now=time.time(),
            )
    except Exception as exc:
        # Fail closed: if the host process list cannot be inspected, do not
        # claim the workspace is idle.
        if args.json:
            print(
                json.dumps(
                    {
                        "idle": False,
                        "answer": "NO",
                        "workspace_root": workspace,
                        "error": str(exc),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print("NO")
            if not args.no_fail:
                print(f"workspace idle check failed: {exc}", file=sys.stderr)
        exit_code = 0 if args.no_fail else 2
        _record_idle_telemetry(
            args,
            workspace=workspace,
            started_at=started_at,
            exit_code=exit_code,
            error=str(exc),
        )
        return exit_code

    _print_assessment(
        assessment,
        json_output=bool(args.json),
        verbose=bool(args.verbose),
        max_command_chars=int(args.max_command_chars),
    )

    if args.no_fail or assessment.idle:
        exit_code = 0
    else:
        exit_code = 1
    _record_idle_telemetry(
        args,
        workspace=workspace,
        started_at=started_at,
        exit_code=exit_code,
        assessment=assessment,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
