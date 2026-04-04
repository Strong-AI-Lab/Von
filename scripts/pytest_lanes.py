from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
repo_root_str = str(REPO_ROOT)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)

_pytest_lane_catalogue = importlib.import_module(
    "src.backend.utils.pytest_lane_catalogue"
)
SUITES = _pytest_lane_catalogue.SUITES
aggregate_lane_ids = _pytest_lane_catalogue.aggregate_lane_ids
build_pytest_command_for_suite = _pytest_lane_catalogue.build_pytest_command_for_suite
build_pytest_command_for_targets = (
    _pytest_lane_catalogue.build_pytest_command_for_targets
)
count_suite_members = _pytest_lane_catalogue.count_suite_members
get_git_changed_paths = _pytest_lane_catalogue.get_git_changed_paths
recommend_for_changed_paths = _pytest_lane_catalogue.recommend_for_changed_paths
render_command = _pytest_lane_catalogue.render_command


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "list":
        _print_lane_list()
        return 0

    if args.command == "recommend":
        changed_paths = _resolve_changed_paths(args)
        recommendation = recommend_for_changed_paths(
            repo_root=REPO_ROOT,
            changed_paths=changed_paths,
            risk=args.risk,
        )
        _print_recommendation(recommendation, args.risk)
        return 0

    if args.command == "aggregate-plan":
        _print_aggregate_plan(include_manual=args.include_manual)
        return 0

    if args.command == "run-lane":
        command = build_pytest_command_for_suite(args.suite_id, extra_args=_clean_extra_args(args.extra_args))
        return _run_command(command)

    if args.command == "run-targets":
        command = build_pytest_command_for_targets(args.targets, extra_args=_clean_extra_args(args.extra_args))
        return _run_command(command)

    parser.error(f"Unsupported command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic pytest lane planner and runner for Von."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List lane and overlay suites with current file counts.")

    recommend_parser = subparsers.add_parser(
        "recommend",
        help="Recommend targeted pytest commands from explicit paths or a git diff.",
    )
    recommend_group = recommend_parser.add_mutually_exclusive_group(required=True)
    recommend_group.add_argument(
        "--changed",
        nargs="+",
        help="Repo-relative changed paths to analyse.",
    )
    recommend_group.add_argument(
        "--git-diff",
        nargs="?",
        const="origin/main",
        metavar="BASE_REF",
        help="Use `git diff BASE_REF...HEAD` to determine changed paths (default: origin/main).",
    )
    recommend_parser.add_argument(
        "--risk",
        choices=("smoke", "normal", "high"),
        default="normal",
        help="Recommendation breadth. `smoke` stays direct; `high` adds broader overlays.",
    )

    aggregate_parser = subparsers.add_parser(
        "aggregate-plan",
        help="Print the shardable aggregate full-coverage lane sequence.",
    )
    aggregate_parser.add_argument(
        "--include-manual",
        action="store_true",
        help="Include the manual-only suite at the end of the plan.",
    )

    run_lane_parser = subparsers.add_parser(
        "run-lane",
        help="Run one suite by its stable lane or overlay identifier.",
    )
    run_lane_parser.add_argument("suite_id", choices=tuple(SUITES))
    run_lane_parser.add_argument(
        "extra_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest arguments after `--`, for example `-- --maxfail=1 --durations=20`.",
    )

    run_targets_parser = subparsers.add_parser(
        "run-targets",
        help="Run an explicit targeted set of pytest files with the same DB safety defaults as the lane runner.",
    )
    run_targets_parser.add_argument("targets", nargs="+")
    run_targets_parser.add_argument(
        "extra_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest arguments after `--`, for example `-- --maxfail=1`.",
    )

    return parser


def _print_lane_list() -> None:
    counts = count_suite_members(REPO_ROOT)
    print("Pytest suites")
    print("-------------")
    for suite_id, definition in sorted(
        SUITES.items(),
        key=lambda item: (
            item[1].aggregate_order if item[1].aggregate_order is not None else 10_000,
            item[0],
        ),
    ):
        members = counts.get(suite_id, 0)
        aggregate_label = "aggregate" if definition.aggregate_order is not None else "overlay"
        automation_label = "automated" if definition.automated else "manual"
        print(f"{suite_id}")
        print(
            f"  kind={definition.kind} scope={aggregate_label} automation={automation_label} "
            f"cost={definition.cost_profile} files={members}"
        )
        print(f"  marker={definition.marker_expression}")
        print(f"  {definition.description}")


def _print_recommendation(recommendation, risk: str) -> None:
    print(f"Risk profile: {risk}")
    print()

    print("Changed paths")
    print("-------------")
    if recommendation.changed_paths:
        for path in recommendation.changed_paths:
            print(path)
    else:
        print("(none)")

    print()
    print("Direct targets")
    print("--------------")
    if recommendation.direct_test_targets:
        print(
            render_command(
                build_pytest_command_for_targets(recommendation.direct_test_targets)
            )
        )
        for target in recommendation.direct_test_targets:
            print(f"  {target}")
    else:
        print("(no direct pytest files inferred)")

    print()
    print("Primary lanes")
    print("-------------")
    if recommendation.primary_suites:
        for suite_id in recommendation.primary_suites:
            definition = SUITES[suite_id]
            print(f"{suite_id}: {definition.description}")
            print(f"  {render_command(build_pytest_command_for_suite(suite_id))}")
    else:
        print("(none)")

    print()
    print("Escalation overlays")
    print("-------------------")
    if recommendation.overlay_suites:
        for suite_id in recommendation.overlay_suites:
            definition = SUITES[suite_id]
            print(f"{suite_id}: {definition.description}")
            print(f"  {render_command(build_pytest_command_for_suite(suite_id))}")
    else:
        print("(none)")

    if recommendation.notes:
        print()
        print("Notes")
        print("-----")
        for note in recommendation.notes:
            print(note)

    print()
    print("Aggregate coverage plan")
    print("----------------------")
    _print_aggregate_plan(include_manual=False)


def _print_aggregate_plan(include_manual: bool) -> None:
    suite_ids = aggregate_lane_ids(include_manual=include_manual)
    for index, suite_id in enumerate(suite_ids, start=1):
        print(f"{index}. {suite_id}")
        print(f"   {render_command(build_pytest_command_for_suite(suite_id))}")
    if not include_manual:
        print("Manual suite omitted by default:")
        print(f"  {render_command(build_pytest_command_for_suite('manual'))}")


def _resolve_changed_paths(args: argparse.Namespace) -> list[str]:
    if args.changed:
        return list(args.changed)
    return get_git_changed_paths(REPO_ROOT, base_ref=args.git_diff)


def _prepare_pytest_env() -> dict[str, str]:
    env = os.environ.copy()
    db_name = env.get("VON_DB_NAME", "").strip()
    if not db_name:
        env["VON_DB_NAME"] = "test_von_db"
    elif db_name == "von_db":
        raise RuntimeError(
            "Refusing to run pytest with VON_DB_NAME=von_db. "
            "Set VON_DB_NAME=test_von_db or unset it before running lanes."
        )
    return env


def _run_command(command: list[str]) -> int:
    print(render_command(command))
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_prepare_pytest_env(),
        check=False,
    )
    return completed.returncode


def _clean_extra_args(extra_args: list[str]) -> list[str]:
    if extra_args and extra_args[0] == "--":
        return extra_args[1:]
    return extra_args


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
