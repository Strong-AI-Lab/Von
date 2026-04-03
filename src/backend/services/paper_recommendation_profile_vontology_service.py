"""Vontology-backed paper recommendation profiles for person concepts.

This service keeps recommendation-specific profile state small and explicit:
the broad predicate extent around a person remains the substrate, while the
profile captures the stable, inspectable preference overlay that later
recommendation/ranking workflows can read directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .relationship_write_service import add_relationship
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .workflow_vontology_materialisation_helpers import (
    ensure_instance_typing,
    load_concept,
    normalise_relationship_targets,
)

PAPER_RECOMMENDATION_PROFILE_TYPE_ID = "#V#paper_recommendation_profile"
PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID = "#V#has_paper_recommendation_profile"
PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID = (
    "#V#has_paper_recommendation_profile_json"
)
RESEARCH_INTEREST_PREDICATE_ID = "#V#has_research_interest"
MEMBER_OF_ORGANISATION_PREDICATE_ID = "#V#member_of_organisation"

_PREDICATE_PARENT_IDS: tuple[str, ...] = ("#V#predicate", "#V#binary_predicate")
_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID,
    "has_paper_recommendation_profile_json",
    "hasPaperRecommendationProfileJson",
    "hasContent",
)

PROFILE_SCHEMA_VERSION = "paper_recommendation_profile.v1"


def _safe_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _dedupe_casefold(values: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _safe_str(value)
        if not cleaned:
            continue
        fingerprint = cleaned.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(cleaned)
    return deduped


def _normalise_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [item for item in value.split(",")]
        return _dedupe_casefold(candidates)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return _dedupe_casefold([str(item) for item in value if isinstance(item, str)])


def _build_profile_concept_id(user_concept_id: str) -> str:
    user_suffix = _safe_str(user_concept_id)
    if user_suffix.startswith("#V#"):
        user_suffix = user_suffix[3:]
    user_suffix = user_suffix or "unknown_user"
    return f"#V#paper_recommendation_profile_for_{user_suffix}"


def _profile_display_name(user_doc: Mapping[str, Any], user_concept_id: str) -> str:
    display_name = _safe_str(user_doc.get("name")) or _safe_str(user_concept_id)
    if display_name.startswith("#V#"):
        display_name = display_name[3:].replace("_", " ").strip()
    return f"Paper recommendation profile for {display_name or 'user'}"


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
    existing_parent_ids = normalise_relationship_targets(
        relationships.get("is_a_type_of")
    )
    if concept_id == "#V#thing" or "#V#thing" in existing_parent_ids:
        return
    existing_parent_ids.append("#V#thing")
    relationships["is_a_type_of"] = existing_parent_ids
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


def ensure_paper_recommendation_profile_primitives() -> dict[str, Any]:
    """Ensure the recommendation-profile type and predicates exist."""

    created_or_repaired: list[str] = []
    try:
        _ensure_type_concept(
            concept_id=PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
            name="Paper recommendation profile",
            description=(
                "A per-person profile capturing explicit preferences and guidance "
                "for paper recommendation workflows."
            ),
        )
        created_or_repaired.append(PAPER_RECOMMENDATION_PROFILE_TYPE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID,
            name="Has paper recommendation profile",
            description=(
                "Links a person to a paper recommendation profile concept that "
                "summarises recommendation-specific preferences."
            ),
        )
        created_or_repaired.append(PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID,
            name="Has paper recommendation profile JSON",
            description=(
                "Stores the canonical JSON payload for a paper recommendation "
                "profile concept."
            ),
        )
        created_or_repaired.append(PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID)
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "ensured_concept_ids": created_or_repaired,
        }

    return {
        "success": True,
        "ensured_concept_ids": created_or_repaired,
    }


def _resolve_related_concept_summaries(concept_ids: Sequence[str]) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for concept_id in concept_ids:
        cleaned_id = _safe_str(concept_id)
        if not cleaned_id:
            continue
        name = cleaned_id
        try:
            concept_doc = get_concept_by_concept_id(cleaned_id)
        except Exception:
            concept_doc = None
        if isinstance(concept_doc, Mapping):
            name = _safe_str(concept_doc.get("name")) or cleaned_id
        summaries.append({"concept_id": cleaned_id, "name": name})
    return summaries


def _derived_context_for_user(user_doc: Mapping[str, Any]) -> dict[str, Any]:
    relationships = dict(user_doc.get("relationships") or {})
    research_interest_ids = normalise_relationship_targets(
        relationships.get(RESEARCH_INTEREST_PREDICATE_ID)
    )
    organisation_ids = normalise_relationship_targets(
        relationships.get(MEMBER_OF_ORGANISATION_PREDICATE_ID)
    )
    return {
        "research_interest_concepts": _resolve_related_concept_summaries(
            research_interest_ids
        ),
        "organisation_concept_ids": organisation_ids,
    }


def _empty_profile(
    *,
    user_concept_id: str,
    profile_concept_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": _safe_str(profile_concept_id) or profile_concept_id,
        "owner_person_concept_id": user_concept_id,
        "profile_concept_id": profile_concept_id,
        "project_description": "",
        "stated_interest_terms": [],
        "negative_interest_terms": [],
        "preferred_authors": [],
        "preferred_venues": [],
        "notes": "",
        "updated_at": None,
    }


def _normalise_profile_payload(
    raw_profile: Mapping[str, Any] | None,
    *,
    user_concept_id: str,
    profile_concept_id: str,
) -> dict[str, Any]:
    base = _empty_profile(
        user_concept_id=user_concept_id,
        profile_concept_id=profile_concept_id,
    )
    if not isinstance(raw_profile, Mapping):
        return base

    base["project_description"] = _safe_str(raw_profile.get("project_description"))
    base["stated_interest_terms"] = _normalise_string_list(
        raw_profile.get("stated_interest_terms")
    )
    base["negative_interest_terms"] = _normalise_string_list(
        raw_profile.get("negative_interest_terms")
    )
    base["preferred_authors"] = _normalise_string_list(
        raw_profile.get("preferred_authors")
    )
    base["preferred_venues"] = _normalise_string_list(
        raw_profile.get("preferred_venues")
    )
    base["notes"] = _safe_str(raw_profile.get("notes"))
    base["updated_at"] = _safe_str(raw_profile.get("updated_at")) or None
    return base


def _load_user_doc(user_concept_id: str) -> Mapping[str, Any]:
    concept_doc = get_concept_by_concept_id(user_concept_id)
    if not isinstance(concept_doc, Mapping):
        raise ConceptNotFoundError(f"User concept not found: {user_concept_id}")
    return concept_doc


def resolve_or_create_paper_recommendation_profile_concept_id(
    *,
    user_concept_id: str,
    create_if_missing: bool = False,
) -> str | None:
    """Resolve the linked profile concept for a user, optionally creating it."""

    ensure_report = ensure_paper_recommendation_profile_primitives()
    if not ensure_report.get("success"):
        raise RuntimeError(
            f"Failed to ensure recommendation profile primitives: {ensure_report.get('error')}"
        )

    user_doc = _load_user_doc(user_concept_id)
    relationships = dict(user_doc.get("relationships") or {})
    linked_profile_ids = normalise_relationship_targets(
        relationships.get(PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID)
    )

    for candidate_id in linked_profile_ids:
        if load_concept(candidate_id) is None:
            continue
        ensure_instance_typing(
            concept_id=candidate_id,
            type_ids=(PAPER_RECOMMENDATION_PROFILE_TYPE_ID,),
        )
        return candidate_id

    stable_profile_id = _build_profile_concept_id(user_concept_id)
    stable_profile_doc = load_concept(stable_profile_id)
    if stable_profile_doc is not None:
        ensure_instance_typing(
            concept_id=stable_profile_id,
            type_ids=(PAPER_RECOMMENDATION_PROFILE_TYPE_ID,),
        )
        add_relationship(
            source_id=user_concept_id,
            predicate=PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID,
            target=stable_profile_id,
        )
        return stable_profile_id

    if not create_if_missing:
        return None

    concept_service.create_concept(
        name=_profile_display_name(user_doc, user_concept_id),
        concept_id=stable_profile_id,
        description=(
            "User-editable paper recommendation profile used by recommendation "
            "and explanation workflows."
        ),
        parent_concept_ids=[PAPER_RECOMMENDATION_PROFILE_TYPE_ID],
        create_as_instance=True,
    )
    add_relationship(
        source_id=user_concept_id,
        predicate=PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID,
        target=stable_profile_id,
    )
    return stable_profile_id


def load_paper_recommendation_profile(
    *,
    user_concept_id: str,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    """Load one user's paper recommendation profile plus derived context."""

    user_doc = _load_user_doc(user_concept_id)
    profile_concept_id = resolve_or_create_paper_recommendation_profile_concept_id(
        user_concept_id=user_concept_id,
        create_if_missing=create_if_missing,
    )
    if not profile_concept_id:
        return {
            "success": False,
            "user_concept_id": user_concept_id,
            "error": "profile_not_found",
        }

    loaded_profile: dict[str, Any] | None = None
    source_predicate: str | None = None
    for predicate in _PROFILE_TEXT_PREDICATES:
        texts = get_texts_for_concept(
            subject_concept_id=profile_concept_id,
            predicate=predicate,
            limit=10,
        )
        for row in texts:
            raw_text = _safe_str((row or {}).get("text"))
            if not raw_text:
                continue
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping):
                loaded_profile = _normalise_profile_payload(
                    parsed,
                    user_concept_id=user_concept_id,
                    profile_concept_id=profile_concept_id,
                )
                source_predicate = predicate
                break
        if loaded_profile is not None:
            break

    if loaded_profile is None:
        loaded_profile = _empty_profile(
            user_concept_id=user_concept_id,
            profile_concept_id=profile_concept_id,
        )
        if create_if_missing:
            upsert_result = upsert_paper_recommendation_profile(
                user_concept_id=user_concept_id,
                recommendation_profile=loaded_profile,
            )
            if isinstance(upsert_result.get("profile"), Mapping):
                loaded_profile = dict(upsert_result["profile"])
            source_predicate = PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID

    return {
        "success": True,
        "user_concept_id": user_concept_id,
        "profile_concept_id": profile_concept_id,
        "profile": loaded_profile,
        "derived_context": _derived_context_for_user(user_doc),
        "diagnostics": {
            "source_predicate": source_predicate,
            "profile_exists": bool(source_predicate),
        },
    }


