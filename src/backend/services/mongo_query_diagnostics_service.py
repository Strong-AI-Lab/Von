"""Von-internal Mongo query-targeting diagnostic support.

This module turns the JVNAUTOSCI-2427 report builders into a reusable service
surface for internal MCP/workflow invocation. It is read-only and redacted: it
never returns raw profiler documents, query values, update values, document
bodies, Mongo URIs, or credentials.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import OperationFailure, PyMongoError

from src.backend.db.mongo_client import get_configured_database_name, get_db
from src.backend.services.mongo_observability_service import (
    build_mongo_command_shape,
    build_mongo_query_targeting_report_from_rows,
    get_mongo_query_targeting_report,
    profiler_document_to_query_shape_row,
    record_mongo_query_stats_snapshot,
)

_JIRA_KEY = "JVNAUTOSCI-2450"
_PARENT_JIRA_KEY = "JVNAUTOSCI-2427"

_MAX_MIN_MILLIS = 3_600_000
_MAX_SAMPLE_LIMIT = 500
_MAX_REPORT_LIMIT = 50
_MAX_EXPLAIN_SAMPLES = 5
_MAX_EXPLAIN_TIME_MS = 5_000
_QUERY_STATS_MAX_TIME_MS = 5_000
_QUERY_SHAPE_HASH_PATTERN = re.compile(r"^[A-Fa-f0-9]{16,128}$")


@dataclass(frozen=True)
class MongoQueryDiagnosticsOptions:
    source: str = "combined"
    namespace: str | None = None
    min_millis: int = 0
    sample_limit: int = 200
    report_limit: int = 20
    explain_samples: int = 0
    explain_max_time_ms: int = 2_000
    reset_in_process: bool = False


def _clean_optional_str(value: Any, *, max_len: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def safe_mongo_diagnostic_error(exc: BaseException) -> dict[str, Any]:
    """Return bounded DB error metadata without embedding command bodies."""

    payload: dict[str, Any] = {"error_type": type(exc).__name__}
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        payload["code"] = code
    details = getattr(exc, "details", None)
    if isinstance(details, Mapping):
        code_name = details.get("codeName")
        if isinstance(code_name, str) and code_name.strip():
            payload["code_name"] = code_name.strip()[:96]
        errmsg = details.get("errmsg")
        if isinstance(errmsg, str) and any(
            marker in errmsg.lower() for marker in ("not authorized", "not authorised")
        ):
            payload["message"] = "not authorised for requested Mongo diagnostic read"
    payload.setdefault("message", "Mongo diagnostic read failed")
    return payload


def normalise_mongo_query_diagnostics_options(
    *,
    source: Any = None,
    namespace: Any = None,
    min_millis: Any = None,
    sample_limit: Any = None,
    report_limit: Any = None,
    explain_samples: Any = None,
    explain_max_time_ms: Any = None,
    reset_in_process: Any = None,
) -> tuple[MongoQueryDiagnosticsOptions, dict[str, Any]]:
    """Normalise caller options and return bound diagnostics for telemetry."""

    raw_source = _clean_optional_str(source, max_len=64) or "combined"
    source_value = raw_source.lower().replace("-", "_")
    if source_value not in {"combined", "in_process", "profiler", "query_stats"}:
        source_value = "combined"

    options = MongoQueryDiagnosticsOptions(
        source=source_value,
        namespace=_clean_optional_str(namespace),
        min_millis=_bounded_int(
            min_millis,
            default=0,
            minimum=0,
            maximum=_MAX_MIN_MILLIS,
        ),
        sample_limit=_bounded_int(
            sample_limit,
            default=200,
            minimum=1,
            maximum=_MAX_SAMPLE_LIMIT,
        ),
        report_limit=_bounded_int(
            report_limit,
            default=20,
            minimum=1,
            maximum=_MAX_REPORT_LIMIT,
        ),
        explain_samples=_bounded_int(
            explain_samples,
            default=0,
            minimum=0,
            maximum=_MAX_EXPLAIN_SAMPLES,
        ),
        explain_max_time_ms=_bounded_int(
            explain_max_time_ms,
            default=2_000,
            minimum=1,
            maximum=_MAX_EXPLAIN_TIME_MS,
        ),
        reset_in_process=bool(reset_in_process),
    )
    bounds = {
        "source": {
            "requested": source,
            "effective": options.source,
            "allowed": ["combined", "in_process", "profiler", "query_stats"],
        },
        "namespace": {
            "requested": namespace,
            "effective": options.namespace,
        },
        "min_millis": {
            "requested": min_millis,
            "effective": options.min_millis,
            "max": _MAX_MIN_MILLIS,
        },
        "sample_limit": {
            "requested": sample_limit,
            "effective": options.sample_limit,
            "max": _MAX_SAMPLE_LIMIT,
        },
        "report_limit": {
            "requested": report_limit,
            "effective": options.report_limit,
            "max": _MAX_REPORT_LIMIT,
        },
        "explain_samples": {
            "requested": explain_samples,
            "effective": options.explain_samples,
            "max": _MAX_EXPLAIN_SAMPLES,
        },
        "explain_max_time_ms": {
            "requested": explain_max_time_ms,
            "effective": options.explain_max_time_ms,
            "max": _MAX_EXPLAIN_TIME_MS,
        },
        "reset_in_process": {
            "requested": reset_in_process,
            "effective": options.reset_in_process,
        },
    }
    return options, bounds


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
    """Fetch a bounded profiler sample.

    Raw documents may contain command values and must stay inside this module.
    Callers receive only rows from ``profiler_document_to_query_shape_row``.
    """

    cursor = (
        db["system.profile"]
        .find(_profile_query(namespace=namespace, min_millis=min_millis))
        .sort("ts", DESCENDING)
        .limit(sample_limit)
    )
    return [dict(doc) for doc in cursor]


def _query_stats_namespace_match(
    *, database_name: str, namespace: str | None
) -> dict[str, Any]:
    match: dict[str, Any] = {"key.queryShape.cmdNs.db": database_name}
    if not namespace:
        return match
    if "." in namespace:
        requested_database, collection = namespace.split(".", 1)
        if requested_database != database_name:
            raise ValueError("Mongo namespace is outside the configured database")
    else:
        collection = namespace
    collection = collection.strip()
    if not collection:
        raise ValueError("Mongo namespace does not include a collection")
    match["key.queryShape.cmdNs.coll"] = collection
    return match


def _query_stats_pipeline(
    *, database_name: str, namespace: str | None, sample_limit: int
) -> list[dict[str, Any]]:
    """Build a bounded pipeline that returns only approved diagnostic fields."""

    match = _query_stats_namespace_match(
        database_name=database_name,
        namespace=namespace,
    )
    return [
        {"$queryStats": {}},
        {"$match": match},
        {
            "$group": {
                "_id": "$queryShapeHash",
                "query_shape": {"$first": "$key.queryShape"},
                "exec_count": {"$sum": {"$ifNull": ["$metrics.execCount", 0]}},
                "total_duration_micros": {
                    "$sum": {"$ifNull": ["$metrics.totalExecMicros.sum", 0]}
                },
                "max_duration_micros": {
                    "$max": {"$ifNull": ["$metrics.totalExecMicros.max", 0]}
                },
                "total_returned": {
                    "$sum": {"$ifNull": ["$metrics.docsReturned.sum", 0]}
                },
                "max_returned": {"$max": {"$ifNull": ["$metrics.docsReturned.max", 0]}},
                "total_docs_examined": {
                    "$sum": {"$ifNull": ["$metrics.docsExamined.sum", 0]}
                },
                "max_docs_examined": {
                    "$max": {"$ifNull": ["$metrics.docsExamined.max", 0]}
                },
                "total_keys_examined": {
                    "$sum": {"$ifNull": ["$metrics.keysExamined.sum", 0]}
                },
                "max_keys_examined": {
                    "$max": {"$ifNull": ["$metrics.keysExamined.max", 0]}
                },
                "has_sort_stage_count": {
                    "$sum": {"$ifNull": ["$metrics.hasSortStage.true", 0]}
                },
                "used_disk_count": {"$sum": {"$ifNull": ["$metrics.usedDisk.true", 0]}},
                "first_seen_at": {"$min": "$metrics.firstSeenTimestamp"},
                "latest_seen_at": {"$max": "$metrics.latestSeenTimestamp"},
            }
        },
        {
            "$set": {
                "targeting_ratio": {
                    "$max": [
                        {
                            "$divide": [
                                "$total_docs_examined",
                                {"$max": ["$total_returned", 1]},
                            ]
                        },
                        {
                            "$divide": [
                                "$total_keys_examined",
                                {"$max": ["$total_returned", 1]},
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$sort": {
                "targeting_ratio": -1,
                "total_duration_micros": -1,
                "exec_count": -1,
            }
        },
        {"$limit": sample_limit + 1},
        {
            "$project": {
                "_id": 0,
                "query_shape_hash": "$_id",
                "query_shape": 1,
                "exec_count": 1,
                "total_duration_micros": 1,
                "max_duration_micros": 1,
                "total_returned": 1,
                "max_returned": 1,
                "total_docs_examined": 1,
                "max_docs_examined": 1,
                "total_keys_examined": 1,
                "max_keys_examined": 1,
                "has_sort_stage_count": 1,
                "used_disk_count": 1,
                "first_seen_at": 1,
                "latest_seen_at": 1,
            }
        },
    ]


def fetch_query_stats_documents(
    db: Database,
    *,
    namespace: str | None,
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Read a bounded, projected ``$queryStats`` sample from the admin DB."""

    database_name = str(getattr(db, "name", "") or get_configured_database_name())
    pipeline = _query_stats_pipeline(
        database_name=database_name,
        namespace=namespace,
        sample_limit=sample_limit,
    )
    cursor = db.client["admin"].aggregate(
        pipeline,
        maxTimeMS=_QUERY_STATS_MAX_TIME_MS,
    )
    return [dict(doc) for doc in cursor]


