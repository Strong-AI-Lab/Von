"""Lightweight MongoDB operation attribution and query-shape diagnostics.

This module deliberately records structural metadata only: route attribution,
collection names, field/operator shapes, and execution counters. It does not
retain query values, update values, documents, prompts, email bodies, Mongo
credentials, or raw diagnostics.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from collections.abc import Iterable
from typing import Any, Mapping

_ATTRIBUTION_JIRA_KEY = "JVNAUTOSCI-2391"
_SNAPSHOT_LOCK = threading.Lock()
_QUERY_SHAPE_LOCK = threading.Lock()
_OPERATION_STATS: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
    lambda: {
        "count": 0,
        "failure_count": 0,
        "slow_count": 0,
        "total_elapsed_ms": 0.0,
        "max_elapsed_ms": 0.0,
        "last_error_type": None,
    }
)
_QUERY_SHAPE_STATS: dict[tuple[str, ...], dict[str, Any]] = {}

_QUERY_TARGETING_JIRA_KEY = "JVNAUTOSCI-2427"
_DEFAULT_QUERY_SHAPE_MAX_ROWS = 512
_QUERY_SHAPE_SAMPLE_REQUEST_IDS = 8
_MAX_SHAPE_TEXT = 240


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "y"}:
        return True
    if value in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def mongo_query_attribution_enabled() -> bool:
    """Return whether MongoDB profiler comments should be attached."""

    return _parse_bool_env("VON_MONGO_QUERY_ATTRIBUTION_ENABLED", True)


def mongo_operation_audit_enabled() -> bool:
    """Return whether in-process operation counters should be recorded."""

    return _parse_bool_env("VON_MONGO_OPERATION_AUDIT_ENABLED", True)


def mongo_operation_slow_threshold_ms() -> float:
    return _positive_float_env("VON_MONGO_OPERATION_AUDIT_SLOW_MS", 500.0)


def mongo_query_shape_telemetry_enabled() -> bool:
    """Return whether safe query-shape telemetry should be retained in-process."""

    return _parse_bool_env("VON_MONGO_QUERY_SHAPE_TELEMETRY_ENABLED", True)


def mongo_query_shape_max_rows() -> int:
    raw = os.getenv("VON_MONGO_QUERY_SHAPE_MAX_ROWS")
    if raw is None:
        return _DEFAULT_QUERY_SHAPE_MAX_ROWS
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_QUERY_SHAPE_MAX_ROWS
    return parsed if parsed > 0 else _DEFAULT_QUERY_SHAPE_MAX_ROWS


def _safe_str(value: Any, *, max_len: int = _MAX_SHAPE_TEXT) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _safe_route_value(key: str, value: Any) -> str:
    text = _safe_str(value, max_len=160)
    if key == "path" and "?" in text:
        text = text.split("?", 1)[0]
    return text


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _shape_from_mapping(value: Mapping[str, Any] | None, *, depth: int = 0) -> str:
    if not isinstance(value, Mapping) or not value:
        return ""
    parts: list[str] = []
    for key in sorted(str(k) for k in value.keys()):
        raw_child = value.get(key)
        if isinstance(raw_child, Mapping) and depth < 1:
            child_keys = ",".join(
                sorted(str(child_key) for child_key in raw_child.keys())
            )
            parts.append(f"{key}{{{child_keys}}}" if child_keys else key)
        elif isinstance(raw_child, list) and key.startswith("$"):
            parts.append(f"{key}[]")
        else:
            parts.append(key)
    return _safe_str(",".join(parts))


def _shape_from_sort(value: Any) -> str:
    if isinstance(value, Mapping):
        return _shape_from_mapping(value)
    if isinstance(value, list):
        keys: list[str] = []
        for item in value:
            if isinstance(item, (list, tuple)) and item:
                keys.append(str(item[0]))
            elif isinstance(item, Mapping):
                keys.extend(str(key) for key in item.keys())
        return _safe_str(",".join(sorted(keys)))
    return ""


def _shape_from_pipeline(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for stage in value[:12]:
        if not isinstance(stage, Mapping):
            parts.append(type(stage).__name__)
            continue
        for op, payload in stage.items():
            op_text = str(op)
            if op_text in {"$match", "$sort", "$project"} and isinstance(
                payload, Mapping
            ):
                parts.append(f"{op_text}({_shape_from_mapping(payload)})")
            else:
                parts.append(op_text)
    if len(value) > 12:
        parts.append("...")
    return _safe_str("|".join(parts))


def _shape_from_update(value: Any) -> str:
    if isinstance(value, Mapping):
        return _shape_from_mapping(value)
    if isinstance(value, list):
        return _shape_from_pipeline(value)
    return type(value).__name__ if value is not None else ""


def build_mongo_command_shape(
    command_name: str,
    command: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return redacted structural metadata for a Mongo command.

    The result contains collection names, field names, operator/stage names,
    and scalar command knobs such as limit. It deliberately excludes query
    values, update values, documents, credentials, and returned content.
    """

    if not isinstance(command, Mapping):
        return {}
    cmd_name = str(command_name or "").strip()
    collection = ""
    if cmd_name == "getMore":
        collection = str(command.get("collection") or "").strip()
    else:
        raw_collection = command.get(cmd_name)
        if isinstance(raw_collection, str):
            collection = raw_collection.strip()

    filter_doc = command.get("filter") or command.get("query")
    sort_doc = command.get("sort")
    projection_doc = command.get("projection") or command.get("fields")
    update_shape = ""
    filter_shape = _shape_from_mapping(
        filter_doc if isinstance(filter_doc, Mapping) else None
    )
    pipeline_shape = ""

    if cmd_name == "aggregate":
        pipeline = command.get("pipeline")
        pipeline_shape = _shape_from_pipeline(pipeline)
        if isinstance(pipeline, list):
            match_shapes = [
                _shape_from_mapping(stage.get("$match"))
                for stage in pipeline
                if isinstance(stage, Mapping)
                and isinstance(stage.get("$match"), Mapping)
            ]
            filter_shape = _safe_str("|".join(shape for shape in match_shapes if shape))
            sort_shapes = [
                _shape_from_mapping(stage.get("$sort"))
                for stage in pipeline
                if isinstance(stage, Mapping)
                and isinstance(stage.get("$sort"), Mapping)
            ]
            if sort_shapes:
                sort_doc = {"pipeline_sort": "|".join(sort_shapes)}

    if cmd_name == "update":
        update_shapes: list[str] = []
        filter_shapes: list[str] = []
        updates = command.get("updates")
        if isinstance(updates, list):
            for item in updates[:12]:
                if not isinstance(item, Mapping):
                    continue
                filter_shapes.append(_shape_from_mapping(item.get("q")))
                update_shapes.append(_shape_from_update(item.get("u")))
        filter_shape = _safe_str("|".join(shape for shape in filter_shapes if shape))
        update_shape = _safe_str("|".join(shape for shape in update_shapes if shape))

    if cmd_name == "findAndModify":
        update_shape = _shape_from_update(command.get("update"))

    limit = _safe_int(command.get("limit"))
    if limit is None and isinstance(command.get("cursor"), Mapping):
        limit = _safe_int(command["cursor"].get("batchSize"))

    comment = command.get("comment")
    comment_summary: dict[str, Any] = {}
    if isinstance(comment, Mapping):
        for key in ("app", "jira", "service", "collection", "operation", "detail"):
            if key in comment:
                comment_summary[key] = _safe_str(comment.get(key), max_len=96)
        route = comment.get("route")
        if isinstance(route, Mapping):
            comment_summary["route"] = {
                key: _safe_route_value(key, route.get(key))
                for key in ("method", "endpoint", "path")
                if route.get(key)
            }

    return {
        "command_name": _safe_str(cmd_name, max_len=64),
        "collection": _safe_str(collection, max_len=96),
        "filter_shape": filter_shape,
        "sort_shape": _shape_from_sort(sort_doc),
        "projection_shape": _shape_from_mapping(
            projection_doc if isinstance(projection_doc, Mapping) else None
        ),
        "update_shape": update_shape,
        "pipeline_shape": pipeline_shape,
        "limit": limit,
        "comment": comment_summary or None,
    }


