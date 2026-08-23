"""Vontology-backed paper recommendation profiles for recommendation subjects.

This service keeps recommendation-specific profile state small and explicit:
the broad predicate extent around a subject remains the substrate, while the
profile captures the stable, inspectable preference overlay that later
recommendation/ranking workflows can read directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .concept_type_closure_service import load_type_closure
from .paper_recommendation_policy_authority_service import (
    normalise_profile_fields,
    resolve_paper_recommendation_policy,
)
from .relationship_write_service import add_relationship
from .paper_recommendation_vontology_service import (
    persist_subject_paper_matching_profile,
)
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
PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID = "#V#paper_recommendation_profile_form"
PROFILE_HAS_FORM_PREDICATE_ID = "#V#profile_has_form"
PROFILE_TYPE_SALIENT_TO_PREDICATE_ID = "#V#profile_type_salient_to"
RESEARCHER_TYPE_ID = "#V#researcher"
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


def _normalise_concept_id(value: Any) -> str:
    return _safe_str(value)


def _normalise_concept_id_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = _normalise_concept_id(value)
        return [cleaned] if cleaned else []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _normalise_concept_id(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        rows.append(cleaned)
    return rows


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


def _normalise_subject_concept_id(
    *,
    subject_concept_id: str | None = None,
    user_concept_id: str | None = None,
) -> str:
    subject_id = _safe_str(subject_concept_id) or _safe_str(user_concept_id)
    if not subject_id:
        raise ValueError("subject_concept_id is required")
    return subject_id


def _build_profile_concept_id(subject_concept_id: str) -> str:
    subject_suffix = _safe_str(subject_concept_id)
    if subject_suffix.startswith("#V#"):
        subject_suffix = subject_suffix[3:]
    subject_suffix = subject_suffix or "unknown_subject"
    return f"#V#paper_recommendation_profile_for_{subject_suffix}"


def _profile_display_name(
    subject_doc: Mapping[str, Any],
    subject_concept_id: str,
) -> str:
    display_name = _safe_str(subject_doc.get("name")) or _safe_str(subject_concept_id)
    if display_name.startswith("#V#"):
        display_name = display_name[3:].replace("_", " ").strip()
    return f"Paper recommendation profile for {display_name or 'subject'}"


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


def _collect_type_and_ancestor_type_ids(type_ids: Sequence[str] | None) -> list[str]:
    return list(
        load_type_closure(_normalise_concept_id_list(type_ids)).get(
            "ordered_type_ids"
        )
        or []
    )


def _load_profile_type_salient_to_type_ids() -> list[str]:
    profile_type_doc = load_concept(PAPER_RECOMMENDATION_PROFILE_TYPE_ID)
    salient_to_type_ids = _normalise_concept_id_list(
        (profile_type_doc or {}).get("relationships", {}).get(
            PROFILE_TYPE_SALIENT_TO_PREDICATE_ID
        )
    )
    if salient_to_type_ids:
        return salient_to_type_ids
    return [RESEARCHER_TYPE_ID] if RESEARCHER_TYPE_ID else []


def _profile_applicability_payload(
    *,
    is_applicable: bool,
    applicability: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "is_applicable": is_applicable,
        "profile_type_id": PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
        "form_type_id": PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID,
        "direct_type_ids": applicability.get("direct_type_ids", []),
        "inherited_type_ids": applicability.get("inherited_type_ids", []),
        "salient_to_type_ids": applicability.get("salient_to_type_ids", []),
        "reasons": applicability.get("reasons", []),
    }


def is_subject_relevant_for_paper_recommendation_profile(
    *,
    subject_concept_id: str | None = None,
    user_concept_id: str | None = None,
    subject_doc: Mapping[str, Any] | None = None,
) -> bool:
    """Return true when a subject concept is (directly or indirectly) of a profile-relevant type."""
    return bool(
        _resolve_subject_profile_type_applicability(
            subject_concept_id=subject_concept_id,
            user_concept_id=user_concept_id,
            subject_doc=subject_doc,
        ).get("is_applicable")
    )


def _resolve_subject_profile_type_applicability(
    *,
    subject_concept_id: str | None = None,
    user_concept_id: str | None = None,
    subject_doc: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return profile applicability details for a subject concept."""

    subject_id = _safe_str(subject_concept_id) or _safe_str(user_concept_id)
    if not subject_id:
        if isinstance(subject_doc, Mapping):
            subject_id = _safe_str(subject_doc.get("concept_id"))
    if not subject_id:
        return {
            "is_applicable": False,
            "subject_concept_id": None,
            "direct_type_ids": [],
            "inherited_type_ids": [],
            "salient_to_type_ids": [],
            "reasons": ["missing_subject_concept_id"],
        }

    if subject_doc is None:
        subject_doc = _load_subject_doc(subject_id)

    direct_type_ids = _normalise_concept_id_list(
        subject_doc.get("relationships", {}).get("is_an_instance_of")
    )
    if not direct_type_ids:
        return {
            "is_applicable": False,
            "subject_concept_id": subject_id,
            "direct_type_ids": [],
            "inherited_type_ids": [],
            "salient_to_type_ids": [],
            "reasons": ["subject_has_no_instance_type"],
        }

    profile_target_type_ids = set(_load_profile_type_salient_to_type_ids())
    if not profile_target_type_ids:
        return {
            "is_applicable": False,
            "subject_concept_id": subject_id,
            "direct_type_ids": direct_type_ids,
            "inherited_type_ids": _collect_type_and_ancestor_type_ids(direct_type_ids),
            "salient_to_type_ids": [],
            "reasons": ["profile_type_has_no_salient_target_types"],
        }

    inherited_type_ids = set(_collect_type_and_ancestor_type_ids(direct_type_ids))
    is_applicable = bool(profile_target_type_ids.intersection(inherited_type_ids))
    return {
        "is_applicable": is_applicable,
        "subject_concept_id": subject_id,
        "direct_type_ids": direct_type_ids,
        "inherited_type_ids": sorted(inherited_type_ids),
        "salient_to_type_ids": sorted(profile_target_type_ids),
        "reasons": ([] if is_applicable else ["subject_not_subclass_of_salient_target"]),
    }


