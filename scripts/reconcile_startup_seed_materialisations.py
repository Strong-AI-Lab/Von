"""Explicitly reconcile startup seed materialisations for a release.

This command may write canonical Vontology state.  It is intentionally separate
from Flask construction so an ordinary process start can remain a cheap,
read-only freshness check.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_summary_service = importlib.import_module(
    "src.backend.services.concept_summary_field_vontology_service"
)
_publication_service = importlib.import_module(
    "src.backend.services.publication_scope_profile_vontology_service"
)
reconcile_canonical_concept_summary_fields = (
    _summary_service.reconcile_canonical_concept_summary_fields
)
reconcile_canonical_publication_scope_profiles = (
    _publication_service.reconcile_canonical_publication_scope_profiles
)

CONCEPT_SUMMARY_FIELDS = "concept-summary-fields"
PUBLICATION_SCOPE_PROFILES = "publication-scope-profiles"
ALL_FAMILIES = "all"
MACHINE_RECEIPT_PREFIX = "VON_STARTUP_SEED_RECONCILIATION_RECEIPT="

_RECONCILERS: dict[str, Callable[[], dict[str, Any]]] = {
    CONCEPT_SUMMARY_FIELDS: reconcile_canonical_concept_summary_fields,
    PUBLICATION_SCOPE_PROFILES: reconcile_canonical_publication_scope_profiles,
}


def reconcile_startup_seed_materialisations(
    families: Sequence[str],
) -> dict[str, Any]:
    """Run the selected canonical repairs and return one release receipt."""

    selected = list(_RECONCILERS) if ALL_FAMILIES in families else list(families)
    results: dict[str, dict[str, Any]] = {}
    for family in selected:
        results[family] = _RECONCILERS[family]()
    success = bool(results) and all(
        bool(result.get("ready")) for result in results.values()
    )
    return {
        "schema_version": "startup_seed_release_reconciliation.v1",
        "success": success,
        "state": "ready" if success else "unavailable",
        "families": results,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply and canonically verify selected startup seed families, then "
            "publish dependency freshness receipts. This is a state-changing "
            "release/maintenance command, not an ordinary startup step."
        )
    )
    parser.add_argument(
        "--family",
        action="append",
        choices=[ALL_FAMILIES, *_RECONCILERS],
        default=None,
        help="Seed family to reconcile; repeat to select more than one (default: all).",
    )
    parser.add_argument(
        "--machine-readable",
        action="store_true",
        help="Emit one prefixed compact JSON receipt for deployment automation.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = reconcile_startup_seed_materialisations(args.family or [ALL_FAMILIES])
    if args.machine_readable:
        print(
            MACHINE_RECEIPT_PREFIX
            + json.dumps(report, ensure_ascii=True, sort_keys=True)
        )
    else:
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
