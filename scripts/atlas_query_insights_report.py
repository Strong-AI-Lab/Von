"""Build a redacted Atlas Query Shape Insights diagnostics report.

This command reads Atlas Admin API telemetry only. It never prints Atlas
credentials, Mongo URI credentials, ``.env`` contents, query literal values, or
document bodies, and it never creates or drops indexes.
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
    assert_report_has_no_known_secrets,
    build_atlas_query_insights_report,
    format_atlas_query_insights_table,
    load_atlas_query_insights_config_from_env,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-id", help="Atlas project/group id")
    parser.add_argument("--cluster-name", help="Atlas cluster name")
    parser.add_argument("--since", help="Optional ISO-8601 start time")
    parser.add_argument("--until", help="Optional ISO-8601 end time")
    parser.add_argument(
        "--namespace",
        action="append",
        help="Mongo namespace filter; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--command",
        action="append",
        help="Mongo command filter; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--query-shape-hash",
        action="append",
        help="Query shape hash filter; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--series",
        action="append",
        help="Atlas series value; may be repeated or comma-separated",
    )
    parser.add_argument("--max-results", type=_positive_int, default=100)
    parser.add_argument(
        "--include-query-shapes",
        action="store_true",
        help="Fetch query shape metadata from the Atlas queryShapes endpoint",
    )
    parser.add_argument(
        "--include-shape-text",
        action="store_true",
        help="Include redacted query shape text when queryShapes is authorised",
    )
    parser.add_argument(
        "--include-suggested-indexes",
        action="store_true",
        help="Fetch Performance Advisor suggested-index summaries for process ids",
    )
    parser.add_argument(
        "--process-id",
        action="append",
        help="Atlas process id for optional Performance Advisor endpoints",
    )
    parser.add_argument(
        "--include-slow-query-logs",
        action="store_true",
        help=(
            "Record slow-query-log endpoint intent in the report. Slow log bodies "
            "are not included by this redacted command."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    args = parser.parse_args(argv)

    try:
        config = load_atlas_query_insights_config_from_env(
            group_id=args.group_id,
            cluster_name=args.cluster_name,
        )
        filters = AtlasQueryInsightsFilters(
            since=args.since,
            until=args.until,
            namespaces=_comma_or_repeat(args.namespace),
            commands=_comma_or_repeat(args.command),
            query_shape_hashes=_comma_or_repeat(args.query_shape_hash),
            series=_comma_or_repeat(args.series) or AtlasQueryInsightsFilters().series,
            max_results=args.max_results,
            include_query_shapes=args.include_query_shapes or args.include_shape_text,
            include_shape_text=args.include_shape_text,
            include_suggested_indexes=args.include_suggested_indexes,
            process_ids=_comma_or_repeat(args.process_id),
            include_slow_query_logs=args.include_slow_query_logs,
        )
        report = build_atlas_query_insights_report(config, filters)
        assert_report_has_no_known_secrets(report)
    except AtlasTypedBlocker as exc:
        report = exc.to_report()
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 2
    except RuntimeError as exc:
        report = {
            "schema_version": "atlas_query_insights_report.v1",
            "status": "blocked",
            "attribution_jira": "JVNAUTOSCI-2428",
            "related_jira": "JVNAUTOSCI-2427",
            "blocker": {
                "type": "report_secret_redaction_failed",
                "message": str(exc),
            },
        }
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 3

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(format_atlas_query_insights_table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
