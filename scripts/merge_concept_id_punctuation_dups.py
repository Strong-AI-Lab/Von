"""Merge punctuation/case-variant duplicate Vontology concepts into canonical IDs.

This consumes the JSON output from scripts/detect_concept_id_punctuation_dups.py and
merges each non-canonical variant into the canonical concept_id.

Safety:
- Defaults to simulation mode.
- Requires --apply to perform DB mutations.

Usage:
  pdm run python scripts/merge_concept_id_punctuation_dups.py --report logs/<file>.json
  pdm run python scripts/merge_concept_id_punctuation_dups.py --report logs/<file>.json --apply
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _bootstrap_sys_path() -> None:
    """Ensure repo root and src/ are on sys.path for direct script execution."""

    here = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(here, ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    src_root = os.path.join(repo_root, "src")
    if src_root not in sys.path:
        sys.path.insert(0, src_root)


_bootstrap_sys_path()

from src.backend.services.concept_merge_service import merge_concepts  # noqa: E402
from src.backend.services.concept_service import ConceptNotFoundError  # noqa: E402


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def _default_latest_report_path() -> Optional[str]:
    candidates = sorted(
        glob.glob(os.path.join("logs", "concept_id_punctuation_dups_*.json"))
    )
    if not candidates:
        return None
    return candidates[-1]


@dataclass(frozen=True)
class MergePair:
    source_id: str
    target_id: str
    canonical_id: str


def _load_report(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Report JSON must be an object")
    return data


def _extract_merge_pairs(report: Dict[str, Any]) -> List[MergePair]:
    groups = report.get("collision_groups")
    if not isinstance(groups, list):
        return []

    pairs: List[MergePair] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        canonical_id = group.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id:
            continue

        distinct_ids = group.get("distinct_ids")
        if not isinstance(distinct_ids, list):
            continue

        # Prefer the explicit 'variants' list if present (it excludes the canonical)
        variants = group.get("variants")
        if isinstance(variants, list) and variants:
            source_ids = [v for v in variants if isinstance(v, str) and v]
        else:
            source_ids = [
                cid
                for cid in distinct_ids
                if isinstance(cid, str) and cid and cid != canonical_id
            ]

        for source_id in source_ids:
            pairs.append(
                MergePair(
                    source_id=source_id,
                    target_id=canonical_id,
                    canonical_id=canonical_id,
                )
            )

    # Stable ordering for predictable output
    pairs.sort(key=lambda p: (p.target_id, p.source_id))
    return pairs


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge punctuation/case-variant duplicate concept IDs into canonical IDs"
    )
    parser.add_argument(
        "--report",
        help="Path to detector JSON report (logs/concept_id_punctuation_dups_*.json). Defaults to latest.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply merges (default is simulate-only).",
    )
    parser.add_argument(
        "--write-json",
        action="store_true",
        help="Write merge results JSON into logs/.",
    )
    parser.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Fail if a concept in the report does not exist (default is to skip missing).",
    )

    args = parser.parse_args(argv)

    report_path = args.report or _default_latest_report_path()
    if not report_path:
        print("[merge-dups] ERROR: No report provided and none found in logs/")
        return 2

    if not os.path.exists(report_path):
        print(f"[merge-dups] ERROR: Report not found: {report_path}")
        return 2

    report = _load_report(report_path)
    pairs = _extract_merge_pairs(report)

    if not pairs:
        print(
            f"[merge-dups] No collision groups found in {report_path}; nothing to merge."
        )
        return 0

    mode = "apply" if args.apply else "simulate"
    print(f"[merge-dups] Mode={mode} report={report_path} pairs={len(pairs)}")

    results: List[Dict[str, Any]] = []
    failures = 0
    skipped_missing = 0
    for pair in pairs:
        print(f"[merge-dups] {mode}: {pair.source_id} -> {pair.target_id}")
        try:
            res = merge_concepts(
                pair.source_id, pair.target_id, simulate=not args.apply
            )
        except ConceptNotFoundError as e:
            if args.fail_on_missing:
                res = {
                    "success": False,
                    "source_id": pair.source_id,
                    "target_id": pair.target_id,
                    "error": "ConceptNotFoundError",
                    "message": str(e),
                }
            else:
                skipped_missing += 1
                print(f"[merge-dups] WARNING: skipping missing concept: {e}")
                res = {
                    "success": True,
                    "skipped": True,
                    "skipped_reason": "missing_concept",
                    "source_id": pair.source_id,
                    "target_id": pair.target_id,
                    "message": str(e),
                }
        except Exception as e:
            res = {
                "success": False,
                "source_id": pair.source_id,
                "target_id": pair.target_id,
                "error": type(e).__name__,
                "message": str(e),
            }
        results.append(res)
        if not res.get("success"):
            failures += 1

    summary = {
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "mode": mode,
        "report_path": report_path,
        "pairs": [{"source": p.source_id, "target": p.target_id} for p in pairs],
        "results": results,
        "failures": failures,
        "skipped_missing": skipped_missing,
    }

    if args.write_json:
        out_path = os.path.join(
            "logs", f"merge_concept_id_punctuation_dups_{_utc_stamp()}.json"
        )
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"[merge-dups] Wrote {out_path}")

    if failures:
        print(f"[merge-dups] ERROR: {failures} merge(s) failed")
        return 1

    print(f"[merge-dups] OK: {len(pairs)} merge(s) completed ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
