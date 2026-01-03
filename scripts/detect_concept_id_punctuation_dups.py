"""Detect punctuation-variant duplicates in Vontology concept_ids.

This is a read-only diagnostic script.

It looks for concept_ids that canonicalise to the same value under the rule:
- lower-case
- replace each maximal span of non-alphanumeric characters with a single underscore
- trim leading/trailing underscores
- ensure #V# prefix

Example duplicate group:
- #V#foo-bar
- #V#foo_bar

Usage (PowerShell):
  pdm run python scripts/detect_concept_id_punctuation_dups.py

Optional args:
  --limit N       Limit number of documents scanned (for quick checks)
  --write-json    Write a JSON report under logs/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


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
class ConceptIdHit:
    concept_id: str
    mongo_id: Optional[str]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def _iter_concept_ids(*, limit: int = 0) -> Iterable[ConceptIdHit]:
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
        yield ConceptIdHit(
            concept_id=cid,
            mongo_id=str(doc.get("_id")) if doc.get("_id") is not None else None,
        )


def _build_collision_groups(
    hits: Iterable[ConceptIdHit],
) -> Dict[str, List[ConceptIdHit]]:
    groups: Dict[str, List[ConceptIdHit]] = defaultdict(list)
    for hit in hits:
        if not hit.concept_id.startswith("#"):
            continue
        canonical = canonicalise_vontology_concept_id(hit.concept_id)
        if canonical is None:
            continue
        groups[canonical].append(hit)
    return groups


def _summarise(groups: Dict[str, List[ConceptIdHit]]) -> Dict[str, Any]:
    collision_groups: List[Dict[str, Any]] = []

    for canonical_id, hits in groups.items():
        # Only report groups with >1 distinct concept_id.
        distinct_ids = sorted({h.concept_id for h in hits})
        if len(distinct_ids) <= 1:
            continue

        has_canonical = canonical_id in set(distinct_ids)
        variants = [cid for cid in distinct_ids if cid != canonical_id]

        collision_groups.append(
            {
                "canonical_id": canonical_id,
                "has_canonical": has_canonical,
                "distinct_ids": distinct_ids,
                "variants": variants,
                "count_docs": len(hits),
            }
        )

    collision_groups.sort(key=lambda g: (-(len(g["distinct_ids"])), g["canonical_id"]))

    return {
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "groups_total": len(groups),
        "collision_groups": collision_groups,
        "collision_groups_total": len(collision_groups),
    }


def _print_report(report: Dict[str, Any], *, max_groups: int = 40) -> None:
    print("Concept ID punctuation-variant duplicate scan")
    print(f"Generated: {report['generated_at_utc']}")
    print(f"Canonical buckets: {report['groups_total']}")
    print(f"Collision groups: {report['collision_groups_total']}")

    groups = report.get("collision_groups") or []
    if not groups:
        print("\nNo collisions found.")
        return

    print("\nTop collision groups:")
    for idx, g in enumerate(groups[:max_groups], start=1):
        canonical_id = g["canonical_id"]
        distinct_ids = g["distinct_ids"]
        has_canonical = g["has_canonical"]
        print(
            f"\n{idx}. {canonical_id}  (has_canonical={has_canonical}, distinct={len(distinct_ids)})"
        )
        for cid in distinct_ids:
            marker = "*" if cid == canonical_id else "-"
            print(f"  {marker} {cid}")

    remaining = len(groups) - min(len(groups), max_groups)
    if remaining > 0:
        print(f"\n… plus {remaining} more collision groups")


def _write_json_report(report: Dict[str, Any]) -> str:
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", f"concept_id_punctuation_dups_{_utc_stamp()}.json")
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

    hits = list(_iter_concept_ids(limit=args.limit))
    groups = _build_collision_groups(hits)
    report = _summarise(groups)

    _print_report(report)

    if args.write_json:
        path = _write_json_report(report)
        print(f"\nWrote JSON report: {path}")

    # Exit non-zero if collisions exist (useful for CI-style checks).
    return 1 if report.get("collision_groups_total", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
