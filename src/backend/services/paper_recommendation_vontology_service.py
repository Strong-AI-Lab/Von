"""Vontology storage surfaces for semantic paper recommendation materialisation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from . import concept_service
from .concept_service import get_concept_by_concept_id_exact
from .paper_recommendation_constants import (
    GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
    HAS_PAPER_RECOMMENDATION_ASSERTION_PREDICATE_ID,
    HAS_PAPER_RECOMMENDATION_FEEDBACK_PREDICATE_ID,
    PAPER_RECOMMENDATION_ASSERTION_TYPE_ID,
    PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID,
    PAPER_RECOMMENDATION_ASSERTS_SUBJECT_PREDICATE_ID,
    PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID,
    PAPER_RECOMMENDATION_FEEDBACK_ASSERTION_PREDICATE_ID,
    PAPER_RECOMMENDATION_FEEDBACK_JSON_PREDICATE_ID,
    PAPER_RECOMMENDATION_FEEDBACK_PAPER_PREDICATE_ID,
    PAPER_RECOMMENDATION_FEEDBACK_PROFILE_PREDICATE_ID,
    PAPER_RECOMMENDATION_FEEDBACK_SUBJECT_PREDICATE_ID,
    PAPER_RECOMMENDATION_FEEDBACK_TYPE_ID,
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
_FEEDBACK_SCORE_BY_LABEL: dict[str, float] = {
    "not_useful": -1.0,
    "partly_useful": 0.0,
    "useful": 1.0,
}
_FEEDBACK_LABEL_ALIASES: dict[str, str] = {
    "bad": "not_useful",
    "negative": "not_useful",
    "no": "not_useful",
    "not helpful": "not_useful",
    "not useful": "not_useful",
    "not_useful": "not_useful",
    "unhelpful": "not_useful",
    "useless": "not_useful",
    "-1": "not_useful",
    "0": "partly_useful",
    "mixed": "partly_useful",
    "neutral": "partly_useful",
    "partial": "partly_useful",
    "partly": "partly_useful",
    "partly useful": "partly_useful",
    "partly_useful": "partly_useful",
    "somewhat": "partly_useful",
    "good": "useful",
    "helpful": "useful",
    "positive": "useful",
    "useful": "useful",
    "very useful": "useful",
    "yes": "useful",
    "1": "useful",
}


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


def _normalise_feedback_label(value: Any) -> tuple[str | None, float | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        label = "useful" if value else "not_useful"
        return label, _FEEDBACK_SCORE_BY_LABEL[label]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value) > 0:
            label = "useful"
        elif float(value) < 0:
            label = "not_useful"
        else:
            label = "partly_useful"
        return label, _FEEDBACK_SCORE_BY_LABEL[label]
    cleaned = _safe_str(value).replace("_", " ").replace("-", " ").casefold()
    if not cleaned:
        return None, None
    alias = _FEEDBACK_LABEL_ALIASES.get(cleaned)
    if alias is None:
        return None, None
    return alias, _FEEDBACK_SCORE_BY_LABEL[alias]


def _feedback_display_name(*, paper_title: str, subject_label: str) -> str:
    if paper_title and subject_label:
        return f"Paper recommendation feedback: {paper_title} for {subject_label}"
    if paper_title:
        return f"Paper recommendation feedback: {paper_title}"
    return "Paper recommendation feedback"


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
    """Ensure recommendation assertion/profile/feedback primitives exist."""

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
        _ensure_type_concept(
            concept_id=PAPER_RECOMMENDATION_FEEDBACK_TYPE_ID,
            name="Paper recommendation feedback",
            description=(
                "A user-provided graded assessment of a paper recommendation and "
                "its explanation, captured with enough context for later "
                "evaluation and learning workflows."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_FEEDBACK_TYPE_ID)
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
        _ensure_predicate_concept(
            concept_id=HAS_PAPER_RECOMMENDATION_FEEDBACK_PREDICATE_ID,
            name="Has paper recommendation feedback",
            description=(
                "Links a recommendation subject to captured user feedback about "
                "one paper recommendation."
            ),
        )
        ensured.append(HAS_PAPER_RECOMMENDATION_FEEDBACK_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_FEEDBACK_ASSERTION_PREDICATE_ID,
            name="Recommendation feedback assertion",
            description=(
                "Links a paper recommendation feedback concept to the specific "
                "recommendation assertion it evaluates."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_FEEDBACK_ASSERTION_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_FEEDBACK_PAPER_PREDICATE_ID,
            name="Recommendation feedback paper",
            description=(
                "Links a paper recommendation feedback concept to the scholarly "
                "paper it discusses."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_FEEDBACK_PAPER_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_FEEDBACK_SUBJECT_PREDICATE_ID,
            name="Recommendation feedback subject",
            description=(
                "Links a paper recommendation feedback concept to the subject "
                "for whom the recommendation was evaluated."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_FEEDBACK_SUBJECT_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_FEEDBACK_PROFILE_PREDICATE_ID,
            name="Recommendation feedback profile",
            description=(
                "Links a paper recommendation feedback concept to the profile "
                "context used when the recommendation was evaluated."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_FEEDBACK_PROFILE_PREDICATE_ID)
        _ensure_predicate_concept(
            concept_id=PAPER_RECOMMENDATION_FEEDBACK_JSON_PREDICATE_ID,
            name="Has paper recommendation feedback JSON",
            description=(
                "Stores canonical structured feedback about recommendation "
                "usefulness, explanation usefulness, and optional free-text notes."
            ),
        )
        ensured.append(PAPER_RECOMMENDATION_FEEDBACK_JSON_PREDICATE_ID)
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


def _load_latest_json_payload(
    subject_concept_id: str,
    predicate: str,
) -> dict[str, Any] | None:
    raw_json = _load_latest_text(subject_concept_id, predicate)
    if not raw_json:
        return None
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    return dict(parsed)


def record_paper_recommendation_feedback(
    *,
    actor_user_concept_id: str,
    subject_concept_id: str | None = None,
    assertion_concept_id: str | None = None,
    paper_concept_id: str | None = None,
    recommendation_usefulness: Any = None,
    explanation_usefulness: Any = None,
    feedback_text: str | None = None,
    capture_surface: str | None = None,
    conversation_session_id: str | None = None,
    request_id: str | None = None,
    organisation_concept_id: str | None = None,
    profile_concept_id: str | None = None,
) -> dict[str, Any]:
    """Persist one user feedback record about a paper recommendation."""

    ensure_report = ensure_paper_recommendation_primitives()
    if not ensure_report.get("success"):
        raise RuntimeError(
            f"Failed to ensure recommendation primitives: {ensure_report.get('error')}"
        )

    actor_id = _safe_str(actor_user_concept_id)
    if not actor_id:
        raise ValueError("actor_user_concept_id is required")

    explicit_subject_id = _safe_str(subject_concept_id) or None
    explicit_assertion_id = _safe_str(assertion_concept_id) or None
    explicit_paper_id = _safe_str(paper_concept_id) or None
    explicit_profile_id = _safe_str(profile_concept_id) or None

    recommendation_label, recommendation_score = _normalise_feedback_label(
        recommendation_usefulness
    )
    if recommendation_usefulness is not None and recommendation_label is None:
        raise ValueError(
            "recommendation_usefulness must be one of: useful, partly_useful, not_useful"
        )

    explanation_label, explanation_score = _normalise_feedback_label(
        explanation_usefulness
    )
    if explanation_usefulness is not None and explanation_label is None:
        raise ValueError(
            "explanation_usefulness must be one of: useful, partly_useful, not_useful"
        )

    free_text_feedback = _safe_str(feedback_text) or None
    if (
        recommendation_label is None
        and explanation_label is None
        and free_text_feedback is None
    ):
        raise ValueError(
            "At least one of recommendation_usefulness, explanation_usefulness, or feedback_text is required"
        )

    resolved_subject_id = explicit_subject_id
    resolved_assertion_id = explicit_assertion_id
    resolved_paper_id = explicit_paper_id
    resolved_profile_id = explicit_profile_id

    assertion_doc = (
        load_concept(resolved_assertion_id) if isinstance(resolved_assertion_id, str) else None
    )
    if resolved_assertion_id and assertion_doc is None:
        raise ValueError("assertion_concept_id does not refer to a recommendation assertion")

    if assertion_doc is None and resolved_subject_id and resolved_paper_id:
        candidate_assertion_id = build_paper_recommendation_assertion_concept_id(
            subject_concept_id=resolved_subject_id,
            paper_concept_id=resolved_paper_id,
        )
        candidate_assertion_doc = load_concept(candidate_assertion_id)
        if candidate_assertion_doc is not None:
            resolved_assertion_id = candidate_assertion_id
            assertion_doc = candidate_assertion_doc

    if assertion_doc is None:
        raise ValueError(
            "Feedback must target an existing materialised recommendation assertion"
        )

    evaluation_payload: dict[str, Any] | None = None
    if assertion_doc is not None:
        if resolved_assertion_id is None:
            raise ValueError("assertion_concept_id resolution unexpectedly failed")
        assertion_relationships = dict(assertion_doc.get("relationships") or {})
        assertion_subject_ids = normalise_relationship_targets(
            assertion_relationships.get(PAPER_RECOMMENDATION_ASSERTS_SUBJECT_PREDICATE_ID)
        )
        assertion_paper_ids = normalise_relationship_targets(
            assertion_relationships.get(PAPER_RECOMMENDATION_ASSERTS_PAPER_PREDICATE_ID)
        )
        assertion_subject_id = assertion_subject_ids[0] if assertion_subject_ids else None
        assertion_paper_id = assertion_paper_ids[0] if assertion_paper_ids else None
        if resolved_subject_id and assertion_subject_id and resolved_subject_id != assertion_subject_id:
            raise ValueError("subject_concept_id does not match assertion_concept_id")
        if resolved_paper_id and assertion_paper_id and resolved_paper_id != assertion_paper_id:
            raise ValueError("paper_concept_id does not match assertion_concept_id")
        resolved_subject_id = resolved_subject_id or assertion_subject_id
        resolved_paper_id = resolved_paper_id or assertion_paper_id
        evaluation_payload = _load_latest_json_payload(
            resolved_assertion_id,
            PAPER_RECOMMENDATION_EVALUATION_JSON_PREDICATE_ID,
        )
        if resolved_profile_id is None and isinstance(evaluation_payload, Mapping):
            resolved_profile_id = (
                _safe_str(evaluation_payload.get("subject_profile_concept_id")) or None
            )

    if resolved_subject_id is None:
        raise ValueError(
            "subject_concept_id is required unless assertion_concept_id resolves the recommendation subject"
        )
    if resolved_paper_id is None:
        raise ValueError(
            "paper_concept_id is required unless assertion_concept_id resolves the recommendation paper"
        )

    subject_doc = load_concept(resolved_subject_id)
    if subject_doc is None:
        raise ValueError("subject_concept_id does not refer to an accessible concept")

    paper_doc = get_concept_by_concept_id_exact(resolved_paper_id) or {}
    paper_title = _safe_str(paper_doc.get("name")) or resolved_paper_id
    subject_label = _safe_str(subject_doc.get("name")) or resolved_subject_id

    if resolved_profile_id is None:
        profile_payload = load_subject_paper_matching_profile(resolved_subject_id)
        if profile_payload.get("success"):
            resolved_profile_id = (
                _safe_str(profile_payload.get("profile_concept_id")) or None
            )
    if resolved_profile_id:
        profile_doc = load_concept(resolved_profile_id)
        if profile_doc is None:
            if explicit_profile_id:
                raise ValueError("profile_concept_id does not refer to an accessible concept")
            resolved_profile_id = None

    feedback_concept_id = f"#V#paper_recommendation_feedback_{uuid.uuid4().hex}"
    concept_service.create_concept(
        name=_feedback_display_name(
            paper_title=paper_title,
            subject_label=subject_label,
        ),
        concept_id=feedback_concept_id,
        description=(
            "Captured user feedback about a paper recommendation and the "
            "helpfulness of its explanation."
        ),
        parent_concept_ids=[PAPER_RECOMMENDATION_FEEDBACK_TYPE_ID],
        create_as_instance=True,
        created_by_concept_id=actor_id,
        organisation_concept_id=_safe_str(organisation_concept_id) or None,
    )

    add_relationship(
        source_id=resolved_subject_id,
        predicate=HAS_PAPER_RECOMMENDATION_FEEDBACK_PREDICATE_ID,
        target=feedback_concept_id,
    )
    add_relationship(
        source_id=feedback_concept_id,
        predicate=PAPER_RECOMMENDATION_FEEDBACK_SUBJECT_PREDICATE_ID,
        target=resolved_subject_id,
    )
    add_relationship(
        source_id=feedback_concept_id,
        predicate=PAPER_RECOMMENDATION_FEEDBACK_PAPER_PREDICATE_ID,
        target=resolved_paper_id,
    )
    if resolved_assertion_id:
        add_relationship(
            source_id=feedback_concept_id,
            predicate=PAPER_RECOMMENDATION_FEEDBACK_ASSERTION_PREDICATE_ID,
            target=resolved_assertion_id,
        )
    if resolved_profile_id:
        add_relationship(
            source_id=feedback_concept_id,
            predicate=PAPER_RECOMMENDATION_FEEDBACK_PROFILE_PREDICATE_ID,
            target=resolved_profile_id,
        )

    recorded_at = _utc_now_iso()
    feedback_payload = {
        "schema_version": "paper_recommendation_feedback.v1",
        "feedback_concept_id": feedback_concept_id,
        "recorded_at": recorded_at,
        "capture_surface": _safe_str(capture_surface) or "conversation",
        "actor_user_concept_id": actor_id,
        "subject_concept_id": resolved_subject_id,
        "paper_concept_id": resolved_paper_id,
        "assertion_concept_id": resolved_assertion_id,
        "profile_concept_id": resolved_profile_id,
        "paper_title": paper_title,
        "recommendation_usefulness_label": recommendation_label,
        "recommendation_usefulness_score": recommendation_score,
        "explanation_usefulness_label": explanation_label,
        "explanation_usefulness_score": explanation_score,
        "free_text_feedback": free_text_feedback,
        "conversation_session_id": _safe_str(conversation_session_id) or None,
        "request_id": _safe_str(request_id) or None,
        "recommendation_policy_version": (
            _safe_str(evaluation_payload.get("policy_version"))
            if isinstance(evaluation_payload, Mapping)
            else None
        )
        or None,
        "recommendation_decision_mode": (
            _safe_str(evaluation_payload.get("decision_mode"))
            if isinstance(evaluation_payload, Mapping)
            else None
        )
        or None,
        "recommendation_trigger_source": (
            _safe_str(evaluation_payload.get("trigger_source"))
            if isinstance(evaluation_payload, Mapping)
            else None
        )
        or None,
        "recommendation_evaluated_at": (
            _safe_str(evaluation_payload.get("updated_at"))
            if isinstance(evaluation_payload, Mapping)
            else None
        )
        or None,
    }
    provenance = {
        "source": _safe_str(capture_surface) or "conversation",
        "actor_user_concept_id": actor_id,
    }
    if isinstance(conversation_session_id, str) and conversation_session_id.strip():
        provenance["conversation_session_id"] = conversation_session_id.strip()
    if isinstance(request_id, str) and request_id.strip():
        provenance["request_id"] = request_id.strip()

    feedback_json_relation = upsert_singleton_text_relation(
        subject_concept_id=feedback_concept_id,
        predicate=PAPER_RECOMMENDATION_FEEDBACK_JSON_PREDICATE_ID,
        lang=_DEFAULT_LANG,
        text=json.dumps(
            feedback_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        provenance=provenance,
        context={
            "assertion_concept_id": resolved_assertion_id,
            "paper_concept_id": resolved_paper_id,
            "subject_concept_id": resolved_subject_id,
        },
        policy="replace_others",
        garbage_collect=True,
    )

    note_relation = None
    if free_text_feedback:
        note_relation = upsert_singleton_text_relation(
            subject_concept_id=feedback_concept_id,
            predicate="hasNote",
            lang=_DEFAULT_LANG,
            text=free_text_feedback,
            provenance=provenance,
            context={
                "assertion_concept_id": resolved_assertion_id,
                "paper_concept_id": resolved_paper_id,
                "subject_concept_id": resolved_subject_id,
            },
            policy="replace_others",
            garbage_collect=True,
        )

    return {
        "success": True,
        "feedback_concept_id": feedback_concept_id,
        "actor_user_concept_id": actor_id,
        "subject_concept_id": resolved_subject_id,
        "paper_concept_id": resolved_paper_id,
        "assertion_concept_id": resolved_assertion_id,
        "profile_concept_id": resolved_profile_id,
        "feedback_payload": feedback_payload,
        "feedback_json_text_relation": feedback_json_relation,
        "note_text_relation": note_relation,
    }


def list_paper_recommendation_feedback(
    *,
    subject_concept_id: str | None = None,
    assertion_concept_id: str | None = None,
    paper_concept_id: str | None = None,
    profile_concept_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Load captured feedback rows for recommendation assertions or subjects."""

    query: dict[str, Any] = {
        "relationships.is_an_instance_of": PAPER_RECOMMENDATION_FEEDBACK_TYPE_ID,
    }
    clean_subject_id = _safe_str(subject_concept_id) or None
    clean_assertion_id = _safe_str(assertion_concept_id) or None
    clean_paper_id = _safe_str(paper_concept_id) or None
    clean_profile_id = _safe_str(profile_concept_id) or None
    if clean_subject_id:
        query[
            f"relationships.{PAPER_RECOMMENDATION_FEEDBACK_SUBJECT_PREDICATE_ID}"
        ] = clean_subject_id
    if clean_assertion_id:
        query[
            f"relationships.{PAPER_RECOMMENDATION_FEEDBACK_ASSERTION_PREDICATE_ID}"
        ] = clean_assertion_id
    if clean_paper_id:
        query[f"relationships.{PAPER_RECOMMENDATION_FEEDBACK_PAPER_PREDICATE_ID}"] = (
            clean_paper_id
        )
    if clean_profile_id:
        query[
            f"relationships.{PAPER_RECOMMENDATION_FEEDBACK_PROFILE_PREDICATE_ID}"
        ] = clean_profile_id
    if len(query) == 1:
        raise ValueError(
            "At least one of subject_concept_id, assertion_concept_id, paper_concept_id, or profile_concept_id is required"
        )

    try:
        safe_limit = int(limit or 50)
    except (TypeError, ValueError):
        safe_limit = 50
    safe_limit = max(1, min(safe_limit, 200))
    cursor = ConceptsRepository.find(
        query,
        projection={"concept_id": 1, "name": 1},
        limit=safe_limit,
    )

    rows: list[dict[str, Any]] = []
    for row in cursor:
        if not isinstance(row, Mapping):
            continue
        feedback_concept_id = _safe_str(row.get("concept_id"))
        if not feedback_concept_id:
            continue
        payload = _load_latest_json_payload(
            feedback_concept_id,
            PAPER_RECOMMENDATION_FEEDBACK_JSON_PREDICATE_ID,
        )
        if payload is None:
            continue
        note_text = _load_latest_text(feedback_concept_id, "hasNote")
        rows.append(
            {
                "feedback_concept_id": feedback_concept_id,
                "feedback_name": _safe_str(row.get("name")) or feedback_concept_id,
                "feedback_payload": payload,
                "free_text_feedback": _safe_str(note_text) or payload.get("free_text_feedback"),
            }
        )

    rows.sort(
        key=lambda item: _safe_str(
            (item.get("feedback_payload") or {}).get("recorded_at")
        ),
        reverse=True,
    )
    return {
        "success": True,
        "count": len(rows),
        "feedback": rows,
        "subject_concept_id": clean_subject_id,
        "assertion_concept_id": clean_assertion_id,
        "paper_concept_id": clean_paper_id,
        "profile_concept_id": clean_profile_id,
    }


__all__ = [
    "build_paper_recommendation_assertion_concept_id",
    "ensure_paper_recommendation_primitives",
    "list_subject_concept_ids_with_paper_matching_profiles",
    "list_paper_recommendation_feedback",
    "load_materialised_paper_recommendations",
    "load_subject_paper_matching_profile",
    "persist_subject_paper_matching_profile",
    "record_paper_recommendation_feedback",
    "resolve_subject_ids_for_legacy_profile_concept",
    "upsert_paper_recommendation_assertion",
]
