"""Synchronise predicate concepts that are referenced in code.

This uses Vontology services (not direct DB access) to ensure predicate concepts
exist and are marked as mentioned in Von code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Dict, Any

from ..services import concept_service
from ..vontology.code_concepts_registry import list_code_predicate_ids
from ..vontology.utils_vontology import is_predicate


MENTIONED_IN_CODE_ID = "#V#mentioned_in_von_code"
PREDICATE_TYPE_ID = "#V#predicate"


@dataclass(frozen=True)
class PredicateSyncResult:
    created: List[str]
    updated: List[str]
    skipped: List[str]
    warnings: List[str]


def _unique(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _resolve_parent_ids() -> List[str]:
    parents: List[str] = []
    for candidate in (PREDICATE_TYPE_ID, MENTIONED_IN_CODE_ID):
        try:
            exists = concept_service.get_concept_by_concept_id(candidate)
        except Exception:
            exists = None
        if exists:
            parents.append(candidate)
    return parents or [PREDICATE_TYPE_ID]


def _ensure_instance_of(
    concept_id: str, *, concept_doc: Dict[str, Any], parent_ids: List[str], dry_run: bool
) -> bool:
    relationships = concept_doc.get("relationships") or {}
    inst_of = relationships.get("is_an_instance_of", [])
    if isinstance(inst_of, str):
        inst_of = [inst_of]
    if not isinstance(inst_of, list):
        inst_of = []

    missing = [pid for pid in parent_ids if pid not in inst_of]
    if not missing:
        return False

    if dry_run:
        return True

    new_inst_of = inst_of + missing
    new_relationships = dict(relationships)
    new_relationships["is_an_instance_of"] = new_inst_of
    concept_service.update_concept(concept_id, {"relationships": new_relationships})
    return True


def sync_code_predicate_concepts(
    *, dry_run: bool = False, retag_non_predicates: bool = False
) -> PredicateSyncResult:
    created: List[str] = []
    updated: List[str] = []
    skipped: List[str] = []
    warnings: List[str] = []

    parent_ids = _resolve_parent_ids()
    predicate_ids = _unique(list_code_predicate_ids())

    for predicate_id in predicate_ids:
        name = predicate_id.replace("#V#", "")
        concept = None
        try:
            concept = concept_service.get_concept_by_concept_id(predicate_id)
        except Exception as exc:
            warnings.append(f"lookup_failed:{predicate_id}:{exc}")
            continue

        if concept is None or (concept.get("metadata") or {}).get("virtual"):
            if dry_run:
                created.append(predicate_id)
                continue
            concept_service.create_concept(
                name=name,
                concept_id=predicate_id,
                parent_concept_ids=parent_ids,
                create_as_instance=True,
            )
            created.append(predicate_id)
            continue

        if not is_predicate(concept):
            if retag_non_predicates:
                if _ensure_instance_of(
                    predicate_id,
                    concept_doc=concept,
                    parent_ids=parent_ids,
                    dry_run=dry_run,
                ):
                    updated.append(predicate_id)
                else:
                    skipped.append(predicate_id)
            else:
                warnings.append(f"not_predicate:{predicate_id}")
                skipped.append(predicate_id)
            continue

        if _ensure_instance_of(
            predicate_id,
            concept_doc=concept,
            parent_ids=parent_ids,
            dry_run=dry_run,
        ):
            updated.append(predicate_id)
        else:
            skipped.append(predicate_id)

    return PredicateSyncResult(
        created=created, updated=updated, skipped=skipped, warnings=warnings
    )
