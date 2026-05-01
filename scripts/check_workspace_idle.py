from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    return parser


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
            return 0 if args.no_fail else 1

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
            return 0 if args.no_fail else 1

        if not fast_processes and os.name == "nt" and not args.full_process_scan:
            raise RuntimeError("fast Windows process snapshot returned no rows")

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
                    if args.no_fail or assessment.idle:
                        return 0
                    return 1
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
            print(f"workspace idle check failed: {exc}", file=sys.stderr)
        return 0 if args.no_fail else 2

    _print_assessment(
        assessment,
        json_output=bool(args.json),
        verbose=bool(args.verbose),
        max_command_chars=int(args.max_command_chars),
    )

    if args.no_fail or assessment.idle:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
