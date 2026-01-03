"""Audit Vontology concept_id canonicality.

This is a read-only diagnostic script.

A concept_id is considered canonical if applying
`canonicalise_vontology_concept_id(concept_id)` yields the same string.

Usage (PowerShell):
  pdm run python scripts/audit_concept_id_canonicality.py

Exit code:
- 0 if no non-canonical IDs found
- 1 if any non-canonical IDs found
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Allow running as a standalone script (not as a module).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_ROOT = os.path.join(PROJECT_ROOT, "src")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

from src.backend.db.mongo_client import get_db
from src.backend.utils.concept_id_utils import canonicalise_vontology_concept_id


@dataclass(frozen=True)
class NonCanonicalHit:
    concept_id: str
    canonical_id: str
    mongo_id: Optional[str]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def _iter_concepts(*, limit: int = 0) -> Iterable[Tuple[str, Optional[str]]]:
    db = get_db()
    if db is None:
        raise RuntimeError(
            "MongoDB database connection not available (get_db() returned None)"
        )

    coll = db["concepts"]
    cursor = coll.find({}, {"concept_id": 1})
    if limit and limit > 0:
        cursor = cursor.limit(limit)

    for doc in cursor:
        cid = doc.get("concept_id")
        if not isinstance(cid, str):
            continue
        yield cid, (str(doc.get("_id")) if doc.get("_id") is not None else None)


def _audit(*, limit: int = 0) -> Dict[str, Any]:
    hits: List[NonCanonicalHit] = []

    for cid, mongo_id in _iter_concepts(limit=limit):
        if not cid.startswith("#"):
            continue
        canonical = canonicalise_vontology_concept_id(cid)
        if canonical and canonical != cid:
            hits.append(
                NonCanonicalHit(
                    concept_id=cid, canonical_id=canonical, mongo_id=mongo_id
                )
            )

    counter = Counter([h.concept_id for h in hits])

    return {
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "limit": limit,
        "noncanonical_total": len(hits),
        "noncanonical_distinct": len(counter),
        "top_noncanonical": [
            {"concept_id": cid, "count": count}
            for cid, count in counter.most_common(50)
        ],
        "examples": [
            {
                "concept_id": h.concept_id,
                "canonical_id": h.canonical_id,
                "mongo_id": h.mongo_id,
            }
            for h in hits[:200]
        ],
    }


def _write_json_report(report: Dict[str, Any]) -> str:
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", f"concept_id_noncanonical_{_utc_stamp()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit number of documents scanned"
    )
    parser.add_argument(
        "--write-json", action="store_true", help="Write JSON report under logs/"
    )
    args = parser.parse_args()

    report = _audit(limit=args.limit)

    print("Concept ID canonicality audit")
    print(f"Generated: {report['generated_at_utc']}")
    if report.get("limit"):
        print(f"Limit: {report['limit']}")

    print(f"Non-canonical concept_ids: {report['noncanonical_total']}")
    print(f"Distinct non-canonical concept_ids: {report['noncanonical_distinct']}")

    examples = report.get("examples") or []
    if examples:
        print("\nExamples (original -> canonical):")
        for row in examples[:20]:
            print(f"  {row['concept_id']} -> {row['canonical_id']}")
    else:
        print("\nNo non-canonical concept_ids found.")

    if args.write_json:
        out_path = _write_json_report(report)
        print(f"\nWrote JSON report: {out_path}")

    return 1 if report.get("noncanonical_distinct", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