def _safe_nonnegative_int(value: Any) -> int:
    try:
        if hasattr(value, "to_decimal"):
            value = value.to_decimal()
        return max(0, int(value or 0))
    except (TypeError, ValueError, ArithmeticError):
        return 0


def _safe_query_shape_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    if not _QUERY_SHAPE_HASH_PATTERN.fullmatch(text):
        return None
    return text.upper()


def _iso_utc(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def query_stats_document_to_query_shape_row(
    doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Convert one projected ``$queryStats`` result to a redacted shape row."""

    query_shape = doc.get("query_shape")
    if not isinstance(query_shape, Mapping):
        return None
    cmd_ns = query_shape.get("cmdNs")
    if not isinstance(cmd_ns, Mapping):
        return None
    database = _clean_optional_str(cmd_ns.get("db"), max_len=96) or ""
    collection = _clean_optional_str(cmd_ns.get("coll"), max_len=96) or ""
    command_name = _clean_optional_str(query_shape.get("command"), max_len=64)
    if not command_name:
        return None

    synthetic_command: dict[str, Any] = {command_name: collection}
    for field in (
        "filter",
        "query",
        "sort",
        "projection",
        "fields",
        "pipeline",
        "limit",
        "skip",
        "hint",
        "collation",
    ):
        if field in query_shape:
            synthetic_command[field] = query_shape[field]
    shape = build_mongo_command_shape(command_name, synthetic_command)
    query_hash = _safe_query_shape_hash(doc.get("query_shape_hash"))
    total_duration_micros = _safe_nonnegative_int(doc.get("total_duration_micros"))
    max_duration_micros = _safe_nonnegative_int(doc.get("max_duration_micros"))
    row: dict[str, Any] = {
        "database": database,
        "collection": collection,
        "namespace": f"{database}.{collection}" if database and collection else "",
        "command_name": command_name,
        "filter_shape": shape.get("filter_shape") or "",
        "sort_shape": shape.get("sort_shape") or "",
        "projection_shape": shape.get("projection_shape") or "",
        "update_shape": shape.get("update_shape") or "",
        "pipeline_shape": shape.get("pipeline_shape") or "",
        "limit": shape.get("limit"),
        "count": _safe_nonnegative_int(doc.get("exec_count")),
        "total_duration_ms": total_duration_micros / 1000.0,
        "max_duration_ms": max_duration_micros / 1000.0,
        "total_returned": _safe_nonnegative_int(doc.get("total_returned")),
        "max_n_returned": _safe_nonnegative_int(doc.get("max_returned")),
        "total_docs_examined": _safe_nonnegative_int(doc.get("total_docs_examined")),
        "max_docs_examined": _safe_nonnegative_int(doc.get("max_docs_examined")),
        "total_keys_examined": _safe_nonnegative_int(doc.get("total_keys_examined")),
        "max_keys_examined": _safe_nonnegative_int(doc.get("max_keys_examined")),
        "last_in_memory_sort": _safe_nonnegative_int(doc.get("has_sort_stage_count"))
        > 0,
        "last_used_disk": _safe_nonnegative_int(doc.get("used_disk_count")) > 0,
        "sample_request_ids": [query_hash] if query_hash else [],
        "sources": ["$queryStats"],
    }
    if query_hash:
        row["query_hash"] = query_hash
        row["_query_shape_identity"] = query_hash
    return row


def build_query_stats_targeting_report(
    db: Database,
    *,
    namespace: str | None,
    sample_limit: int,
    report_limit: int,
) -> dict[str, Any]:
    docs = fetch_query_stats_documents(
        db,
        namespace=namespace,
        sample_limit=sample_limit,
    )
    truncated = len(docs) > sample_limit
    selected_docs = docs[:sample_limit]
    rows = [
        row
        for row in (
            query_stats_document_to_query_shape_row(doc) for doc in selected_docs
        )
        if row is not None
    ]
    report = build_mongo_query_targeting_report_from_rows(
        rows,
        limit=report_limit,
        source="$queryStats",
    )
    first_seen = [
        value
        for value in (_iso_utc(doc.get("first_seen_at")) for doc in selected_docs)
        if value
    ]
    latest_seen = [
        value
        for value in (_iso_utc(doc.get("latest_seen_at")) for doc in selected_docs)
        if value
    ]
    database_name = str(getattr(db, "name", "") or get_configured_database_name())
    report.update(
        {
            "status": "ok",
            "database": database_name,
            "selected_shape_count": len(rows),
            "query_stats_filter": {
                "namespace": namespace,
                "sample_limit": sample_limit,
                "max_time_ms": _QUERY_STATS_MAX_TIME_MS,
            },
            "observation_scope": {
                "kind": "current_mongodb_member_cumulative_query_stats",
                "counter_semantics": (
                    "cumulative_since_each_shape_first_seen_or_member_restart"
                ),
                "atlas_time_window_equivalent": False,
                "freshness_scope": "selected_server_ranked_shapes",
            },
            "first_seen_at_utc": min(first_seen) if first_seen else None,
            "latest_seen_at_utc": max(latest_seen) if latest_seen else None,
            "collected_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "truncated": truncated,
            "recommended_next_step": (
                "Compare persistent high-ratio query hashes with Atlas Query "
                "Insights over a bounded time window before changing indexes."
            ),
        }
    )
    return report


def _find_stage(
    plan: Mapping[str, Any] | None,
    stage_name: str,
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

    explain = db.command({"explain": find_command, "verbosity": "executionStats"})
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
    winning_plan = (
        explain.get("queryPlanner", {}).get("winningPlan")
        if isinstance(explain.get("queryPlanner"), Mapping)
        else None
    )
    return {
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
            winning_plan if isinstance(winning_plan, Mapping) else None,
            "SORT",
        )
        is not None,
        "sample_request_ids": [],
        "sources": ["explain.executionStats"],
    }


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
                safe_error = safe_mongo_diagnostic_error(exc)
                explain_errors.append(
                    ":".join(
                        str(part)
                        for part in (
                            safe_error.get("error_type"),
                            safe_error.get("code_name") or safe_error.get("code"),
                            safe_error.get("message"),
                        )
                        if part
                    )
                )
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


def build_von_mongo_query_diagnostics_report(
    *,
    allow_operator_diagnostics: bool = False,
    source: Any = None,
    namespace: Any = None,
    min_millis: Any = None,
    sample_limit: Any = None,
    report_limit: Any = None,
    explain_samples: Any = None,
    explain_max_time_ms: Any = None,
    reset_in_process: Any = None,
) -> dict[str, Any]:
    """Return a bounded report for Von-internal conversation/workflow use."""

    options, bounds = normalise_mongo_query_diagnostics_options(
        source=source,
        namespace=namespace,
        min_millis=min_millis,
        sample_limit=sample_limit,
        report_limit=report_limit,
        explain_samples=explain_samples,
        explain_max_time_ms=explain_max_time_ms,
        reset_in_process=reset_in_process,
    )
    base: dict[str, Any] = {
        "schema_version": "von_mongo_query_diagnostics.v1",
        "success": False,
        "status": "blocked",
        "attribution_jira": _JIRA_KEY,
        "parent_jira": _PARENT_JIRA_KEY,
        "mode": "read_only_redacted_diagnostics",
        "direct_index_mutation": False,
        "parameter_bounds": bounds,
        "privacy": {
            "redacted": True,
            "omits": [
                "mongo_uri",
                "credentials",
                ".env",
                "query_values",
                "update_values",
                "document_bodies",
                "returned_documents",
            ],
        },
    }
    if not allow_operator_diagnostics:
        return {
            **base,
            "error_code": "operator_diagnostics_not_allowed",
            "error": (
                "Mongo query diagnostics require allow_operator_diagnostics=true "
                "from an authenticated/operator or trusted local workflow path."
            ),
            "recommended_next_step": (
                "Retry only when the user/operator explicitly requested DB diagnostics."
            ),
        }

    reports: dict[str, Any] = {}
    if options.source in {"combined", "in_process"}:
        reports["in_process"] = get_mongo_query_targeting_report(
            limit=options.report_limit,
            reset=options.reset_in_process,
        )

    db = None
    if options.source in {"combined", "profiler", "query_stats"}:
        db = get_db()

    if options.source in {"combined", "profiler"}:
        if db is None:
            reports["profiler"] = {
                "schema_version": "mongo_query_targeting_report.v1",
                "status": "mongo_unavailable",
                "database": get_configured_database_name(),
                "rows": [],
            }
        else:
            try:
                reports["profiler"] = build_profiler_query_targeting_report(
                    db,
                    namespace=options.namespace,
                    min_millis=options.min_millis,
                    sample_limit=options.sample_limit,
                    report_limit=options.report_limit,
                    explain_samples=options.explain_samples,
                    explain_max_time_ms=options.explain_max_time_ms,
                )
            except OperationFailure as exc:
                safe_error = safe_mongo_diagnostic_error(exc)
                reports["profiler"] = {
                    "schema_version": "mongo_query_targeting_report.v1",
                    "status": "profiler_unavailable",
                    "database": get_configured_database_name(),
                    "error_type": safe_error.get("error_type"),
                    "error_code": safe_error.get("code_name") or safe_error.get("code"),
                    "error": safe_error.get("message"),
                    "rows": [],
                    "recommended_next_step": (
                        "Enable MongoDB profiler, use Atlas Query Insights, or run "
                        "with Atlas Performance Advisor exports for this database."
                    ),
                }

    if options.source in {"combined", "query_stats"}:
        if db is None:
            reports["query_stats"] = {
                "schema_version": "mongo_query_targeting_report.v1",
                "source": "$queryStats",
                "status": "mongo_unavailable",
                "database": get_configured_database_name(),
                "rows": [],
            }
        else:
            try:
                reports["query_stats"] = build_query_stats_targeting_report(
                    db,
                    namespace=options.namespace,
                    sample_limit=options.sample_limit,
                    report_limit=options.report_limit,
                )
            except ValueError as exc:
                reports["query_stats"] = {
                    "schema_version": "mongo_query_targeting_report.v1",
                    "source": "$queryStats",
                    "status": "invalid_namespace",
                    "database": get_configured_database_name(),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "rows": [],
                    "recommended_next_step": (
                        "Use the configured database name and an optional collection."
                    ),
                }
            except (
                OperationFailure,
                PyMongoError,
                RuntimeError,
                TypeError,
                AttributeError,
            ) as exc:
                safe_error = safe_mongo_diagnostic_error(exc)
                reports["query_stats"] = {
                    "schema_version": "mongo_query_targeting_report.v1",
                    "source": "$queryStats",
                    "status": "query_stats_unavailable",
                    "database": get_configured_database_name(),
                    "error_type": safe_error.get("error_type"),
                    "error_code": safe_error.get("code_name") or safe_error.get("code"),
                    "error": safe_error.get("message"),
                    "rows": [],
                    "recommended_next_step": (
                        "Use a MongoDB deployment that supports $queryStats with "
                        "queryStatsRead/clusterMonitor, or reconcile with Atlas "
                        "Query Insights."
                    ),
                }
        record_mongo_query_stats_snapshot(reports["query_stats"])

    all_rows: list[Mapping[str, Any]] = []
    for report in reports.values():
        rows = report.get("rows") if isinstance(report, Mapping) else None
        if isinstance(rows, list):
            all_rows.extend(row for row in rows if isinstance(row, Mapping))
    summary = build_mongo_query_targeting_report_from_rows(
        all_rows,
        limit=options.report_limit,
        source=f"von_internal_mcp:{options.source}",
    )
    return {
        **base,
        "success": True,
        "status": "ok",
        "database": get_configured_database_name(),
        "options": {
            "source": options.source,
            "namespace": options.namespace,
            "min_millis": options.min_millis,
            "sample_limit": options.sample_limit,
            "report_limit": options.report_limit,
            "explain_samples": options.explain_samples,
            "explain_max_time_ms": options.explain_max_time_ms,
            "reset_in_process": options.reset_in_process,
        },
        "summary": summary,
        "reports": reports,
        "source_status": {
            name: (
                str(report.get("status") or "ok")
                if isinstance(report, Mapping)
                else "unknown"
            )
            for name, report in reports.items()
        },
        "recommended_next_step": (
            "Review high-ratio rows against repo-owned indexes and Atlas Query "
            "Insights. Keep index creation/drop as explicit reviewed code or "
            "operator work, not hidden tool mutation."
        ),
    }