def ensure_paper_recommendation_profile_primitives() -> dict[str, Any]:
    """Ensure the recommendation-profile type and predicates exist."""

    created_or_repaired: list[str] = []
    try:
        _ensure_type_concept(
            concept_id=PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
            name="Paper recommendation profile",
            description=(
                "A recommendation-subject profile capturing explicit preferences and guidance "
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
        _ensure_type_concept(
            concept_id=PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID,
            name="Paper recommendation profile form",
            description=(
                "A UI-facing form concept for capturing the author's preferred "
                "paper-matching profile for a specific recommendation subject."
            ),
        )
        created_or_repaired.append(PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID)
        _ensure_predicate_concept(
            concept_id=PROFILE_HAS_FORM_PREDICATE_ID,
            name="Profile has form",
            description=(
                "Links a profile type to one of its canonical UI form types."
            ),
        )
        created_or_repaired.append(PROFILE_HAS_FORM_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PROFILE_TYPE_SALIENT_TO_PREDICATE_ID,
            name="Profile type salient to",
            description=(
                "Relates a paper recommendation profile type to concept types that "
                "should render and own that profile form."
            ),
        )
        created_or_repaired.append(PROFILE_TYPE_SALIENT_TO_PREDICATE_ID)

        profile_type_doc = load_concept(PAPER_RECOMMENDATION_PROFILE_TYPE_ID)
        if profile_type_doc is not None:
            existing_form_target_ids = normalise_relationship_targets(
                (profile_type_doc.get("relationships") or {}).get(
                    PROFILE_HAS_FORM_PREDICATE_ID
                )
            )
            if PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID not in existing_form_target_ids:
                add_relationship(
                    source_id=PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
                    predicate=PROFILE_HAS_FORM_PREDICATE_ID,
                    target=PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID,
                )

            existing_salient_targets = normalise_relationship_targets(
                (profile_type_doc.get("relationships") or {}).get(
                    PROFILE_TYPE_SALIENT_TO_PREDICATE_ID
                )
            )
            if RESEARCHER_TYPE_ID and RESEARCHER_TYPE_ID not in existing_salient_targets:
                add_relationship(
                    source_id=PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
                    predicate=PROFILE_TYPE_SALIENT_TO_PREDICATE_ID,
                    target=RESEARCHER_TYPE_ID,
                )
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


def _derived_context_for_subject(subject_doc: Mapping[str, Any]) -> dict[str, Any]:
    relationships = dict(subject_doc.get("relationships") or {})
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
    subject_concept_id: str,
    profile_concept_id: str,
) -> dict[str, Any]:
    policy = resolve_paper_recommendation_policy()
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": _safe_str(profile_concept_id) or profile_concept_id,
        "subject_concept_id": subject_concept_id,
        "owner_subject_concept_id": subject_concept_id,
        "owner_person_concept_id": subject_concept_id,
        "profile_concept_id": profile_concept_id,
        "updated_at": None,
    }
    profile.update(normalise_profile_fields({}, policy=policy))
    return profile


