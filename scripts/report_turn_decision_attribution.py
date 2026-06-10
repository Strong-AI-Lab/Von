"""Aggregate per-turn decision attribution over recent turn execution records.

JVNAUTOSCI-2499 diagnostic support surface. For each of the most recent turn
execution records (optionally namespace-filtered), this builds the persisted
turn diagnostics payload, projects its decision attribution, and reports the
aggregate architecture-integrity score plus the Python-fallback signature
histogram. Intended for the nightly drift review and for before/after
comparison around routing-authority seam closures; it reads telemetry only
and changes nothing.

Usage:
  python scripts/report_turn_decision_attribution.py --limit 50
  python scripts/report_turn_decision_attribution.py --namespace <ns> \
      --output-json tmp/decision_attribution_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPORT_SCHEMA_VERSION = "turn_decision_attribution_report.v1"


def _recent_request_ids(*, limit: int, namespace: str | None) -> list[dict[str, Any]]:
    from src.backend.services.turn_execution_record_service import (
        get_turn_execution_records_collection,
    )

    coll = get_turn_execution_records_collection()
    if coll is None:
        raise RuntimeError(
            "turn_execution_records collection is unavailable; check MONGO_URI"
        )
    query: dict[str, Any] = {}
    if namespace:
        query["namespace"] = namespace
    cursor = (
        coll.find(query, {"_id": 0, "request_id": 1, "namespace": 1, "created_at_utc": 1})
        .sort("created_at_utc", -1)
        .limit(int(limit))
    )
    rows: list[dict[str, Any]] = []
    for doc in cursor:
        request_id = doc.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            rows.append(
                {
                    "request_id": request_id.strip(),
                    "namespace": doc.get("namespace"),
                    "created_at": str(doc.get("created_at_utc") or ""),
                }
            )
    return rows


def build_report(*, limit: int, namespace: str | None) -> dict[str, Any]:
    from src.backend.services.turn_decision_attribution_service import (
        aggregate_turn_decision_attributions,
    )
    from src.backend.services.turn_execution_diagnostics_service import (
        get_turn_execution_diagnostics_payload,
    )

    rows = _recent_request_ids(limit=limit, namespace=namespace)
    attributions: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    diagnostics_failures = 0
    for row in rows:
        try:
            payload = get_turn_execution_diagnostics_payload(
                request_id=row["request_id"],
                namespace=row.get("namespace"),
            )
        except Exception:
            payload = None
        attribution = (
            payload.get("decision_attribution") if isinstance(payload, dict) else None
        )
        if not isinstance(attribution, dict):
            diagnostics_failures += 1
            continue
        attributions.append(attribution)
        turns.append(
            {
                "request_id": row["request_id"],
                "created_at": row.get("created_at"),
                "architecture_integrity_score": attribution.get("summary", {}).get(
                    "architecture_integrity_score"
                ),
                "decision_kind_breakdown": attribution.get("summary", {}).get(
                    "decision_kind_breakdown"
                ),
            }
        )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "requested_limit": int(limit),
        "namespace": namespace,
        "record_count": len(rows),
        "attributed_turn_count": len(attributions),
        "diagnostics_unavailable_count": diagnostics_failures,
        "aggregate": aggregate_turn_decision_attributions(attributions),
        "turns": turns,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()

    report = build_report(limit=args.limit, namespace=args.namespace)

    rendered = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote {output_path}")
    aggregate = report.get("aggregate", {})
    print(
        "Decision attribution over "
        f"{report['attributed_turn_count']}/{report['record_count']} recent turns: "
        f"mean architecture_integrity_score="
        f"{aggregate.get('mean_architecture_integrity_score')}"
    )
    signatures = aggregate.get("python_fallback_signatures") or {}
    for signature, count in signatures.items():
        print(f"  python_fallback {signature}: {count}")
    if not args.output_json:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
