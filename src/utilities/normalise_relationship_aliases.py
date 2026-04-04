from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

concept_service = importlib.import_module("src.backend.services.concept_service")


ALIAS_MAP = {
    "has_subtypes": "has_subtype",
    "is_a_type_ofs": "is_a_type_of",
    "is_an_instance_ofs": "is_an_instance_of",
    "has_instances": "has_instance",
    "related_tos": "related_to",
}


def _normalise_list(value: object) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def normalise_relationship_aliases(
    relationships: Mapping[str, object],
) -> Tuple[Dict[str, object], Dict[str, str]]:
    if not isinstance(relationships, dict):
        return {}, {}

    updated: Dict[str, object] = dict(relationships)
    applied: Dict[str, str] = {}

    for alias, canonical in ALIAS_MAP.items():
        alias_vals = _normalise_list(updated.get(alias))
        canonical_vals = _normalise_list(updated.get(canonical))
        merged = canonical_vals + [val for val in alias_vals if val not in canonical_vals]

        if alias in updated or merged != canonical_vals:
            if merged:
                updated[canonical] = merged
            else:
                updated.pop(canonical, None)
            updated.pop(alias, None)
            applied[alias] = canonical

    return updated, applied


def _iter_concept_relationship_docs() -> Iterable[dict]:
    yield from concept_service.iter_concept_relationship_docs()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalise legacy relationship keys to canonical Vontology predicates."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to Vontology.",
    )
    args = parser.parse_args()

    updated: Dict[str, Dict[str, str]] = {}
    virtual: List[str] = []
    skipped: List[str] = []
    warnings: List[str] = []
    scanned = 0

    for concept in _iter_concept_relationship_docs():
        scanned += 1
        concept_id = concept.get("concept_id")
        if not isinstance(concept_id, str) or not concept_id:
            continue
        if (concept.get("metadata") or {}).get("virtual"):
            virtual.append(concept_id)
            continue

        relationships = concept.get("relationships") or {}
        normalised, applied = normalise_relationship_aliases(relationships)
        if not applied:
            skipped.append(concept_id)
            continue

        updated[concept_id] = applied
        if args.dry_run:
            continue

        try:
            concept_service.update_concept(concept_id, {"relationships": normalised})
        except Exception as exc:
            warnings.append(f"update_failed:{concept_id}:{exc}")

    payload = {
        "dry_run": args.dry_run,
        "scanned": scanned,
        "updated": updated,
        "skipped": skipped,
        "virtual": virtual,
        "warnings": warnings,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