def extract_n_returned_from_reply(
    command_name: str, reply: Mapping[str, Any] | None
) -> int | None:
    if not isinstance(reply, Mapping):
        return None
    cmd_name = str(command_name or "")
    if cmd_name in {"find", "aggregate", "getMore"}:
        cursor = reply.get("cursor")
        if isinstance(cursor, Mapping):
            for batch_key in ("firstBatch", "nextBatch"):
                batch = cursor.get(batch_key)
                if isinstance(batch, list):
                    return len(batch)
    for key in ("n", "nReturned", "nreturned", "nMatched"):
        value = _safe_int(reply.get(key))
        if value is not None:
            return value
    return None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    top = _safe_float(numerator)
    bottom = _safe_float(denominator)
    if top is None:
        return None
    if bottom is None or bottom <= 0:
        bottom = 1.0
    return round(top / bottom, 3)


def _query_shape_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        _safe_str(row.get("database"), max_len=96),
        _safe_str(row.get("collection"), max_len=96),
        _safe_str(row.get("command_name"), max_len=64),
        _safe_str(row.get("filter_shape")),
        _safe_str(row.get("sort_shape")),
        _safe_str(row.get("projection_shape")),
        _safe_str(row.get("update_shape")),
        _safe_str(row.get("pipeline_shape")),
        _safe_str(row.get("limit"), max_len=32),
    )