def _normalise_profile_payload(
    raw_profile: Mapping[str, Any] | None,
    *,
    subject_concept_id: str,
    profile_concept_id: str,
) -> dict[str, Any]:
    base = _empty_profile(
        subject_concept_id=subject_concept_id,
        profile_concept_id=profile_concept_id,
    )
    if not isinstance(raw_profile, Mapping):
        return base

    base.update(
        normalise_profile_fields(
            raw_profile,
            policy=resolve_paper_recommendation_policy(),
        )
    )
    base["updated_at"] = _safe_str(raw_profile.get("updated_at")) or None
    return base


def _load_subject_doc(subject_concept_id: str) -> Mapping[str, Any]:
    concept_doc = get_concept_by_concept_id(subject_concept_id)
    if not isinstance(concept_doc, Mapping):
        raise ConceptNotFoundError(f"Subject concept not found: {subject_concept_id}")
    return concept_doc


def resolve_or_create_paper_recommendation_profile_concept_id(
    *,
    subject_concept_id: str | None = None,
    user_concept_id: str | None = None,
    create_if_missing: bool = False,
) -> str | None:
    """Resolve the linked profile concept for a subject, optionally creating it."""

    subject_id = _normalise_subject_concept_id(
        subject_concept_id=subject_concept_id,
        user_concept_id=user_concept_id,
    )
    subject_doc = _load_subject_doc(subject_id)
    if create_if_missing and not is_subject_relevant_for_paper_recommendation_profile(
        subject_doc=subject_doc,
        subject_concept_id=subject_id,
    ):
        return None

    ensure_report = ensure_paper_recommendation_profile_primitives()
    if not ensure_report.get("success"):
        raise RuntimeError(
            f"Failed to ensure recommendation profile primitives: {ensure_report.get('error')}"
        )
    relationships = dict(subject_doc.get("relationships") or {})
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
        ensure_instance_typing(
            concept_id=candidate_id,
            type_ids=(PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID,),
        )
        return candidate_id

    stable_profile_id = _build_profile_concept_id(subject_id)
    stable_profile_doc = load_concept(stable_profile_id)
    if stable_profile_doc is not None:
        ensure_instance_typing(
            concept_id=stable_profile_id,
            type_ids=(PAPER_RECOMMENDATION_PROFILE_TYPE_ID,),
        )
        ensure_instance_typing(
            concept_id=stable_profile_id,
            type_ids=(PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID,),
        )
        add_relationship(
            source_id=subject_id,
            predicate=PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID,
            target=stable_profile_id,
        )
        return stable_profile_id

    if not create_if_missing:
        return None

    concept_service.create_concept(
        name=_profile_display_name(subject_doc, subject_id),
        concept_id=stable_profile_id,
        description=(
            "User-editable paper recommendation profile used by recommendation "
            "and explanation workflows."
        ),
        parent_concept_ids=[
            PAPER_RECOMMENDATION_PROFILE_TYPE_ID,
            PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID,
        ],
        create_as_instance=True,
    )
    add_relationship(
        source_id=subject_id,
        predicate=PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID,
        target=stable_profile_id,
    )
    return stable_profile_id


