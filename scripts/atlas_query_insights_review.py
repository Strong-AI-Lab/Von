"""Review Atlas Query Shape Insights reports and prepare Jira tuning tasks.

By default this command is dry-run only: it compares redacted Atlas reports and
prints candidate Jira payloads without creating issues. Pass both
``--apply-jira`` and ``--approved`` to use the shared deduplicated Jira gateway.
The review loop never creates or drops Mongo indexes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.services.atlas_query_insights_service import (
    AtlasQueryInsightsFilters,
    AtlasTypedBlocker,
    load_atlas_query_insights_config_from_env,
)
from src.backend.services.atlas_query_review_service import (
    AtlasQueryReviewThresholds,
    AtlasReviewOptions,
    AtlasReviewTypedBlocker,
    default_review_store_dir,
    format_review_summary,
    load_or_fetch_current_report,
    read_report_file,
    run_atlas_query_review,
    scheduled_review_enabled,
    write_report_file,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _comma_or_repeat(values: list[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    parsed: list[str] = []
    for value in values:
        parsed.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(parsed)


def _blocker_report(
    exc: Exception, blocker_type: str | None = None
) -> dict[str, object]:
    if isinstance(exc, AtlasReviewTypedBlocker):
        return exc.to_report()
    if isinstance(exc, AtlasTypedBlocker):
        report = exc.to_report()
        report["schema_version"] = "atlas_query_insights_review.v1"
        report["attribution_jira"] = "JVNAUTOSCI-2429"
        return report
    return {
        "schema_version": "atlas_query_insights_review.v1",
        "status": "blocked",
        "attribution_jira": "JVNAUTOSCI-2429",
        "parent_jira": "JVNAUTOSCI-2427",
        "atlas_diagnostics_jira": "JVNAUTOSCI-2428",
        "blocker": {
            "type": blocker_type or type(exc).__name__,
            "message": str(exc),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-report", help="Previous redacted Atlas report JSON")
    parser.add_argument("--current-report", help="Current redacted Atlas report JSON")
    parser.add_argument(
        "--fetch-current",
        action="store_true",
        help="Fetch current report via the Atlas diagnostics service when --current-report is omitted",
    )
    parser.add_argument(
        "--store-current-report", help="Path to persist the current report"
    )
    parser.add_argument(
        "--use-default-store",
        action="store_true",
        help="Use .von/atlas_query_reviews/current.json and previous.json",
    )
    parser.add_argument(
        "--schedule-enabled-only",
        action="store_true",
        help="Exit with typed blocker unless VON_ATLAS_QUERY_REVIEW_SCHEDULE_ENABLED is truthy",
    )
    parser.add_argument("--group-id", help="Atlas project/group id for --fetch-current")
    parser.add_argument("--cluster-name", help="Atlas cluster name for --fetch-current")
    parser.add_argument("--since", help="Optional Atlas report start time")
    parser.add_argument("--until", help="Optional Atlas report end time")
    parser.add_argument("--namespace", action="append")
    parser.add_argument("--command", action="append")
    parser.add_argument("--process-id", action="append")
    parser.add_argument("--max-results", type=_positive_int, default=100)
    parser.add_argument(
        "--include-suggested-indexes",
        action="store_true",
        help="Fetch optional Atlas Performance Advisor suggested-index summaries",
    )
    parser.add_argument(
        "--apply-jira",
        action="store_true",
        help="Apply generated Jira payloads through deduplicated Jira upsert",
    )
    parser.add_argument(
        "--approved",
        action="store_true",
        help="Required with --apply-jira to create/update Jira issues",
    )
    parser.add_argument("--project-key", default="JVNAUTOSCI")
    parser.add_argument("--issue-type", default="Task")
    parser.add_argument("--assignee-account-id")
    parser.add_argument("--request-id")
    parser.add_argument(
        "--high-total-ms",
        type=_positive_float,
        default=AtlasQueryReviewThresholds.high_total_execution_time_ms,
    )
    parser.add_argument(
        "--high-ratio",
        type=_positive_float,
        default=AtlasQueryReviewThresholds.high_examined_returned_ratio,
    )
    parser.add_argument(
        "--candidate-limit",
        type=_positive_int,
        default=AtlasQueryReviewThresholds.candidate_limit,
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON review")
    args = parser.parse_args(argv)

    try:
        if args.schedule_enabled_only and not scheduled_review_enabled():
            raise AtlasReviewTypedBlocker(
                "scheduled_review_disabled",
                "VON_ATLAS_QUERY_REVIEW_SCHEDULE_ENABLED is not truthy.",
            )

        previous_path = args.previous_report
        current_path = args.current_report
        if args.use_default_store:
            store_dir = default_review_store_dir()
            previous_path = previous_path or store_dir / "previous.json"
            current_path = current_path or store_dir / "current.json"

        previous_report = read_report_file(previous_path)
        current_report = None
        if current_path:
            current_report = read_report_file(current_path)
        if current_report is None:
            if not args.fetch_current:
                raise AtlasReviewTypedBlocker(
                    "current_report_required",
                    "Provide --current-report or pass --fetch-current.",
                )
            config = load_atlas_query_insights_config_from_env(
                group_id=args.group_id,
                cluster_name=args.cluster_name,
            )
            filters = AtlasQueryInsightsFilters(
                since=args.since,
                until=args.until,
                namespaces=_comma_or_repeat(args.namespace),
                commands=_comma_or_repeat(args.command),
                max_results=args.max_results,
                include_query_shapes=True,
                include_shape_text=True,
                include_suggested_indexes=args.include_suggested_indexes,
                process_ids=_comma_or_repeat(args.process_id),
            )
            current_report = load_or_fetch_current_report(
                config=config,
                filters=filters,
            )

        thresholds = AtlasQueryReviewThresholds(
            high_total_execution_time_ms=args.high_total_ms,
            high_examined_returned_ratio=args.high_ratio,
            candidate_limit=args.candidate_limit,
        )
        options = AtlasReviewOptions(
            project_key=args.project_key,
            issue_type=args.issue_type,
            dry_run=not (args.apply_jira and args.approved),
            approved=bool(args.approved),
            assignee_account_id=args.assignee_account_id,
            request_id=args.request_id,
            thresholds=thresholds,
        )
        review = run_atlas_query_review(
            previous_report=previous_report,
            current_report=current_report,
            options=options,
            apply_jira=args.apply_jira,
        )
        if args.store_current_report:
            write_report_file(args.store_current_report, current_report)
    except Exception as exc:
        report = _blocker_report(exc)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 2

    if args.json:
        print(json.dumps(review, indent=2, sort_keys=True, default=str))
    else:
        print(format_review_summary(review))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
