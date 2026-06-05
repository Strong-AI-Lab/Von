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

from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import OperationFailure, PyMongoError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backend.db.mongo_client import get_configured_database_name, get_db
from src.backend.services.mongo_observability_service import (
    build_mongo_command_shape,
    build_mongo_query_targeting_report_from_rows,
    profiler_document_to_query_shape_row,
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


def _profile_query(*, namespace: str | None, min_millis: int) -> dict[str, Any]:
    query: dict[str, Any] = {"millis": {"$gte": min_millis}}
    if namespace:
        query["ns"] = namespace
    else:
        query["ns"] = {"$not": {"$regex": r"\.system\."}}
    return query


def fetch_profiler_documents(
    db: Database,
    *,
    namespace: str | None,
    min_millis: int,
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Fetch a bounded set of profiler documents.

    The caller must not print these raw documents: they may contain query
    values in ``command``. Downstream conversion keeps only redacted shapes.
    """

    cursor = (
        db["system.profile"]
        .find(_profile_query(namespace=namespace, min_millis=min_millis))
        .sort("ts", DESCENDING)
        .limit(sample_limit)
    )
    return [dict(doc) for doc in cursor]


def _find_stage(
    plan: Mapping[str, Any] | None, stage_name: str
) -> Mapping[str, Any] | None:
    if not isinstance(plan, Mapping):
        return None
    if plan.get("stage") == stage_name:
        return plan
    for key in ("inputStage", "inputStages", "shards"):
        value = plan.get(key)
        if isinstance(value, Mapping):
            found = _find_stage(value, stage_name)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value:
                found = _find_stage(item, stage_name)
                if found is not None:
                    return found
    return None


def _winning_index_from_explain(explain: Mapping[str, Any]) -> str | None:
    planner = explain.get("queryPlanner")
    if not isinstance(planner, Mapping):
        return None
    plan = planner.get("winningPlan")
    ixscan = _find_stage(plan if isinstance(plan, Mapping) else None, "IXSCAN")
    if ixscan is None:
        return None
    index_name = ixscan.get("indexName")
    return str(index_name) if index_name else None


def _explain_find_profile_document(
    db: Database,
    doc: Mapping[str, Any],
    *,
    max_time_ms: int,
) -> dict[str, Any] | None:
    command = doc.get("command")
    if not isinstance(command, Mapping) or "find" not in command:
        return None
    collection = command.get("find")
    if not isinstance(collection, str) or not collection.strip():
        return None

    find_command: dict[str, Any] = {"find": collection}
    for key in ("filter", "sort", "projection", "limit", "skip", "hint", "collation"):
        if key in command:
            find_command[key] = command[key]
    if max_time_ms > 0:
        find_command["maxTimeMS"] = max_time_ms

    explain = db.command(
        {
            "explain": find_command,
            "verbosity": "executionStats",
        }
    )
    if not isinstance(explain, Mapping):
        return None

    stats = explain.get("executionStats")
    if not isinstance(stats, Mapping):
        return None
    shape = build_mongo_command_shape("find", command)
    ns = str(doc.get("ns") or "")
    database = ""
    coll_name = collection
    if "." in ns:
        database, coll_name = ns.split(".", 1)
    row = {
        "database": database,
        "collection": coll_name,
        "namespace": ns,
        "command_name": "find",
        "filter_shape": shape.get("filter_shape") or "",
        "sort_shape": shape.get("sort_shape") or "",
        "projection_shape": shape.get("projection_shape") or "",
        "update_shape": "",
        "pipeline_shape": "",
        "limit": shape.get("limit"),
        "count": 1,
        "total_duration_ms": float(stats.get("executionTimeMillis") or 0.0),
        "max_duration_ms": float(stats.get("executionTimeMillis") or 0.0),
        "total_returned": int(stats.get("nReturned") or 0),
        "max_n_returned": int(stats.get("nReturned") or 0),
        "total_docs_examined": int(stats.get("totalDocsExamined") or 0),
        "total_keys_examined": int(stats.get("totalKeysExamined") or 0),
        "max_docs_examined": int(stats.get("totalDocsExamined") or 0),
        "max_keys_examined": int(stats.get("totalKeysExamined") or 0),
        "last_winning_index": _winning_index_from_explain(explain),
        "last_in_memory_sort": _find_stage(
            (
                explain.get("queryPlanner", {}).get("winningPlan")
                if isinstance(explain.get("queryPlanner"), Mapping)
                else None
            ),
            "SORT",
        )
        is not None,
        "sample_request_ids": [],
        "sources": ["explain.executionStats"],
    }
    return row


def build_profiler_query_targeting_report(
    db: Database,
    *,
    namespace: str | None,
    min_millis: int,
    sample_limit: int,
    report_limit: int,
    explain_samples: int,
    explain_max_time_ms: int,
) -> dict[str, Any]:
    docs = fetch_profiler_documents(
        db,
        namespace=namespace,
        min_millis=min_millis,
        sample_limit=sample_limit,
    )
    rows = [
        row
        for row in (profiler_document_to_query_shape_row(doc) for doc in docs)
        if row is not None
    ]
    explain_errors: list[str] = []
    if explain_samples > 0:
        explained = 0
        for doc in docs:
            if explained >= explain_samples:
                break
            try:
                row = _explain_find_profile_document(
                    db,
                    doc,
                    max_time_ms=explain_max_time_ms,
                )
            except (OperationFailure, PyMongoError, RuntimeError) as exc:
                explain_errors.append(f"{type(exc).__name__}:{exc}")
                continue
            if row is not None:
                rows.append(row)
                explained += 1

    report = build_mongo_query_targeting_report_from_rows(
        rows,
        limit=report_limit,
        source="system.profile",
    )
    report["database"] = get_configured_database_name()
    report["profile_sample_count"] = len(docs)
    report["profile_filter"] = {
        "namespace": namespace,
        "min_millis": min_millis,
        "sample_limit": sample_limit,
    }
    report["explain"] = {
        "requested_samples": explain_samples,
        "max_time_ms": explain_max_time_ms,
        "error_count": len(explain_errors),
        "errors": explain_errors[:5],
    }
    return report


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
        report = {
            "schema_version": "mongo_query_targeting_report.v1",
            "status": "profiler_unavailable",
            "database": get_configured_database_name(),
            "error_type": type(exc).__name__,
            "error": str(exc),
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
