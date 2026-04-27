"""Pure evidence-gathering primitives for entity-identity reasoning.

This module is a *support surface* for the
``#V#entity_identity_resolution_workflow``. It assembles structured evidence
profiles that the LLM rumination stage consumes via the
``#V#entity_duplicate_reasoning_prompt`` Vontology prompt concept.

This module deliberately performs NO scoring, ranking, classification, or
merge-direction decision-making. Those policies belong to the authored
prompt and are not the responsibility of Python code (see AGENTS.md section 4.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
import logging
import re

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.visibility_predicates import (
    get_specific_to_org_values,
    get_specific_to_user_values,
)
from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback
from .concept_relation_service import find_relations_with_argument
from .text_value_service import get_texts_for_concept

logger = logging.getLogger(__name__)

DEFAULT_TEXT_RELATION_LIMIT = 60
DEFAULT_AUTHORED_PAPER_LIMIT = 30
DEFAULT_INCIDENT_RELATION_LIMIT = 200
DEFAULT_RELATIONSHIP_TARGET_SAMPLE = 25
DEFAULT_NAMES_LIMIT = 10
DEFAULT_SOURCE_REFS_LIMIT = 25

PERSON_TYPE_ID = "#V#person"
AUTHORED_BY_PREDICATE = "#V#authored_by"

_URL_PATTERN = re.compile(r"^https?://\S+$", flags=re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOI_PATTERN = re.compile(r"^(?:doi:\s*)?10\.\S+/\S+$", flags=re.IGNORECASE)
_ORCID_PATTERN = re.compile(
    r"^(?:https?://orcid\.org/|orcid:\s*)?\d{4}-\d{4}-\d{4}-[\dX]{3}[\dX]$",
    flags=re.IGNORECASE,
)
_ARXIV_PATTERN = re.compile(
    r"^(?:arxiv:\s*)?(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+/\d{7}(?:v\d+)?)$",
    flags=re.IGNORECASE,
)
_TEXT_SAMPLE_LIMIT = 12
_TEXT_SAMPLE_PREVIEW_CHARS = 300


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _normalise_source_ref(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"^https?://", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.rstrip("/")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _identifier_ref_from_value(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if (
        _URL_PATTERN.match(cleaned)
        or _EMAIL_PATTERN.match(cleaned)
        or _DOI_PATTERN.match(cleaned)
        or _ORCID_PATTERN.match(cleaned)
        or _ARXIV_PATTERN.match(cleaned)
    ):
        return _normalise_source_ref(cleaned)
    return None


def _scope_descriptor(relationships: Mapping[str, Any]) -> dict[str, Any]:
    rels = dict(relationships)
    users = sorted(get_specific_to_user_values(rels))
    orgs = sorted(get_specific_to_org_values(rels))
    if not users and not orgs:
        kind = "global"
    elif users and not orgs:
        kind = "user"
    elif orgs and not users:
        kind = "org"
    else:
        kind = "user_and_org"
    return {
        "kind": kind,
        "specific_to_users": users,
        "specific_to_orgs": orgs,
    }


def _infer_kind(concept_doc: Mapping[str, Any]) -> str:
    computed = concept_doc.get("computed_kind")
    if isinstance(computed, str):
        cleaned = computed.strip().lower()
        if cleaned == "individual":
            return "instance"
        if cleaned in {"type", "instance", "predicate"}:
            return cleaned
    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return "unknown"
    instance_of = _as_string_list(relationships.get("is_an_instance_of"))
    if "#V#predicate" in instance_of:
        return "predicate"
    if _as_string_list(relationships.get("is_a_type_of")):
        return "type"
    if instance_of:
        return "instance"
    return "unknown"


def _gather_authored_papers(person_concept_id: str, *, limit: int) -> list[dict[str, Any]]:
    """Return papers connected to a person via ``#V#authored_by``.

    The person sits at argument 2 (first object) of the relation; the paper is
    the subject. We surface a small per-paper digest the LLM can reason over.
    """
    try:
        payload = find_relations_with_argument(
            person_concept_id,
            argument_index=2,
            predicate_filter=[AUTHORED_BY_PREDICATE],
            include_concept_preview=True,
            limit=limit,
        )
    except Exception as exc:
        logger.debug(
            "[entity_identity_evidence] authored-papers lookup failed for %s: %s",
            person_concept_id,
            exc,
        )
        return []

    hits = payload.get("hits") if isinstance(payload, Mapping) else None
    if not isinstance(hits, list):
        return []

    papers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, Mapping):
            continue
        paper_id = _as_text(hit.get("source_concept_id"))
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        papers.append({"paper_concept_id": paper_id})
        if len(papers) >= limit:
            break
    return papers


def _summarise_text_relations(
    concept_id: str,
    *,
    text_limit: int,
    source_refs_limit: int,
) -> dict[str, Any]:
    try:
        rows = get_texts_for_concept(concept_id, limit=text_limit)
    except Exception as exc:
        logger.debug(
            "[entity_identity_evidence] text-relation lookup failed for %s: %s",
            concept_id,
            exc,
        )
        rows = []

    source_refs: list[str] = []
    seen_sources: set[str] = set()
    predicate_counts: dict[str, int] = {}
    samples: list[dict[str, str]] = []

    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        text_value = _as_text(row.get("text"))
        predicate_raw = str(row.get("predicate") or "")
        if predicate_raw:
            predicate_counts[predicate_raw] = predicate_counts.get(predicate_raw, 0) + 1
        if not text_value:
            continue
        if len(samples) < _TEXT_SAMPLE_LIMIT:
            samples.append(
                {
                    "predicate": predicate_raw,
                    "text_preview": text_value[:_TEXT_SAMPLE_PREVIEW_CHARS],
                }
            )
        normalised = _identifier_ref_from_value(text_value)
        if normalised and normalised not in seen_sources and len(source_refs) < source_refs_limit:
            source_refs.append(normalised)
            seen_sources.add(normalised)

    return {
        "source_refs": source_refs,
        "text_predicate_counts": predicate_counts,
        "text_relation_samples": samples,
    }


def _summarise_relationships(
    relationships: Mapping[str, Any],
    *,
    target_sample_limit: int,
    source_refs_limit: int,
) -> dict[str, Any]:
    incident_predicate_counts: dict[str, int] = {}
    relationship_targets: list[str] = []
    seen_targets: set[str] = set()
    structural_source_refs: list[str] = []
    seen_struct_sources: set[str] = set()

    for predicate_id, raw_value in relationships.items():
        if not isinstance(predicate_id, str) or not predicate_id:
            continue
        targets = _as_string_list(raw_value)
        if not targets:
            continue
        incident_predicate_counts[predicate_id] = (
            incident_predicate_counts.get(predicate_id, 0) + len(targets)
        )
        for target in targets:
            if target.startswith("#V#") and target not in seen_targets:
                if len(relationship_targets) < target_sample_limit:
                    relationship_targets.append(target)
                seen_targets.add(target)
        for target in targets:
            if target.startswith("#V#"):
                continue
            normalised = _identifier_ref_from_value(target)
            if (
                normalised
                and normalised not in seen_struct_sources
                and len(structural_source_refs) < source_refs_limit
            ):
                structural_source_refs.append(normalised)
                seen_struct_sources.add(normalised)

    total_incident = sum(incident_predicate_counts.values())
    return {
        "incident_predicate_counts": incident_predicate_counts,
        "incident_relation_total": total_incident,
        "relationship_targets_sample": relationship_targets,
        "structural_source_refs": structural_source_refs,
    }


def build_concept_evidence_profile(
    concept_id: str,
    *,
    text_relation_limit: int = DEFAULT_TEXT_RELATION_LIMIT,
    authored_paper_limit: int = DEFAULT_AUTHORED_PAPER_LIMIT,
    relationship_target_sample_limit: int = DEFAULT_RELATIONSHIP_TARGET_SAMPLE,
    names_limit: int = DEFAULT_NAMES_LIMIT,
    source_refs_limit: int = DEFAULT_SOURCE_REFS_LIMIT,
) -> dict[str, Any] | None:
    """Assemble the LLM-facing evidence profile for ``concept_id``.

    Returns ``None`` if the concept does not exist or is not an instance.
    No scoring, no decisions - just structured facts.
    """

    cleaned_id = _as_text(concept_id)
    if not cleaned_id:
        return None

    concept_doc = ConceptsRepository.find_one({"concept_id": cleaned_id})
    if not isinstance(concept_doc, Mapping):
        return None

    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        relationships = {}

    if _infer_kind(concept_doc) != "instance":
        return None

    type_ids = sorted(
        {
            type_id
            for type_id in _as_string_list(relationships.get("is_an_instance_of"))
            if type_id and type_id != "#V#predicate"
        }
    )

    display_name = get_concept_display_name_with_names_fallback(dict(concept_doc))
    top_name = _as_text(concept_doc.get("name"))
    name_set: list[str] = []
    seen_names: set[str] = set()
    for candidate in (display_name, top_name):
        if candidate and candidate not in seen_names and len(name_set) < names_limit:
            name_set.append(candidate)
            seen_names.add(candidate)
    for candidate in _as_string_list(concept_doc.get("names")):
        if candidate and candidate not in seen_names and len(name_set) < names_limit:
            name_set.append(candidate)
            seen_names.add(candidate)

    text_summary = _summarise_text_relations(
        cleaned_id,
        text_limit=text_relation_limit,
        source_refs_limit=source_refs_limit,
    )
    relationship_summary = _summarise_relationships(
        relationships,
        target_sample_limit=relationship_target_sample_limit,
        source_refs_limit=source_refs_limit,
    )

    merged_source_refs: list[str] = []
    seen_sources: set[str] = set()
    for ref in (
        list(text_summary["source_refs"])
        + list(relationship_summary["structural_source_refs"])
    ):
        if ref and ref not in seen_sources and len(merged_source_refs) < source_refs_limit:
            merged_source_refs.append(ref)
            seen_sources.add(ref)

    is_person = PERSON_TYPE_ID in type_ids
    authored_papers: list[dict[str, Any]] = []
    if is_person:
        authored_papers = _gather_authored_papers(
            cleaned_id, limit=authored_paper_limit
        )

    evidence_counts = {
        "name_count": len(name_set),
        "source_ref_count": len(merged_source_refs),
        "relationship_target_sample_count": len(
            relationship_summary["relationship_targets_sample"]
        ),
        "authored_paper_count": len(authored_papers),
        "incident_relation_total": relationship_summary["incident_relation_total"],
        "text_relation_count": sum(text_summary["text_predicate_counts"].values()),
        "text_predicate_count": len(text_summary["text_predicate_counts"]),
    }

    return {
        "concept_id": cleaned_id,
        "display_name": display_name or top_name or cleaned_id,
        "names": name_set,
        "type_ids": type_ids,
        "is_person": is_person,
        "scope": _scope_descriptor(relationships),
        "source_refs": merged_source_refs,
        "incident_predicate_counts": relationship_summary["incident_predicate_counts"],
        "incident_relation_total": relationship_summary["incident_relation_total"],
        "relationship_targets": relationship_summary["relationship_targets_sample"],
        "authored_papers": authored_papers,
        "text_relations_summary": {
            "text_predicate_counts": text_summary["text_predicate_counts"],
            "text_relation_samples": text_summary["text_relation_samples"],
        },
        "evidence_counts": evidence_counts,
    }


def build_candidate_evidence_pairs(
    candidate_pairs: Sequence[Mapping[str, Any]],
    *,
    text_relation_limit: int = DEFAULT_TEXT_RELATION_LIMIT,
    authored_paper_limit: int = DEFAULT_AUTHORED_PAPER_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build per-pair evidence bundles ready for the LLM rumination stage.

    Inputs are ``{"a_concept_id": ..., "b_concept_id": ..., "name_key": ...}``
    rows produced by the scan stage. Output rows carry both profiles and
    pre-computed shared facts. No scoring or decisions are applied.
    """

    profile_cache: dict[str, dict[str, Any] | None] = {}
    enriched_pairs: list[dict[str, Any]] = []
    skipped_missing_profile = 0
    skipped_invalid = 0

    for raw_pair in candidate_pairs or []:
        if not isinstance(raw_pair, Mapping):
            skipped_invalid += 1
            continue
        a_id = _as_text(raw_pair.get("a_concept_id"))
        b_id = _as_text(raw_pair.get("b_concept_id"))
        if not a_id or not b_id or a_id == b_id:
            skipped_invalid += 1
            continue

        if a_id not in profile_cache:
            profile_cache[a_id] = build_concept_evidence_profile(
                a_id,
                text_relation_limit=text_relation_limit,
                authored_paper_limit=authored_paper_limit,
            )
        if b_id not in profile_cache:
            profile_cache[b_id] = build_concept_evidence_profile(
                b_id,
                text_relation_limit=text_relation_limit,
                authored_paper_limit=authored_paper_limit,
            )

        a_profile = profile_cache[a_id]
        b_profile = profile_cache[b_id]
        if a_profile is None or b_profile is None:
            skipped_missing_profile += 1
            continue

        shared_types = sorted(
            set(a_profile.get("type_ids") or []).intersection(
                b_profile.get("type_ids") or []
            )
        )
        shared_source_refs = sorted(
            set(a_profile.get("source_refs") or []).intersection(
                b_profile.get("source_refs") or []
            )
        )
        shared_relationship_targets = sorted(
            set(a_profile.get("relationship_targets") or []).intersection(
                b_profile.get("relationship_targets") or []
            )
        )

        enriched_pairs.append(
            {
                "pair_ids": sorted([a_id, b_id]),
                "name_key": _as_text(raw_pair.get("name_key")),
                "scope_match": str(a_profile.get("scope", {}).get("kind") or "")
                == str(b_profile.get("scope", {}).get("kind") or ""),
                "a": a_profile,
                "b": b_profile,
                "shared_facts": {
                    "type_ids": shared_types,
                    "source_refs": shared_source_refs,
                    "relationship_targets": shared_relationship_targets,
                },
            }
        )

    diagnostics = {
        "candidate_pair_count_in": len(candidate_pairs or []),
        "evidence_pair_count_out": len(enriched_pairs),
        "skipped_missing_profile": skipped_missing_profile,
        "skipped_invalid": skipped_invalid,
        "unique_concepts_profiled": len(profile_cache),
    }
    return enriched_pairs, diagnostics


__all__ = [
    "build_concept_evidence_profile",
    "build_candidate_evidence_pairs",
    "DEFAULT_TEXT_RELATION_LIMIT",
    "DEFAULT_AUTHORED_PAPER_LIMIT",
]