def load_paper_recommendation_profile(
    *,
    subject_concept_id: str | None = None,
    user_concept_id: str | None = None,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    """Load one subject's paper recommendation profile plus derived context."""

    subject_id = _normalise_subject_concept_id(
        subject_concept_id=subject_concept_id,
        user_concept_id=user_concept_id,
    )
    subject_doc = _load_subject_doc(subject_id)
    applicability = _resolve_subject_profile_type_applicability(
        subject_doc=subject_doc,
        subject_concept_id=subject_id,
    )
    is_applicable = bool(applicability.get("is_applicable"))
    if not is_applicable:
        return {
            "success": True,
            "subject_concept_id": subject_id,
            "user_concept_id": subject_id,
            "profile_concept_id": _build_profile_concept_id(subject_id),
            "profile": _empty_profile(
                subject_concept_id=subject_id,
                profile_concept_id=_build_profile_concept_id(subject_id),
            ),
            "derived_context": {},
            "diagnostics": {
                "source_predicate": None,
                "profile_exists": False,
                "profile_materialised": False,
            },
            "profile_applicability": {
                **_profile_applicability_payload(
                    is_applicable=False,
                    applicability=applicability,
                ),
            },
        }

    profile_concept_id = resolve_or_create_paper_recommendation_profile_concept_id(
        subject_concept_id=subject_id,
        create_if_missing=create_if_missing,
    )
    profile_materialised = bool(profile_concept_id)
    if not profile_concept_id:
        profile_concept_id = _build_profile_concept_id(subject_id)

    loaded_profile: dict[str, Any] | None = None
    source_predicate: str | None = None
    if profile_materialised:
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
                        subject_concept_id=subject_id,
                        profile_concept_id=profile_concept_id,
                    )
                    source_predicate = predicate
                    break
            if loaded_profile is not None:
                break

    if loaded_profile is None:
        loaded_profile = _empty_profile(
            subject_concept_id=subject_id,
            profile_concept_id=profile_concept_id,
        )
        if create_if_missing and profile_materialised:
            upsert_result = upsert_paper_recommendation_profile(
                subject_concept_id=subject_id,
                recommendation_profile=loaded_profile,
            )
            if isinstance(upsert_result.get("profile"), Mapping):
                loaded_profile = dict(upsert_result["profile"])
            source_predicate = PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID

    return {
        "success": True,
        "subject_concept_id": subject_id,
        "user_concept_id": subject_id,
        "profile_concept_id": profile_concept_id,
        "profile": loaded_profile,
        "profile_applicability": _profile_applicability_payload(
            is_applicable=is_applicable,
            applicability=applicability,
        ),
        "derived_context": _derived_context_for_subject(subject_doc),
        "diagnostics": {
            "source_predicate": source_predicate,
            "profile_exists": bool(source_predicate),
            "profile_materialised": profile_materialised,
        },
    }


def upsert_paper_recommendation_profile(
    *,
    subject_concept_id: str | None = None,
    user_concept_id: str | None = None,
    recommendation_profile: Mapping[str, Any] | None,
    language: str = "en-NZ",
    policy: str = "replace_others",
    garbage_collect: bool = True,
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one subject's paper recommendation profile."""

    subject_id = _normalise_subject_concept_id(
        subject_concept_id=subject_concept_id,
        user_concept_id=user_concept_id,
    )
    subject_doc = _load_subject_doc(subject_id)
    if not is_subject_relevant_for_paper_recommendation_profile(
        subject_doc=subject_doc,
        subject_concept_id=subject_id,
    ):
        raise ValueError(
            "Paper recommendation profile is only supported for concepts that are instances of researcher-like types."
        )

    profile_concept_id = resolve_or_create_paper_recommendation_profile_concept_id(
        subject_concept_id=subject_id,
        create_if_missing=True,
    )
    if not profile_concept_id:
        raise RuntimeError(
            f"Could not resolve or create profile concept for {subject_id}"
        )

    normalised_profile = _normalise_profile_payload(
        recommendation_profile,
        subject_concept_id=subject_id,
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
    generic_profile_result = persist_subject_paper_matching_profile(
        subject_concept_id=subject_id,
        profile=normalised_profile,
        provenance=dict(provenance) if isinstance(provenance, Mapping) else None,
        context=dict(context) if isinstance(context, Mapping) else None,
    )

    return {
        "success": True,
        "subject_concept_id": subject_id,
        "user_concept_id": subject_id,
        "profile_concept_id": profile_concept_id,
        "profile": normalised_profile,
        "text_relation": result,
        "generic_subject_profile": generic_profile_result,
    }


__all__ = [
    "MEMBER_OF_ORGANISATION_PREDICATE_ID",
    "PAPER_RECOMMENDATION_PROFILE_JSON_PREDICATE_ID",
    "PAPER_RECOMMENDATION_PROFILE_LINK_PREDICATE_ID",
    "PAPER_RECOMMENDATION_PROFILE_TYPE_ID",
    "PAPER_RECOMMENDATION_PROFILE_FORM_TYPE_ID",
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_HAS_FORM_PREDICATE_ID",
    "PROFILE_TYPE_SALIENT_TO_PREDICATE_ID",
    "RESEARCHER_TYPE_ID",
    "RESEARCH_INTEREST_PREDICATE_ID",
    "ensure_paper_recommendation_profile_primitives",
    "load_paper_recommendation_profile",
    "is_subject_relevant_for_paper_recommendation_profile",
    "resolve_or_create_paper_recommendation_profile_concept_id",
    "upsert_paper_recommendation_profile",
]
