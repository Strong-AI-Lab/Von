"""Audit relationship/text-relation predicates for "virtual" usage.

This is a read-only / dry-run audit.

It reports relationship keys and text relation predicates that are NOT backed by
an *actual persisted* predicate concept in the Vontology (i.e. a MongoDB concept
document with a concept_id starting with "#V#" that is typed as a predicate via
(is_an_instance_of ...) and `is_predicate()`.

Notes:
- Structural relationship keys (e.g. is_a_type_of) are excluded.
- Plain text-relation predicates like "hasContent" will be reported, because
  they are not predicate concepts.
- Code-only predicate concepts (see `code_concepts_registry.py`) are reported as
  "code_only_virtual" because they are not persisted.

PowerShell examples:
    pdm run python utilities/audit_virtual_predicates.py
    pdm run python utilities/audit_virtual_predicates.py --limit-concepts 5000 --limit-text-relations 20000

"""

from __future__ import annotations

import sys
import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


_STRUCTURAL_RELATIONSHIP_KEYS = {
    "is_a_type_of",
    "has_subtype",
    "is_an_instance_of",
    "has_instance",
    "related_to",
}

_KNOWN_NON_PREDICATE_RELATIONSHIP_KEYS = {
    "linked_to",
    "most_salient_type",
}

# Canonical mappings for known legacy relationship keys.
# JVNAUTOSCI-913: historical todo list membership relations were stored under 'has_item'.
_CANONICAL_PREDICATE_ID_OVERRIDES = {
    "has_item": "#V#has_todo_item",
    "#V#has_item": "#V#has_todo_item",
}


@dataclass(frozen=True)
class PredicateUsage:
    predicate: str
    count: int
    kind: str
    mapped_to: Optional[str] = None
    sample_subjects: Optional[List[str]] = None


def _normalise_values(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, str) and v.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw]
    return []


def _iter_relationship_predicate_keys(relationships: Dict[str, Any]) -> Iterable[str]:
    for key, value in relationships.items():
        if key in _STRUCTURAL_RELATIONSHIP_KEYS:
            continue
        if key in _KNOWN_NON_PREDICATE_RELATIONSHIP_KEYS:
            continue
        # Only report if it actually has content.
        if not _normalise_values(value):
            continue
        yield key


def _classify_predicate_id(predicate: str) -> tuple[str, Optional[str]]:
    """Return (kind, mapped_to).

    kind values:
    - structural_or_non_predicate (should not appear here)
    - plain_string
    - actual_predicate_concept
    - missing_predicate_concept
    - existing_non_predicate_concept
    - code_only_virtual
    """

    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.vontology.code_concepts_registry import is_code_concept_id
    from src.backend.vontology.utils_vontology import is_predicate

    mapped_to = _CANONICAL_PREDICATE_ID_OVERRIDES.get(predicate)
    effective = mapped_to or predicate

    if not isinstance(effective, str) or not effective:
        return "plain_string", mapped_to

    if not effective.startswith("#V#"):
        return "plain_string", mapped_to

    doc = ConceptsRepository.find_one(
        {"concept_id": effective}, {"concept_id": 1, "relationships": 1}
    )
    if doc is None:
        if is_code_concept_id(effective):
            return "code_only_virtual", mapped_to
        return "missing_predicate_concept", mapped_to

    if is_predicate(doc):
        return "actual_predicate_concept", mapped_to

    return "existing_non_predicate_concept", mapped_to


