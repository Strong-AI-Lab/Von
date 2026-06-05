"""Build a redacted Mongo query-targeting diagnostics report.

The report consumes MongoDB ``system.profile`` entries and optionally runs
bounded ``explain("executionStats")`` for sampled read-only find shapes. It
prints structural query metadata and execution counters only: no Mongo URI,
credentials, query values, update values, or document bodies.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pymongo.errors import OperationFailure

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.db.mongo_client import get_configured_database_name, get_db
from src.backend.services.mongo_query_diagnostics_service import (
    build_profiler_query_targeting_report,
    safe_mongo_diagnostic_error,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _print_table(report: Mapping[str, Any]) -> None:
    rows = report.get("rows")
    if not isinstance(rows, list):
        return
    headers = (
        "rank",
        "namespace",
        "cmd",
        "count",
        "docs/ret",
        "keys/ret",
        "max_ms",
        "filter_shape",
        "sort_shape",
        "plan/index",
    )
    print("\t".join(headers))
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            continue
        plan = row.get("last_winning_index") or row.get("last_plan_summary") or ""
        values = (
            index,
            row.get("namespace") or f"{row.get('database')}.{row.get('collection')}",
            row.get("command_name") or "",
            row.get("count") or 0,
            row.get("max_docs_examined_per_returned")
            or row.get("docs_examined_per_returned")
            or "",
            row.get("max_keys_examined_per_returned")
            or row.get("keys_examined_per_returned")
            or "",
            row.get("max_duration_ms") or "",
            row.get("filter_shape") or "",
            row.get("sort_shape") or "",
            plan,
        )
        print("\t".join(str(value) for value in values))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--namespace", help="Optional Mongo namespace, e.g. von_db.workflow_instances"
    )
    parser.add_argument("--min-millis", type=_non_negative_int, default=0)
    parser.add_argument("--sample-limit", type=_positive_int, default=200)
    parser.add_argument("--report-limit", type=_positive_int, default=20)
    parser.add_argument("--explain-samples", type=_non_negative_int, default=0)
    parser.add_argument("--explain-max-time-ms", type=_positive_int, default=2000)
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    args = parser.parse_args(argv)

    db = get_db()
    if db is None:
        print(
            json.dumps(
                {
                    "schema_version": "mongo_query_targeting_report.v1",
                    "status": "mongo_unavailable",
                    "database": get_configured_database_name(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    try:
        report = build_profiler_query_targeting_report(
            db,
            namespace=args.namespace,
            min_millis=args.min_millis,
            sample_limit=args.sample_limit,
            report_limit=args.report_limit,
            explain_samples=args.explain_samples,
            explain_max_time_ms=args.explain_max_time_ms,
        )
    except OperationFailure as exc:
        safe_error = safe_mongo_diagnostic_error(exc)
        report = {
            "schema_version": "mongo_query_targeting_report.v1",
            "status": "profiler_unavailable",
            "database": get_configured_database_name(),
            "error_type": safe_error.get("error_type"),
            "error_code": safe_error.get("code_name") or safe_error.get("code"),
            "error": safe_error.get("message"),
            "recommended_next_step": (
                "Enable MongoDB profiler, use Atlas Query Insights, or run with "
                "Atlas Performance Advisor exports for this database."
            ),
        }
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 3

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
