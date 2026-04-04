"""Vontology storage surfaces for semantic paper recommendation materialisation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from . import concept_service
from .concept_service import get_concept_by_concept_id_exact
from .paper_recommendation_constants import (
    GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
    HAS_PAPER_RECOMMENDATION_ASSERTION_PREDICATE_ID,
    PAPER_RECOMMENDATION_ASSERTION_TYPE_ID,
    PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID,
    PAPER_RECOMMENDATION_ASSERTS_SUBJECT_PREDICATE_ID,
    PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID,
)
from .relationship_write_service import add_relationship
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .workflow_vontology_materialisation_helpers import (
    ensure_instance_typing,
    load_concept,
    normalise_relationship_targets,
)

_PREDICATE_PARENT_IDS: tuple[str, ...] = ("#V#predicate", "#V#binary_predicate")
_LEGACY_PROFILE_LINK_PREDICATE_ID = "#V#has_paper_recommendation_profile"
_LEGACY_PROFILE_JSON_PREDICATE_ID = "#V#has_paper_recommendation_profile_json"
_DEFAULT_LANG = "en-NZ"
_PROFILE_FIELDS: tuple[str, ...] = (
    "project_description",
    "stated_interest_terms",
    "negative_interest_terms",
    "preferred_authors",
    "preferred_venues",
    "notes",
)


def _safe_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _safe_str(item)
        if not cleaned:
            continue
        fingerprint = cleaned.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        values.append(cleaned)
    return values


def _normalise_profile_overlay(
    value: Mapping[str, Any] | None,
    *,
    subject_concept_id: str,
) -> dict[str, Any]:
    profile = {
        "schema_version": "paper_matching_profile.v1",
        "subject_concept_id": subject_concept_id,
        "project_description": "",
        "stated_interest_terms": [],
        "negative_interest_terms": [],
        "preferred_authors": [],
        "preferred_venues": [],
        "notes": "",
        "updated_at": None,
    }
    if not isinstance(value, Mapping):
        return profile
    for field in _PROFILE_FIELDS:
        raw = value.get(field)
        if isinstance(profile[field], list):
            profile[field] = _normalise_string_list(raw)
        else:
            profile[field] = _safe_str(raw)
    profile["updated_at"] = _safe_str(value.get("updated_at")) or None
    return profile


def _ensure_type_concept(*, concept_id: str, name: str, description: str) -> None:
    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            parent_concept_ids=["#V#thing"],
            create_as_instance=False,
        )
        return

    relationships = dict(concept_doc.get("relationships") or {})
    parent_ids = normalise_relationship_targets(relationships.get("is_a_type_of"))
    if concept_id != "#V#thing" and "#V#thing" not in parent_ids:
        parent_ids.append("#V#thing")
        relationships["is_a_type_of"] = parent_ids
        concept_service.update_concept(concept_id, {"relationships": relationships})


def _ensure_predicate_concept(*, concept_id: str, name: str, description: str) -> None:
    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            description=description,
            parent_concept_ids=["#V#predicate"],
            create_as_instance=True,
        )
        return

    ensure_instance_typing(
        concept_id=concept_id,
        type_ids=("#V#predicate",),
        remove_type_parent_ids=_PREDICATE_PARENT_IDS,
    )


def ensure_paper_recommendation_primitives() -> dict[str, Any]:
    """Ensure recommendation assertion/profile primitives exist."""

    ensured: list[str] = []
    try:
        _ensure_type_concept(
            concept_id=PAPER_RECOMMENDATION_ASSERTION_TYPE_ID,
            name="Paper recommendation assertion",
            description=(
                "A materialised assertion linking a recommendation subject to a "
                "scholarly paper together with semantic evaluation metadata."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_ASSERTION_TYPE_ID)
        _ensure_predicate_concept(
            concept_id=GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
            name="Has paper matching profile JSON",
            description=(
                "Stores canonical profile overlay JSON for any concept that should "
                "be matched semantically against scholarly papers."
            ),
        )
        ensured.append(GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=HAS_PAPER_RECOMMENDATION_ASSERTION_PREDICATE_ID,
            name="Has paper recommendation assertion",
            description=(
                "Links a recommendation subject to a materialised paper "
                "recommendation assertion concept."
            ),
        )
        ensured.append(HAS_PAPER_RECOMMENDATION_ASSERTION_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID,
            name="Recommends paper",
            description=(
                "Links a paper recommendation assertion concept to the scholarly "
                "paper it recommends."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_ASSERTS_SUBJECT_PREDICATE_ID,
            name="Recommendation subject",
            description=(
                "Links a paper recommendation assertion concept to the concept "
                "for which the paper was recommended."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_ASSERTS_SUBJECT_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID,
            name="Has paper recommendation evaluation JSON",
            description=(
                "Stores the canonical semantic evaluation payload for a "
                "materialised paper recommendation assertion."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID)
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "ensured_concept_ids": ensured,
        }

    return {
        "success": True,
        "ensured_concept_ids": ensured,
    }


def _load_profile_json_from_text_relations(
    *,
    subject_concept_id: str,
    predicate_ids: Sequence[str],
) -> tuple[dict[str, Any] | None, str | None]:
    for predicate in predicate_ids:
        rows = get_texts_for_concept(
            subject_concept_id=subject_concept_id,
            predicate=predicate,
            limit=8,
        )
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            raw_text = _safe_str(row.get("text"))
            if not raw_text:
                continue
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping):
                return dict(parsed), predicate
    return None, None


def resolve_subject_ids_for_legacy_profile_concept(profile_concept_id: str) -> list[str]:
    """Resolve subject concepts that point to a legacy recommendation profile concept."""

    profile_id = _safe_str(profile_concept_id)
    if not profile_id:
        return []
    query = {f"relationships.{_LEGACY_PROFILE_LINK_PREDICATE_ID}": profile_id}
    cursor = ConceptsRepository.find(query, projection={"concept_id": 1}, limit=200)
    subjects: list[str] = []
    for row in cursor:
        if not isinstance(row, Mapping):
            continue
        concept_id = _safe_str(row.get("concept_id"))
        if concept_id:
            subjects.append(concept_id)
    return subjects


def load_subject_paper_matching_profile(subject_concept_id: str) -> dict[str, Any]:
    """Load the generic profile overlay for a recommendation subject."""

    subject_id = _safe_str(subject_concept_id)
    subject_doc = load_concept(subject_id)
    if subject_doc is None:
        return {
            "success": False,
            "error": "subject_not_found",
            "subject_concept_id": subject_id,
        }

    profile_json, source_predicate = _load_profile_json_from_text_relations(
        subject_concept_id=subject_id,
        predicate_ids=(GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,),
    )
    profile_concept_id: str | None = None
    legacy_payload: Mapping[str, Any] | None = None
    if profile_json is None:
        linked_profiles = normalise_relationship_targets(
            (subject_doc.get("relationships") or {}).get(_LEGACY_PROFILE_LINK_PREDICATE_ID)
        )
        for profile_id in linked_profiles:
            legacy_json, legacy_predicate = _load_profile_json_from_text_relations(
                subject_concept_id=profile_id,
                predicate_ids=(_LEGACY_PROFILE_JSON_PREDICATE_ID, "hasContent"),
            )
            if legacy_json is None:
                continue
            profile_json = legacy_json
            source_predicate = legacy_predicate
            profile_concept_id = profile_id
            break
        if profile_json is None:
            try:
                from .paper_recommendation_profile_vontology_service import (
                    load_paper_recommendation_profile,
                )

                legacy_payload = load_paper_recommendation_profile(
                    user_concept_id=subject_id,
                    create_if_missing=False,
                )
            except Exception:
                legacy_payload = None
            if isinstance(legacy_payload, Mapping) and legacy_payload.get("success"):
                maybe_profile = legacy_payload.get("profile")
                if isinstance(maybe_profile, Mapping):
                    profile_json = dict(maybe_profile)
                    source_predicate = _LEGACY_PROFILE_JSON_PREDICATE_ID
                    profile_concept_id = _safe_str(
                        legacy_payload.get("profile_concept_id")
                    ) or None

    return {
        "success": True,
        "subject_concept_id": subject_id,
        "subject_doc": dict(subject_doc),
        "profile": _normalise_profile_overlay(profile_json, subject_concept_id=subject_id),
        "profile_concept_id": profile_concept_id,
        "profile_source_predicate": source_predicate,
        "profile_present": profile_json is not None,
        "legacy_profile_payload": dict(legacy_payload)
        if isinstance(legacy_payload, Mapping)
        else None,
    }


def persist_subject_paper_matching_profile(
    *,
    subject_concept_id: str,
    profile: Mapping[str, Any] | None,
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the generic subject-level profile overlay."""

    ensure_report = ensure_paper_recommendation_primitives()
    if not ensure_report.get("success"):
        raise RuntimeError(
            f"Failed to ensure recommendation primitives: {ensure_report.get('error')}"
        )

    subject_id = _safe_str(subject_concept_id)
    normalised = _normalise_profile_overlay(profile, subject_concept_id=subject_id)
    normalised["updated_at"] = _utc_now_iso()
    upsert = upsert_singleton_text_relation(
        subject_concept_id=subject_id,
        predicate=GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
        lang=_DEFAULT_LANG,
        text=json.dumps(
            normalised,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        provenance=dict(provenance) if isinstance(provenance, Mapping) else None,
        context=dict(context) if isinstance(context, Mapping) else None,
        policy="replace_others",
        garbage_collect=True,
    )
    return {
        "success": True,
        "subject_concept_id": subject_id,
        "profile": normalised,
        "text_relation": upsert,
    }


def list_subject_concept_ids_with_paper_matching_profiles(*, limit: int = 200) -> list[str]:
    """List concepts that carry an explicit paper matching profile overlay."""

    subject_ids: list[str] = []
    seen: set[str] = set()

    generic_profile_relation_rows = ConceptsRepository.find(
        {"concept_id": {"$exists": True}},
        projection={
            "concept_id": 1,
            f"relationships.{_LEGACY_PROFILE_LINK_PREDICATE_ID}": 1,
        },
        limit=max(1, limit),
    )
    for row in generic_profile_relation_rows:
        if not isinstance(row, Mapping):
            continue
        concept_id = _safe_str(row.get("concept_id"))
        if not concept_id or concept_id in seen:
            continue
        generic_profile, _source_predicate = _load_profile_json_from_text_relations(
            subject_concept_id=concept_id,
            predicate_ids=(GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,),
        )
        has_legacy_link = bool(
            normalise_relationship_targets(
                (row.get("relationships") or {}).get(_LEGACY_PROFILE_LINK_PREDICATE_ID)
            )
        )
        if generic_profile is None and not has_legacy_link:
            continue
        seen.add(concept_id)
        subject_ids.append(concept_id)
        if len(subject_ids) >= limit:
            break

    return subject_ids


def _assertion_display_name(subject_concept_id: str, paper_concept_id: str) -> str:
    subject_name = _safe_str(
        (get_concept_by_concept_id_exact(subject_concept_id) or {}).get("name")
    ) or subject_concept_id
    paper_name = _safe_str(
        (get_concept_by_concept_id_exact(paper_concept_id) or {}).get("name")
    ) or paper_concept_id
    return f"Paper recommendation: {subject_name} -> {paper_name}"


def build_paper_recommendation_assertion_concept_id(
    *,
    subject_concept_id: str,
    paper_concept_id: str,
) -> str:
    """Return a stable assertion concept ID for one subject/paper pair."""

    raw = (
        f"{_safe_str(subject_concept_id).casefold()}|"
        f"{_safe_str(paper_concept_id).casefold()}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"#V#paper_recommendation_assertion_{digest}"


def upsert_paper_recommendation_assertion(
    *,
    subject_concept_id: str,
    paper_concept_id: str,
    evaluation_payload: Mapping[str, Any],
    rationale_summary: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist or update one subject-paper recommendation assertion."""

    ensure_report = ensure_paper_recommendation_primitives()
    if not ensure_report.get("success"):
        raise RuntimeError(
            f"Failed to ensure recommendation primitives: {ensure_report.get('error')}"
        )

    subject_id = _safe_str(subject_concept_id)
    paper_id = _safe_str(paper_concept_id)
    assertion_id = build_paper_recommendation_assertion_concept_id(
        subject_concept_id=subject_id,
        paper_concept_id=paper_id,
    )
    if load_concept(assertion_id) is None:
        concept_service.create_concept(
            name=_assertion_display_name(subject_id, paper_id),
            concept_id=assertion_id,
            description=(
                "Materialised semantic paper recommendation assertion with score, "
                "rationale, provenance, and policy metadata."
            ),
            parent_concept_ids=[PAPER_RECOMMENDATION_ASSERTION_TYPE_ID],
            create_as_instance=True,
        )
    else:
        ensure_instance_typing(
            concept_id=assertion_id,
            type_ids=(PAPER_RECOMMENDATION_ASSERTION_TYPE_ID,),
        )

    add_relationship(
        source_id=subject_id,
        predicate=HAS_PAPER_RECOMMENDATION_ASSERTION_PREDICATE_ID,
        target=assertion_id,
    )
    add_relationship(
        source_id=assertion_id,
        predicate=PAPER_RECOMMENDATION_ASSERTS_SUBJECT_PREDICATE_ID,
        target=subject_id,
    )
    add_relationship(
        source_id=assertion_id,
        predicate=PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID,
        target=paper_id,
    )

    canonical_payload = dict(evaluation_payload)
    canonical_payload.setdefault("assertion_concept_id", assertion_id)
    canonical_payload.setdefault("subject_concept_id", subject_id)
    canonical_payload.setdefault("paper_concept_id", paper_id)
    canonical_payload.setdefault("updated_at", _utc_now_iso())
    evaluation_upsert = upsert_singleton_text_relation(
        subject_concept_id=assertion_id,
        predicate=PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID,
        lang=_DEFAULT_LANG,
        text=json.dumps(
            canonical_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        provenance=dict(provenance) if isinstance(provenance, Mapping) else None,
        context=dict(context) if isinstance(context, Mapping) else None,
        policy="replace_others",
        garbage_collect=True,
    )

    description_upsert = None
    summary = _safe_str(rationale_summary) or _safe_str(
        canonical_payload.get("rationale_summary")
    )
    if summary:
        description_upsert = upsert_singleton_text_relation(
            subject_concept_id=assertion_id,
            predicate="hasDescription",
            lang=_DEFAULT_LANG,
            text=summary,
            provenance=dict(provenance) if isinstance(provenance, Mapping) else None,
            context=dict(context) if isinstance(context, Mapping) else None,
            policy="replace_others",
            garbage_collect=True,
        )

    return {
        "success": True,
        "assertion_concept_id": assertion_id,
        "subject_concept_id": subject_id,
        "paper_concept_id": paper_id,
        "evaluation_text_relation": evaluation_upsert,
        "description_text_relation": description_upsert,
    }


def _load_latest_text(subject_concept_id: str, predicate: str) -> str | None:
    rows = get_texts_for_concept(
        subject_concept_id=subject_concept_id,
        predicate=predicate,
        limit=4,
    )
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = _safe_str(row.get("text"))
        if text:
            return text
    return None


def load_materialised_paper_recommendations(
    *,
    subject_concept_id: str,
    include_inactive: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Load materialised paper recommendation assertions for a subject."""

    subject_id = _safe_str(subject_concept_id)
    subject_doc = load_concept(subject_id)
    if subject_doc is None:
        return {
            "success": False,
            "error": "subject_not_found",
            "subject_concept_id": subject_id,
            "recommendations": [],
        }

    assertion_ids = normalise_relationship_targets(
        (subject_doc.get("relationships") or {}).get(
            HAS_PAPER_RECOMMENDATION_ASSERTION_PREDICATE_ID
        )
    )
    rows: list[dict[str, Any]] = []
    for assertion_id in assertion_ids:
        assertion_doc = load_concept(assertion_id)
        if assertion_doc is None:
            continue
        paper_ids = normalise_relationship_targets(
            (assertion_doc.get("relationships") or {}).get(
                PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID
            )
        )
        paper_concept_id = paper_ids[0] if paper_ids else None
        if not paper_concept_id:
            continue
        raw_json = _load_latest_text(
            assertion_id, PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID
        )
        if not raw_json:
            continue
        try:
            evaluation = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(evaluation, Mapping):
            continue
        active = bool(evaluation.get("active", True))
        if not active and not include_inactive:
            continue
        paper_doc = get_concept_by_concept_id_exact(paper_concept_id) or {}
        paper_title = _safe_str(paper_doc.get("name")) or paper_concept_id
        row = {
            "assertion_concept_id": assertion_id,
            "paper_concept_id": paper_concept_id,
            "paper_title": paper_title,
            "score": float(evaluation.get("score") or 0.0),
            "active": active,
            "rationale_summary": _safe_str(evaluation.get("rationale_summary"))
            or _load_latest_text(assertion_id, "hasDescription"),
            "evaluation": dict(evaluation),
        }
        rows.append(row)

    rows.sort(
        key=lambda item: (
            not bool(item.get("active")),
            -float(item.get("score") or 0.0),
            _safe_str(item.get("paper_title")).casefold(),
            _safe_str(item.get("paper_concept_id")).casefold(),
        )
    )
    if limit > 0:
        rows = rows[:limit]

    return {
        "success": True,
        "subject_concept_id": subject_id,
        "recommendations": rows,
        "assertion_count": len(rows),
    }


__all__ = [
    "build_paper_recommendation_assertion_concept_id",
    "ensure_paper_recommendation_primitives",
    "list_subject_concept_ids_with_paper_matching_profiles",
    "load_materialised_paper_recommendations",
    "load_subject_paper_matching_profile",
    "persist_subject_paper_matching_profile",
    "resolve_subject_ids_for_legacy_profile_concept",
    "upsert_paper_recommendation_assertion",
]