def audit(
    *,
    limit_concepts: Optional[int],
    limit_text_relations: Optional[int],
    sample_size: int,
) -> Dict[str, Any]:
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.db.repositories.text_value_repository import (
        TextRelationsRepository,
    )

    concepts_coll = ConceptsRepository.collection()
    if concepts_coll is None:
        raise RuntimeError("Concepts collection not available")

    relationship_predicate_counts: Counter[str] = Counter()
    relationship_predicate_samples: dict[str, list[str]] = defaultdict(list)

    scanned_concepts = 0
    cursor = concepts_coll.find(
        {"relationships": {"$exists": True}}, {"concept_id": 1, "relationships": 1}
    )
    for doc in cursor:
        scanned_concepts += 1
        if limit_concepts is not None and scanned_concepts > limit_concepts:
            break

        concept_id = doc.get("concept_id")
        relationships = doc.get("relationships")
        if not isinstance(concept_id, str) or not isinstance(relationships, dict):
            continue

        for pred_key in _iter_relationship_predicate_keys(relationships):
            relationship_predicate_counts[pred_key] += 1
            if len(relationship_predicate_samples[pred_key]) < sample_size:
                relationship_predicate_samples[pred_key].append(concept_id)

    text_predicate_counts: Counter[str] = Counter()
    text_predicate_samples: dict[str, list[str]] = defaultdict(list)

    scanned_text_relations = 0
    tr_cursor = TextRelationsRepository.find(
        {},
        {"predicate": 1, "subject_concept_id": 1},
    )
    for tr in tr_cursor:
        scanned_text_relations += 1
        if (
            limit_text_relations is not None
            and scanned_text_relations > limit_text_relations
        ):
            break

        pred = tr.get("predicate")
        subject = tr.get("subject_concept_id")
        if not isinstance(pred, str) or not pred.strip():
            continue

        pred = pred.strip()
        text_predicate_counts[pred] += 1
        if (
            isinstance(subject, str)
            and subject
            and len(text_predicate_samples[pred]) < sample_size
        ):
            text_predicate_samples[pred].append(subject)

    relationship_usages: list[PredicateUsage] = []
    for predicate, count in relationship_predicate_counts.most_common():
        kind, mapped_to = _classify_predicate_id(predicate)
        relationship_usages.append(
            PredicateUsage(
                predicate=predicate,
                count=count,
                kind=kind,
                mapped_to=mapped_to,
                sample_subjects=relationship_predicate_samples.get(predicate) or None,
            )
        )

    text_usages: list[PredicateUsage] = []
    for predicate, count in text_predicate_counts.most_common():
        kind, mapped_to = _classify_predicate_id(predicate)
        text_usages.append(
            PredicateUsage(
                predicate=predicate,
                count=count,
                kind=kind,
                mapped_to=mapped_to,
                sample_subjects=text_predicate_samples.get(predicate) or None,
            )
        )

    def _summarise(usages: list[PredicateUsage]) -> dict[str, int]:
        summary: Counter[str] = Counter()
        for usage in usages:
            summary[usage.kind] += usage.count
        return dict(summary)

    return {
        "success": True,
        "scanned": {
            "concepts": scanned_concepts,
            "text_relations": scanned_text_relations,
        },
        "relationship_predicates": {
            "summary": _summarise(relationship_usages),
            "items": [u.__dict__ for u in relationship_usages],
        },
        "text_relation_predicates": {
            "summary": _summarise(text_usages),
            "items": [u.__dict__ for u in text_usages],
        },
        "notes": [
            "Kinds: actual_predicate_concept (persisted predicate), code_only_virtual (registered in code only), missing_predicate_concept (not found), existing_non_predicate_concept (found but not predicate-typed), plain_string (not a #V# concept id).",
            "Plain text predicates like 'hasContent' are expected in text_relations and will appear as plain_string.",
            "Legacy override mappings are reported via mapped_to (e.g. has_item -> #V#has_todo_item).",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run audit for relationship keys and text relation predicates that are not backed by persisted predicate concepts."
        )
    )
    parser.add_argument(
        "--limit-concepts",
        type=int,
        default=None,
        help="Only scan the first N concept docs (debugging aid).",
    )
    parser.add_argument(
        "--limit-text-relations",
        type=int,
        default=None,
        help="Only scan the first N text_relations docs (debugging aid).",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=5,
        help="How many sample subject concept_ids to include per predicate.",
    )

    args = parser.parse_args()

    result = audit(
        limit_concepts=args.limit_concepts,
        limit_text_relations=args.limit_text_relations,
        sample_size=max(0, int(args.sample_size)),
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