def record_mongo_query_shape_observation(
    *,
    command_name: str,
    database: str | None = None,
    collection: str | None = None,
    filter_shape: str | None = None,
    sort_shape: str | None = None,
    projection_shape: str | None = None,
    update_shape: str | None = None,
    pipeline_shape: str | None = None,
    limit: int | None = None,
    duration_ms: float | None = None,
    request_id: Any = None,
    n_returned: int | None = None,
    docs_examined: int | None = None,
    keys_examined: int | None = None,
    plan_summary: str | None = None,
    winning_index: str | None = None,
    in_memory_sort: bool | None = None,
    source: str = "command_listener",
    query_hash: str | None = None,
) -> None:
    """Record one redacted query-shape observation for diagnostics."""

    if not mongo_query_shape_telemetry_enabled():
        return

    row = {
        "database": _safe_str(database, max_len=96),
        "collection": _safe_str(collection, max_len=96),
        "command_name": _safe_str(command_name, max_len=64),
        "filter_shape": _safe_str(filter_shape),
        "sort_shape": _safe_str(sort_shape),
        "projection_shape": _safe_str(projection_shape),
        "update_shape": _safe_str(update_shape),
        "pipeline_shape": _safe_str(pipeline_shape),
        "limit": _safe_int(limit),
    }
    key = _query_shape_key(row)
    elapsed = _safe_float(duration_ms)
    returned = _safe_int(n_returned)
    docs = _safe_int(docs_examined)
    keys = _safe_int(keys_examined)

    with _QUERY_SHAPE_LOCK:
        if (
            key not in _QUERY_SHAPE_STATS
            and len(_QUERY_SHAPE_STATS) >= mongo_query_shape_max_rows()
        ):
            lowest_key = min(
                _QUERY_SHAPE_STATS,
                key=lambda candidate: (
                    int(_QUERY_SHAPE_STATS[candidate].get("count") or 0),
                    float(_QUERY_SHAPE_STATS[candidate].get("max_duration_ms") or 0.0),
                ),
            )
            _QUERY_SHAPE_STATS.pop(lowest_key, None)

        stats = _QUERY_SHAPE_STATS.setdefault(
            key,
            {
                **row,
                "namespace": ".".join(
                    part for part in (row["database"], row["collection"]) if part
                ),
                "count": 0,
                "total_duration_ms": 0.0,
                "max_duration_ms": 0.0,
                "total_returned": 0,
                "max_n_returned": None,
                "total_docs_examined": 0,
                "total_keys_examined": 0,
                "max_docs_examined": None,
                "max_keys_examined": None,
                "sample_request_ids": [],
                "sources": [],
            },
        )
        stats["count"] += 1
        if elapsed is not None:
            stats["total_duration_ms"] += max(0.0, elapsed)
            stats["max_duration_ms"] = max(float(stats["max_duration_ms"]), elapsed)
        if returned is not None:
            stats["total_returned"] += max(0, returned)
            current_max_returned = stats.get("max_n_returned")
            stats["max_n_returned"] = (
                returned
                if current_max_returned is None
                else max(int(current_max_returned), returned)
            )
        if docs is not None:
            stats["total_docs_examined"] += max(0, docs)
            stats["max_docs_examined"] = max(int(stats["max_docs_examined"] or 0), docs)
        if keys is not None:
            stats["total_keys_examined"] += max(0, keys)
            stats["max_keys_examined"] = max(int(stats["max_keys_examined"] or 0), keys)
        if plan_summary:
            stats["last_plan_summary"] = _safe_str(plan_summary, max_len=160)
        if winning_index:
            stats["last_winning_index"] = _safe_str(winning_index, max_len=160)
        if in_memory_sort is not None:
            stats["last_in_memory_sort"] = bool(in_memory_sort)
        if query_hash:
            stats["query_hash"] = _safe_str(query_hash, max_len=96)
        request_text = _safe_str(request_id, max_len=96)
        sample_ids = stats["sample_request_ids"]
        if request_text and request_text not in sample_ids:
            sample_ids.append(request_text)
            del sample_ids[:-_QUERY_SHAPE_SAMPLE_REQUEST_IDS]
        sources = stats["sources"]
        source_text = _safe_str(source, max_len=64)
        if source_text and source_text not in sources:
            sources.append(source_text)