def upsert_paper_recommendation_profile(
    *,
    user_concept_id: str,
    recommendation_profile: Mapping[str, Any] | None,
    language: str = "en-NZ",
    policy: str = "replace_others",
    garbage_collect: bool = True,
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one user's paper recommendation profile."""

    profile_concept_id = resolve_or_create_paper_recommendation_profile_concept_id(
        user_concept_id=user_concept_id,
        create_if_missing=True,
    )
    if not profile_concept_id:
        raise RuntimeError(
            f"Could not resolve or create profile concept for {user_concept_id}"
        )

    normalised_profile = _normalise_profile_payload(
        recommendation_profile,
        user_concept_id=user_concept_id,
        profile_concept_id=profile_concept_id,
    )
    normalised_profile["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = upsert_singleton_text_relation(
        subject_concept_id=profile_concept_id,
        predicate=PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID,
        lang=language,
        text=json.dumps(
            normalised_profile,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        policy=policy,
        provenance=dict(provenance) if isinstance(provenance, Mapping) else None,
        context=dict(context) if isinstance(context, Mapping) else None,
        garbage_collect=garbage_collect,
    )

    return {
        "success": True,
        "user_concept_id": user_concept_id,
        "profile_concept_id": profile_concept_id,
        "profile": normalised_profile,
        "text_relation": result,
    }


__all__ = [
    "MEMBER_OF_ORGANISATION_PREDICATE_ID",
    "PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID",
    "PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID",
    "PAPER_RECOMMENDATION_PROFILE_TYPE_ID",
    "PROFILE_SCHEMA_VERSION",
    "RESEARCH_INTEREST_PREDICATE_ID",
    "ensure_paper_recommendation_profile_primitives",
    "load_paper_recommendation_profile",
    "resolve_or_create_paper_recommendation_profile_concept_id",
    "upsert_paper_recommendation_profile",
]
