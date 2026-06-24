"""Summarise redacted Mongo and route latency rows from Von logs.

This utility parses existing structural log lines such as ``[mongo_slow]`` and
``[slow_request]``. It emits only command names, collection names, query-shape
labels, endpoints, counts, and durations already redacted by the server logs.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any

REPORT_SCHEMA_VERSION = "mongo_slow_log_summary.v1"

_MONGO_RE = re.compile(
    r"\[(?P<kind>mongo_slow|mongo_command_failed)\]\s+"
    r"cmd=(?P<cmd>\S+)\s+db=(?P<db>\S+)\s+coll=(?P<coll>\S+)\s+"
    r"filter_shape=(?P<filter_shape>.*?)\s+sort_shape=(?P<sort_shape>.*?)\s+"
    r"projection_shape=(?P<projection_shape>.*?)\s+"
    r"(?:n_returned=(?P<n_returned>\d+)\s+)?"
    r"duration_ms=(?P<duration_ms>[0-9.]+)"
)
_SLOW_REQUEST_RE = re.compile(
    r"\[slow_request\]\s+"
    r"(?P<method>[A-Z]+)\s+(?P<endpoint>\S+)\s+took\s+"
    r"(?P<duration_ms>[0-9.]+)ms\s+"
    r"\(threshold=(?P<threshold_ms>[0-9.]+)ms\)\s+status=(?P<status>\d+)"
)


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed >= 0 else 0.0


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SlowLogRow:
    kind: str
    group_key: tuple[str, ...]
    duration_ms: float
    n_returned: int | None = None


def parse_slow_log_line(line: str) -> SlowLogRow | None:
    mongo_match = _MONGO_RE.search(line)
    if mongo_match:
        data = mongo_match.groupdict()
        return SlowLogRow(
            kind=data["kind"],
            group_key=(
                data["cmd"],
                data["db"],
                data["coll"],
                _safe_text(data.get("filter_shape")),
                _safe_text(data.get("sort_shape")),
                _safe_text(data.get("projection_shape")),
            ),
            duration_ms=_safe_float(data.get("duration_ms")),
            n_returned=_safe_int(data.get("n_returned")),
        )
    request_match = _SLOW_REQUEST_RE.search(line)
    if request_match:
        data = request_match.groupdict()
        return SlowLogRow(
            kind="slow_request",
            group_key=(data["method"], data["endpoint"], data["status"]),
            duration_ms=_safe_float(data.get("duration_ms")),
        )
    return None


def _iter_log_files(paths: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(item for item in path.rglob("*.log") if item.is_file()))
        elif path.is_file():
            files.append(path)
    return sorted(dict.fromkeys(files))


def _normalise_rows(
    rows: Iterable[Mapping[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    normalised = sorted(
        (
            {
                **row,
                "count": int(row.get("count") or 0),
                "total_duration_ms": round(
                    float(row.get("total_duration_ms") or 0.0), 1
                ),
                "max_duration_ms": round(float(row.get("max_duration_ms") or 0.0), 1),
                "avg_duration_ms": round(
                    float(row.get("total_duration_ms") or 0.0)
                    / max(int(row.get("count") or 1), 1),
                    1,
                ),
            }
            for row in rows
        ),
        key=lambda item: (item["total_duration_ms"], item["max_duration_ms"]),
        reverse=True,
    )
    return normalised[:limit]


def build_slow_log_summary(paths: Sequence[Path], *, limit: int = 20) -> dict[str, Any]:
    files = _iter_log_files(paths)
    mongo_groups: dict[tuple[str, ...], dict[str, Any]] = defaultdict(dict)
    route_groups: dict[tuple[str, ...], dict[str, Any]] = defaultdict(dict)
    line_count = 0
    matched_count = 0

    for path in files:
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line_count += 1
                parsed = parse_slow_log_line(line)
                if parsed is None:
                    continue
                matched_count += 1
                if parsed.kind in {"mongo_slow", "mongo_command_failed"}:
                    target = mongo_groups[parsed.group_key]
                    if not target:
                        cmd, db, coll, filter_shape, sort_shape, projection_shape = (
                            parsed.group_key
                        )
                        target.update(
                            {
                                "kind": parsed.kind,
                                "command": cmd,
                                "database": db,
                                "collection": coll,
                                "filter_shape": filter_shape,
                                "sort_shape": sort_shape,
                                "projection_shape": projection_shape,
                                "count": 0,
                                "failure_count": 0,
                                "total_duration_ms": 0.0,
                                "max_duration_ms": 0.0,
                                "max_n_returned": None,
                            }
                        )
                    if parsed.kind == "mongo_command_failed":
                        target["failure_count"] = (
                            int(target.get("failure_count") or 0) + 1
                        )
                    if parsed.n_returned is not None:
                        current_max = target.get("max_n_returned")
                        target["max_n_returned"] = max(
                            int(current_max or 0),
                            parsed.n_returned,
                        )
                else:
                    target = route_groups[parsed.group_key]
                    if not target:
                        method, endpoint, status = parsed.group_key
                        target.update(
                            {
                                "method": method,
                                "endpoint": endpoint,
                                "status": status,
                                "count": 0,
                                "total_duration_ms": 0.0,
                                "max_duration_ms": 0.0,
                            }
                        )
                target["count"] = int(target.get("count") or 0) + 1
                target["total_duration_ms"] = (
                    float(target.get("total_duration_ms") or 0.0) + parsed.duration_ms
                )
                target["max_duration_ms"] = max(
                    float(target.get("max_duration_ms") or 0.0),
                    parsed.duration_ms,
                )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_file_count": len(files),
        "line_count": line_count,
        "matched_line_count": matched_count,
        "mongo_shape_totals": _normalise_rows(mongo_groups.values(), limit=limit),
        "slow_route_totals": _normalise_rows(route_groups.values(), limit=limit),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Mongo Slow Log Summary",
        "",
        f"- Schema: `{_safe_text(report.get('schema_version'))}`",
        f"- Input log files: {int(report.get('input_file_count') or 0)}",
        f"- Matched slow lines: {int(report.get('matched_line_count') or 0)}",
        "",
        "## Mongo Shapes",
        "",
        "| Command | Collection | Count | Failures | Total ms | Max ms | Filter | Sort | Projection |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in report.get("mongo_shape_totals") or []:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {command} | {collection} | {count} | {failure_count} | {total_duration_ms} | {max_duration_ms} | {filter_shape} | {sort_shape} | {projection_shape} |".format(
                command=_safe_text(row.get("command")),
                collection=_safe_text(row.get("collection")),
                count=int(row.get("count") or 0),
                failure_count=int(row.get("failure_count") or 0),
                total_duration_ms=row.get("total_duration_ms") or 0,
                max_duration_ms=row.get("max_duration_ms") or 0,
                filter_shape=_safe_text(row.get("filter_shape")) or "-",
                sort_shape=_safe_text(row.get("sort_shape")) or "-",
                projection_shape=_safe_text(row.get("projection_shape")) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## Slow Routes",
            "",
            "| Method | Endpoint | Status | Count | Total ms | Max ms |",
            "| --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in report.get("slow_route_totals") or []:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| {method} | {endpoint} | {status} | {count} | {total_duration_ms} | {max_duration_ms} |".format(
                method=_safe_text(row.get("method")),
                endpoint=_safe_text(row.get("endpoint")),
                status=_safe_text(row.get("status")),
                count=int(row.get("count") or 0),
                total_duration_ms=row.get("total_duration_ms") or 0,
                max_duration_ms=row.get("max_duration_ms") or 0,
            )
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Log files or directories.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = build_slow_log_summary(args.paths, limit=max(1, args.limit))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_markdown(report), end="")
    return 0 if int(report.get("matched_line_count") or 0) else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