def record_mongo_command_observation(
    *,
    command_name: str,
    database: str | None,
    command_shape: Mapping[str, Any],
    duration_ms: float | None,
    request_id: Any = None,
    n_returned: int | None = None,
    source: str = "command_listener",
) -> None:
    record_mongo_query_shape_observation(
        command_name=command_name,
        database=database,
        collection=command_shape.get("collection"),
        filter_shape=command_shape.get("filter_shape"),
        sort_shape=command_shape.get("sort_shape"),
        projection_shape=command_shape.get("projection_shape"),
        update_shape=command_shape.get("update_shape"),
        pipeline_shape=command_shape.get("pipeline_shape"),
        limit=_safe_int(command_shape.get("limit")),
        duration_ms=duration_ms,
        request_id=request_id,
        n_returned=n_returned,
        source=source,
    )


def _recommend_next_step(row: Mapping[str, Any]) -> str:
    if row.get("max_docs_examined") is None and row.get("max_keys_examined") is None:
        return (
            'Collect profiler/Atlas Query Insights or run bounded explain("executionStats") '
            "for this redacted shape."
        )
    docs_ratio = _safe_float(row.get("max_docs_examined_per_returned"))
    keys_ratio = _safe_float(row.get("max_keys_examined_per_returned"))
    if (docs_ratio is not None and docs_ratio >= 1000) or (
        keys_ratio is not None and keys_ratio >= 1000
    ):
        return (
            "High query targeting: compare filter/sort shape with repo-owned indexes "
            "and Atlas Performance Advisor before creating any index."
        )
    if row.get("last_in_memory_sort"):
        return (
            "Investigate sort coverage; profiler/explain indicates an in-memory sort."
        )
    return "Keep under observation; compare with Atlas Query Insights if this recurs."


