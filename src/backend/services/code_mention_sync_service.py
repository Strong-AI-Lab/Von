"""Synchronise mentioned-in-code/test markers for concept IDs referenced in code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Set

from ..services import concept_service
from ..services.concept_service import ConceptNotFoundError
from ..utils.concept_id_utils import canonicalise_vontology_concept_id
from ..vontology.code_concepts_registry import (
    MENTIONED_IN_VON_CODE_ID,
    MENTIONED_IN_VON_TEST_ID,
)


@dataclass(frozen=True)
class MentionSyncResult:
    updated: Dict[str, List[str]]
    skipped: List[str]
    missing: List[str]
    virtual: List[str]
    warnings: List[str]


def _normalise_instance_of(value: object) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _unique(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _mention_tags_for(
    concept_id: str, *, code_mentions: Set[str], test_mentions: Set[str]
) -> List[str]:
    tags: List[str] = []
    if concept_id in code_mentions:
        tags.append(MENTIONED_IN_VON_CODE_ID)
    if concept_id in test_mentions:
        tags.append(MENTIONED_IN_VON_TEST_ID)
    return tags


def sync_code_mention_concepts(
    *,
    code_mentions: Set[str],
    test_mentions: Set[str],
    dry_run: bool = True,
) -> MentionSyncResult:
    updated: Dict[str, List[str]] = {}
    skipped: List[str] = []
    missing: List[str] = []
    virtual: List[str] = []
    warnings: List[str] = []

    def _canonicalise(values: Set[str]) -> Set[str]:
        canonical: Set[str] = set()
        for value in values:
            canonical_id = canonicalise_vontology_concept_id(value) or value
            canonical.add(canonical_id)
        return canonical

    canonical_code = _canonicalise(code_mentions)
    canonical_test = _canonicalise(test_mentions)
    all_mentions = _unique(sorted(canonical_code | canonical_test))

    for concept_id in all_mentions:
        tags = _mention_tags_for(
            concept_id, code_mentions=canonical_code, test_mentions=canonical_test
        )
        if not tags:
            skipped.append(concept_id)
            continue

        try:
            concept = concept_service.get_concept_by_concept_id(concept_id)
        except ConceptNotFoundError:
            missing.append(concept_id)
            continue
        except Exception as exc:
            warnings.append(f"lookup_failed:{concept_id}:{exc}")
            continue

        if not concept:
            missing.append(concept_id)
            continue

        target_id = concept.get("concept_id") or concept_id
        if (concept.get("metadata") or {}).get("virtual"):
            virtual.append(concept_id)
            continue

        relationships = concept.get("relationships") or {}
        inst_of = _normalise_instance_of(relationships.get("is_an_instance_of"))
        missing_tags = [tag for tag in tags if tag not in inst_of]
        if not missing_tags:
            skipped.append(concept_id)
            continue

        updated[target_id] = missing_tags
        if dry_run:
            continue

        new_relationships = dict(relationships)
        new_relationships["is_an_instance_of"] = inst_of + missing_tags
        try:
            concept_service.update_concept(
                target_id, {"relationships": new_relationships}
            )
        except ConceptNotFoundError:
            warnings.append(f"update_failed:{target_id}")

    return MentionSyncResult(
        updated=updated,
        skipped=skipped,
        missing=missing,
        virtual=virtual,
        warnings=warnings,
    )