def _ranked_query_shape_rows(
    stats_rows: Iterable[Mapping[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in stats_rows:
        count = int(raw.get("count") or 0)
        total_duration = float(raw.get("total_duration_ms") or 0.0)
        total_returned = int(raw.get("total_returned") or 0)
        total_docs = int(raw.get("total_docs_examined") or 0)
        total_keys = int(raw.get("total_keys_examined") or 0)
        row = dict(raw)
        row["average_duration_ms"] = round(total_duration / count, 3) if count else 0.0
        row["max_duration_ms"] = round(float(row.get("max_duration_ms") or 0.0), 3)
        row["docs_examined_per_returned"] = _ratio(total_docs, total_returned)
        row["keys_examined_per_returned"] = _ratio(total_keys, total_returned)
        row["max_docs_examined_per_returned"] = _ratio(
            row.get("max_docs_examined"), row.get("max_n_returned")
        )
        row["max_keys_examined_per_returned"] = _ratio(
            row.get("max_keys_examined"), row.get("max_n_returned")
        )
        row["estimated_waste_score"] = round(
            max(
                float(row.get("max_docs_examined_per_returned") or 0.0),
                float(row.get("max_keys_examined_per_returned") or 0.0),
                0.0,
            )
            * max(count, 1),
            3,
        )
        row["recommended_next_step"] = _recommend_next_step(row)
        rows.append(row)

    rows.sort(
        key=lambda item: (
            float(item.get("estimated_waste_score") or 0.0),
            float(item.get("max_docs_examined_per_returned") or 0.0),
            float(item.get("max_keys_examined_per_returned") or 0.0),
            float(item.get("total_duration_ms") or 0.0),
            int(item.get("count") or 0),
        ),
        reverse=True,
    )
    return rows[: max(1, int(limit))]


def aggregate_mongo_query_shape_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate already-redacted shape rows by command/filter/sort shape."""

    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in rows:
        key = _query_shape_key(raw)
        count = int(raw.get("count") or 1)
        row = grouped.setdefault(
            key,
            {
                "database": _safe_str(raw.get("database"), max_len=96),
                "collection": _safe_str(raw.get("collection"), max_len=96),
                "namespace": _safe_str(raw.get("namespace"), max_len=200),
                "command_name": _safe_str(raw.get("command_name"), max_len=64),
                "filter_shape": _safe_str(raw.get("filter_shape")),
                "sort_shape": _safe_str(raw.get("sort_shape")),
                "projection_shape": _safe_str(raw.get("projection_shape")),
                "update_shape": _safe_str(raw.get("update_shape")),
                "pipeline_shape": _safe_str(raw.get("pipeline_shape")),
                "limit": _safe_int(raw.get("limit")),
                "count": 0,
                "total_duration_ms": 0.0,
                "max_duration_ms": 0.0,
                "total_returned": 0,
                "max_n_returned": None,
                "total_docs_examined": 0,
                "total_keys_examined": 0,
                "max_docs_examined": None,
                "max_keys_examined": None,
                "sample_request_ids": [],
                "sources": [],
            },
        )
        row["count"] += count
        row["total_duration_ms"] += float(raw.get("total_duration_ms") or 0.0)
        row["max_duration_ms"] = max(
            float(row.get("max_duration_ms") or 0.0),
            float(raw.get("max_duration_ms") or 0.0),
        )
        row["total_returned"] += int(raw.get("total_returned") or 0)
        raw_max_returned = _safe_int(raw.get("max_n_returned"))
        if raw_max_returned is not None:
            row["max_n_returned"] = max(
                int(row.get("max_n_returned") or 0), raw_max_returned
            )
        row["total_docs_examined"] += int(raw.get("total_docs_examined") or 0)
        row["total_keys_examined"] += int(raw.get("total_keys_examined") or 0)
        raw_max_docs = _safe_int(raw.get("max_docs_examined"))
        if raw_max_docs is not None:
            row["max_docs_examined"] = max(
                int(row.get("max_docs_examined") or 0), raw_max_docs
            )
        raw_max_keys = _safe_int(raw.get("max_keys_examined"))
        if raw_max_keys is not None:
            row["max_keys_examined"] = max(
                int(row.get("max_keys_examined") or 0), raw_max_keys
            )
        for field in ("last_plan_summary", "last_winning_index", "query_hash"):
            if raw.get(field):
                row[field] = raw.get(field)
        if raw.get("limit") is not None and row.get("limit") is None:
            row["limit"] = _safe_int(raw.get("limit"))
        if raw.get("last_in_memory_sort") is not None:
            row["last_in_memory_sort"] = bool(raw.get("last_in_memory_sort"))
        sample_ids = row["sample_request_ids"]
        for request_id in raw.get("sample_request_ids") or []:
            request_text = _safe_str(request_id, max_len=96)
            if request_text and request_text not in sample_ids:
                sample_ids.append(request_text)
        del sample_ids[:-_QUERY_SHAPE_SAMPLE_REQUEST_IDS]
        sources = row["sources"]
        for source in raw.get("sources") or []:
            source_text = _safe_str(source, max_len=64)
            if source_text and source_text not in sources:
                sources.append(source_text)
    return list(grouped.values())


def build_mongo_query_targeting_report_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    limit: int = 20,
    source: str = "supplied_rows",
) -> dict[str, Any]:
    aggregated = aggregate_mongo_query_shape_rows(rows)
    ranked = _ranked_query_shape_rows(aggregated, limit=limit)
    return {
        "schema_version": "mongo_query_targeting_report.v1",
        "attribution_jira": _QUERY_TARGETING_JIRA_KEY,
        "source": source,
        "query_shape_telemetry_enabled": mongo_query_shape_telemetry_enabled(),
        "observed_shape_count": len(aggregated),
        "ranking": "estimated_waste_score, max scanned/returned ratios, total duration, count",
        "rows": ranked,
    }


def get_mongo_query_targeting_report(
    *, limit: int = 20, reset: bool = False
) -> dict[str, Any]:
    """Return ranked, redacted Mongo query-shape diagnostics."""

    with _QUERY_SHAPE_LOCK:
        rows = [dict(row) for row in _QUERY_SHAPE_STATS.values()]
        if reset:
            _QUERY_SHAPE_STATS.clear()
    return build_mongo_query_targeting_report_from_rows(
        rows,
        limit=limit,
        source="in_process_command_listener",
    )


def reset_mongo_query_shape_telemetry() -> None:
    with _QUERY_SHAPE_LOCK:
        _QUERY_SHAPE_STATS.clear()


def profiler_document_to_query_shape_row(
    doc: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Convert one Mongo ``system.profile`` document into a redacted shape row."""

    if not isinstance(doc, Mapping):
        return None
    command = doc.get("command")
    if not isinstance(command, Mapping):
        command = {}
    command_name = ""
    for candidate in (
        "find",
        "aggregate",
        "count",
        "distinct",
        "findAndModify",
        "update",
        "delete",
    ):
        if candidate in command:
            command_name = candidate
            break
    if not command_name:
        raw_op = str(doc.get("op") or "").strip()
        command_name = raw_op or "unknown"

    ns = str(doc.get("ns") or "").strip()
    database = ""
    collection = ""
    if "." in ns:
        database, collection = ns.split(".", 1)
    shape = build_mongo_command_shape(command_name, command)
    collection = collection or str(shape.get("collection") or "")
    n_returned = _safe_int(doc.get("nreturned") or doc.get("nReturned"))
    row = {
        "database": _safe_str(database, max_len=96),
        "collection": _safe_str(collection, max_len=96),
        "namespace": _safe_str(ns, max_len=200),
        "command_name": _safe_str(command_name, max_len=64),
        "filter_shape": shape.get("filter_shape") or "",
        "sort_shape": shape.get("sort_shape") or "",
        "projection_shape": shape.get("projection_shape") or "",
        "update_shape": shape.get("update_shape") or "",
        "pipeline_shape": shape.get("pipeline_shape") or "",
        "limit": shape.get("limit"),
        "count": 1,
        "total_duration_ms": float(_safe_float(doc.get("millis")) or 0.0),
        "max_duration_ms": float(_safe_float(doc.get("millis")) or 0.0),
        "total_returned": max(0, n_returned or 0),
        "max_n_returned": n_returned,
        "total_docs_examined": max(0, _safe_int(doc.get("docsExamined")) or 0),
        "total_keys_examined": max(0, _safe_int(doc.get("keysExamined")) or 0),
        "max_docs_examined": _safe_int(doc.get("docsExamined")),
        "max_keys_examined": _safe_int(doc.get("keysExamined")),
        "last_plan_summary": _safe_str(doc.get("planSummary"), max_len=160) or None,
        "last_in_memory_sort": "SORT" in str(doc.get("planSummary") or ""),
        "sample_request_ids": [
            _safe_str(value, max_len=96)
            for value in (
                doc.get("queryHash"),
                doc.get("planCacheKey"),
                doc.get("appName"),
            )
            if value
        ][:_QUERY_SHAPE_SAMPLE_REQUEST_IDS],
        "sources": ["system.profile"],
    }
    if doc.get("queryHash"):
        row["query_hash"] = _safe_str(doc.get("queryHash"), max_len=96)
    return row


def current_mongo_route_context() -> dict[str, str]:
    """Return safe Flask route metadata when a request context is active."""

    try:
        from flask import has_request_context, request

        if not has_request_context():
            return {}
        context: dict[str, str] = {}
        method = str(getattr(request, "method", "") or "").strip()
        endpoint = str(getattr(request, "endpoint", "") or "").strip()
        path = str(getattr(request, "path", "") or "").strip()
        if method:
            context["method"] = method
        if endpoint:
            context["endpoint"] = endpoint
        if path:
            context["path"] = path
        return context
    except Exception:
        return {}


def build_mongo_operation_comment(
    *,
    service: str,
    collection: str,
    operation: str,
    detail: str | None = None,
) -> dict[str, Any] | None:
    """Build a PyMongo/Atlas profiler comment for route-service attribution."""

    if not mongo_query_attribution_enabled():
        return None

    comment: dict[str, Any] = {
        "app": "von",
        "jira": _ATTRIBUTION_JIRA_KEY,
        "service": str(service or "unknown")[:96],
        "collection": str(collection or "unknown")[:96],
        "operation": str(operation or "unknown")[:96],
    }
    route_context = current_mongo_route_context()
    if route_context:
        comment["route"] = route_context
    if isinstance(detail, str) and detail.strip():
        comment["detail"] = detail.strip()[:96]
    return comment


def record_mongo_operation(
    *,
    service: str,
    collection: str,
    operation: str,
    elapsed_ms: float,
    success: bool,
    detail: str | None = None,
    error_type: str | None = None,
) -> None:
    """Record a safe in-process counter for a MongoDB operation."""

    if not mongo_operation_audit_enabled():
        return

    route_context = current_mongo_route_context()
    route_key = (
        route_context.get("endpoint") or route_context.get("path") or "background"
    )
    key = (
        str(service or "unknown")[:96],
        str(collection or "unknown")[:96],
        str(operation or "unknown")[:96],
        str(route_key or "background")[:160],
    )
    elapsed_value = max(0.0, float(elapsed_ms or 0.0))
    slow_threshold = mongo_operation_slow_threshold_ms()

    with _SNAPSHOT_LOCK:
        row = _OPERATION_STATS[key]
        row["count"] += 1
        row["total_elapsed_ms"] += elapsed_value
        row["max_elapsed_ms"] = max(float(row["max_elapsed_ms"]), elapsed_value)
        if not success:
            row["failure_count"] += 1
            row["last_error_type"] = str(error_type or "unknown")[:96]
        if elapsed_value >= slow_threshold:
            row["slow_count"] += 1
        if isinstance(detail, str) and detail.strip():
            row["last_detail"] = detail.strip()[:96]


def observe_mongo_operation(
    *,
    service: str,
    collection: str,
    operation: str,
    started_at: float,
    success: bool,
    detail: str | None = None,
    error_type: str | None = None,
) -> None:
    record_mongo_operation(
        service=service,
        collection=collection,
        operation=operation,
        elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
        success=success,
        detail=detail,
        error_type=error_type,
    )


def get_mongo_operation_audit_snapshot(*, reset: bool = False) -> dict[str, Any]:
    """Return safe aggregate counters for recent MongoDB operations."""

    with _SNAPSHOT_LOCK:
        operations = []
        for (service, collection, operation, route), row in _OPERATION_STATS.items():
            count = int(row["count"])
            total_elapsed_ms = float(row["total_elapsed_ms"])
            operations.append(
                {
                    "service": service,
                    "collection": collection,
                    "operation": operation,
                    "route": route,
                    "count": count,
                    "failure_count": int(row["failure_count"]),
                    "slow_count": int(row["slow_count"]),
                    "total_elapsed_ms": round(total_elapsed_ms, 3),
                    "average_elapsed_ms": (
                        round(total_elapsed_ms / count, 3) if count > 0 else 0.0
                    ),
                    "max_elapsed_ms": round(float(row["max_elapsed_ms"]), 3),
                    "last_error_type": row.get("last_error_type"),
                    "last_detail": row.get("last_detail"),
                }
            )
        if reset:
            _OPERATION_STATS.clear()

    operations.sort(
        key=lambda item: (
            int(item.get("slow_count") or 0),
            float(item.get("total_elapsed_ms") or 0.0),
            int(item.get("count") or 0),
        ),
        reverse=True,
    )
    return {
        "schema_version": "mongo_operation_audit_snapshot.v1",
        "attribution_jira": _ATTRIBUTION_JIRA_KEY,
        "query_attribution_enabled": mongo_query_attribution_enabled(),
        "operation_audit_enabled": mongo_operation_audit_enabled(),
        "slow_threshold_ms": mongo_operation_slow_threshold_ms(),
        "operations": operations,
    }


def reset_mongo_operation_audit_snapshot() -> None:
    with _SNAPSHOT_LOCK:
        _OPERATION_STATS.clear()


__all__ = [
    "build_mongo_operation_comment",
    "aggregate_mongo_query_shape_rows",
    "build_mongo_command_shape",
    "build_mongo_query_targeting_report_from_rows",
    "extract_n_returned_from_reply",
    "get_mongo_operation_audit_snapshot",
    "get_mongo_query_targeting_report",
    "mongo_operation_audit_enabled",
    "mongo_operation_slow_threshold_ms",
    "mongo_query_shape_telemetry_enabled",
    "profiler_document_to_query_shape_row",
    "mongo_query_attribution_enabled",
    "observe_mongo_operation",
    "record_mongo_command_observation",
    "record_mongo_operation",
    "record_mongo_query_shape_observation",
    "reset_mongo_operation_audit_snapshot",
    "reset_mongo_query_shape_telemetry",
]
