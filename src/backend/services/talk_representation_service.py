"""Materialise and verify talk / presentation concepts for durable workflows."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from . import concept_service
from . import concept_search_service
from .arxiv_paper_link_service import resolve_or_create_scholarly_author_concept_id
from .relationship_write_service import add_relationship
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .workflow_vontology_materialisation_helpers import (
    ensure_instance_typing,
    load_concept,
    normalise_relationship_targets,
    stable_named_instance_concept_id,
)
from ..utils.concept_id_utils import canonicalise_vontology_concept_id

logger = logging.getLogger(__name__)

PRESENTATION_TYPE_ID = "#V#presentation"
SCIENTIFIC_PRESENTATION_TYPE_ID = "#V#scientific_presentation"
SEMINAR_TYPE_ID = "#V#seminar"
PRESENTER_OF_PRESENTATION_PREDICATE_ID = "#V#presenter_of_presentation"
HAS_START_TIME_PREDICATE_ID = "#V#has_start_time"
PRESENTATION_MEETING_LINK_PREDICATE_ID = "#V#presentation_has_meeting_link"
PRESENTATION_MEETING_ID_PREDICATE_ID = "#V#presentation_has_meeting_id"
PRESENTATION_MEETING_PASSCODE_PREDICATE_ID = "#V#presentation_has_meeting_passcode"
PRESENTATION_STATUS_PREDICATE_ID = "#V#presentation_status"
_PREDICATE_TYPE_IDS: tuple[str, ...] = ("#V#predicate", "#V#binary_predicate")

GENERIC_TALK_VERIFICATION_PROFILE = "generic_talk"
TECHNICAL_SCIENTIFIC_TALK_VERIFICATION_PROFILE = "technical_scientific_talk"
ACADEMIC_PRESENTATION_VERIFICATION_PROFILE = "academic_presentation"


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _first_non_empty_text(*values: Any) -> str | None:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [item.strip() for item in value.split(",")]
        return [item for item in parts if item]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    values: list[str] = []
    for item in value:
        cleaned = _clean_text(item)
        if cleaned:
            values.append(cleaned)
    return values


def _dedupe_casefold(values: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        fingerprint = cleaned.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(cleaned)
    return deduped


def _normalise_verification_profile(value: Any) -> str:
    profile = _clean_text(value).lower()
    if profile == TECHNICAL_SCIENTIFIC_TALK_VERIFICATION_PROFILE:
        return TECHNICAL_SCIENTIFIC_TALK_VERIFICATION_PROFILE
    if profile == ACADEMIC_PRESENTATION_VERIFICATION_PROFILE:
        return ACADEMIC_PRESENTATION_VERIFICATION_PROFILE
    return GENERIC_TALK_VERIFICATION_PROFILE


def _required_type_ids_for_profile(profile: str) -> tuple[str, ...]:
    if profile == TECHNICAL_SCIENTIFIC_TALK_VERIFICATION_PROFILE:
        return (PRESENTATION_TYPE_ID, SCIENTIFIC_PRESENTATION_TYPE_ID)
    if profile == ACADEMIC_PRESENTATION_VERIFICATION_PROFILE:
        return (
            PRESENTATION_TYPE_ID,
            SCIENTIFIC_PRESENTATION_TYPE_ID,
            SEMINAR_TYPE_ID,
        )
    return (PRESENTATION_TYPE_ID,)


def _normalise_type_ids(raw_type_ids: Any, *, verification_profile: str) -> list[str]:
    requested = _coerce_string_list(raw_type_ids)
    canonical_ids: list[str] = []
    for raw in requested:
        canonical = canonicalise_vontology_concept_id(raw) or raw
        if canonical.startswith("#V#"):
            canonical_ids.append(canonical)
    canonical_ids.extend(_required_type_ids_for_profile(verification_profile))
    return _dedupe_casefold(canonical_ids)


def _build_presentation_display_name(
    *,
    title: str | None,
    speaker_name: str | None,
    start_time: str | None,
    presentation_concept_id: str | None,
) -> str:
    title_text = _clean_text(title)
    speaker_text = _clean_text(speaker_name)
    start_time_text = _clean_text(start_time)

    if title_text and speaker_text:
        base = f"{speaker_text} - {title_text}"
    else:
        base = title_text or speaker_text or ""

    if start_time_text:
        if base:
            return f"{base} ({start_time_text})"
        return start_time_text

    if base:
        return base

    if _clean_text(presentation_concept_id):
        return _clean_text(presentation_concept_id)

    raise ValueError("talk_representation_display_name_missing")


def _ensure_type_concept(
    *,
    concept_id: str,
    name: str,
    parent_type_ids: Sequence[str] = (),
) -> None:
    desired_parent_ids = _dedupe_casefold(
        [
            canonicalise_vontology_concept_id(parent_id) or parent_id
            for parent_id in parent_type_ids
            if _clean_text(parent_id)
        ]
    )
    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            parent_concept_ids=list(desired_parent_ids),
            create_as_instance=False,
        )
        return

    relationships = dict(concept_doc.get("relationships") or {})
    existing_parent_ids = normalise_relationship_targets(relationships.get("is_a_type_of"))
    merged_parent_ids = list(existing_parent_ids)
    for parent_id in desired_parent_ids:
        if parent_id not in merged_parent_ids:
            merged_parent_ids.append(parent_id)
    if merged_parent_ids != existing_parent_ids:
        relationships["is_a_type_of"] = merged_parent_ids
        concept_service.update_concept(concept_id, {"relationships": relationships})


def _ensure_predicate_concept(*, concept_id: str, name: str) -> None:
    concept_doc = load_concept(concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            parent_concept_ids=["#V#predicate"],
            create_as_instance=True,
        )
        return
    ensure_instance_typing(
        concept_id=concept_id,
        type_ids=("#V#predicate",),
        remove_type_parent_ids=_PREDICATE_TYPE_IDS,
    )


def ensure_talk_representation_primitives() -> None:
    """Ensure required talk types and predicates exist and are typed correctly."""

    _ensure_type_concept(
        concept_id=PRESENTATION_TYPE_ID,
        name="Presentation",
    )
    _ensure_type_concept(
        concept_id=SCIENTIFIC_PRESENTATION_TYPE_ID,
        name="Scientific Presentation",
        parent_type_ids=(PRESENTATION_TYPE_ID,),
    )
    _ensure_type_concept(
        concept_id=SEMINAR_TYPE_ID,
        name="Seminar",
        parent_type_ids=(PRESENTATION_TYPE_ID,),
    )
    _ensure_predicate_concept(
        concept_id=PRESENTER_OF_PRESENTATION_PREDICATE_ID,
        name="Presenter Of Presentation",
    )
    _ensure_predicate_concept(
        concept_id=HAS_START_TIME_PREDICATE_ID,
        name="Has Start Time",
    )
    _ensure_predicate_concept(
        concept_id=PRESENTATION_MEETING_LINK_PREDICATE_ID,
        name="Presentation Has Meeting Link",
    )
    _ensure_predicate_concept(
        concept_id=PRESENTATION_MEETING_ID_PREDICATE_ID,
        name="Presentation Has Meeting ID",
    )
    _ensure_predicate_concept(
        concept_id=PRESENTATION_MEETING_PASSCODE_PREDICATE_ID,
        name="Presentation Has Meeting Passcode",
    )
    _ensure_predicate_concept(
        concept_id=PRESENTATION_STATUS_PREDICATE_ID,
        name="Presentation Status",
    )


def _ensure_nl_name_text(concept_id: str, name_text: str) -> None:
    existing_rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate="hasName",
        limit=100,
    )
    existing_names = {
        _clean_text(row.get("text")).casefold()
        for row in existing_rows
        if isinstance(row, Mapping) and _clean_text(row.get("text"))
    }
    if name_text.casefold() in existing_names:
        return
    from .text_value_service import upsert_text_for_concept

    upsert_text_for_concept(
        subject_concept_id=concept_id,
        predicate="hasName",
        text=name_text,
        lang="en-NZ",
        context={"name_type": "NL", "source": "talk_representation_workflow"},
    )


def _ensure_presentation_concept(
    *,
    presentation_concept_id: str,
    display_name: str,
    type_ids: Sequence[str],
    created_by_concept_id: str,
    organisation_concept_id: str | None,
    namespace: str | None,
) -> tuple[str, bool]:
    concept_doc = load_concept(presentation_concept_id)
    if concept_doc is None:
        concept_service.create_concept(
            name=display_name,
            concept_id=presentation_concept_id,
            parent_concept_ids=list(type_ids),
            create_as_instance=True,
            created_by_concept_id=created_by_concept_id,
            organisation_concept_id=organisation_concept_id,
            event_namespace=namespace,
        )
        return presentation_concept_id, True

    ensure_instance_typing(concept_id=presentation_concept_id, type_ids=type_ids)
    _ensure_nl_name_text(presentation_concept_id, display_name)
    return presentation_concept_id, False


def _upsert_singleton_text(
    *,
    concept_id: str,
    predicate: str,
    text: str | None,
    source: str,
) -> None:
    text_value = _clean_text(text)
    if not text_value:
        return
    upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=predicate,
        text=text_value,
        lang="en-NZ",
        context={"source": source},
        garbage_collect=True,
    )


def _relation_contains_target(
    concept_doc: Mapping[str, Any] | None,
    predicate: str,
    target_id: str | None,
) -> bool:
    target_clean = _clean_text(target_id)
    if not target_clean:
        return False
    relationship_targets = normalise_relationship_targets(
        (concept_doc.get("relationships") or {}).get(predicate)
        if isinstance(concept_doc, Mapping)
        else None
    )
    return target_clean in relationship_targets


def _has_text_value(
    *,
    concept_id: str,
    predicate: str,
    expected_text: str | None,
) -> bool:
    expected = _clean_text(expected_text)
    if not expected:
        return True
    rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate=predicate,
        limit=50,
    )
    return any(
        _clean_text(row.get("text")) == expected
        for row in rows
        if isinstance(row, Mapping)
    )


def _speaker_exists_by_name(speaker_name: str) -> bool:
    search_result = concept_search_service.search_concepts(
        query=speaker_name,
        instance_of="#V#person",
        match_type="exact",
        limit=10,
    )
    for item in search_result.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        candidate_name = _clean_text(item.get("name"))
        candidate_id = _clean_text(item.get("concept_id"))
        if candidate_id and candidate_name.casefold() == speaker_name.casefold():
            return True
    return False


def materialise_talk_representation(
    *,
    title: str | None = None,
    speaker_name: str | None = None,
    summary: str | None = None,
    start_time: str | None = None,
    meeting_link: str | None = None,
    meeting_id: str | None = None,
    meeting_passcode: str | None = None,
    presentation_status: str | None = None,
    presentation_concept_id: str | None = None,
    presentation_type_ids: Sequence[str] | str | None = None,
    verification_profile: str | None = None,
    user_concept_id: str | None = None,
    organisation_concept_id: str | None = None,
    namespace: str | None = None,
    materialisation_source: str = "talk_representation_workflow",
) -> dict[str, Any]:
    """Create or update a talk/presentation concept and key relationships."""

    from .workflow_event_integration_service import resolve_event_actor_context

    actor_user_id, actor_org_id = resolve_event_actor_context(
        user_id=_clean_text(user_concept_id) or None,
        org_id=_clean_text(organisation_concept_id) or None,
        namespace=namespace,
    )
    if not actor_user_id:
        raise ValueError("talk_representation_actor_user_missing")

    ensure_talk_representation_primitives()

    profile = _normalise_verification_profile(verification_profile)
    type_ids = _normalise_type_ids(
        presentation_type_ids,
        verification_profile=profile,
    )

    display_name = _build_presentation_display_name(
        title=title,
        speaker_name=speaker_name,
        start_time=start_time,
        presentation_concept_id=presentation_concept_id,
    )
    resolved_presentation_concept_id = _clean_text(presentation_concept_id) or (
        stable_named_instance_concept_id(display_name, prefix="presentation")
    )
    resolved_presentation_concept_id = (
        canonicalise_vontology_concept_id(resolved_presentation_concept_id)
        or resolved_presentation_concept_id
    )

    resolved_presentation_concept_id, created_presentation_concept = (
        _ensure_presentation_concept(
            presentation_concept_id=resolved_presentation_concept_id,
            display_name=display_name,
            type_ids=type_ids,
            created_by_concept_id=actor_user_id,
            organisation_concept_id=actor_org_id,
            namespace=namespace,
        )
    )

    _upsert_singleton_text(
        concept_id=resolved_presentation_concept_id,
        predicate="hasDescription",
        text=summary,
        source=materialisation_source,
    )
    _upsert_singleton_text(
        concept_id=resolved_presentation_concept_id,
        predicate=HAS_START_TIME_PREDICATE_ID,
        text=start_time,
        source=materialisation_source,
    )
    _upsert_singleton_text(
        concept_id=resolved_presentation_concept_id,
        predicate=PRESENTATION_MEETING_LINK_PREDICATE_ID,
        text=meeting_link,
        source=materialisation_source,
    )
    _upsert_singleton_text(
        concept_id=resolved_presentation_concept_id,
        predicate=PRESENTATION_MEETING_ID_PREDICATE_ID,
        text=meeting_id,
        source=materialisation_source,
    )
    _upsert_singleton_text(
        concept_id=resolved_presentation_concept_id,
        predicate=PRESENTATION_MEETING_PASSCODE_PREDICATE_ID,
        text=meeting_passcode,
        source=materialisation_source,
    )
    _upsert_singleton_text(
        concept_id=resolved_presentation_concept_id,
        predicate=PRESENTATION_STATUS_PREDICATE_ID,
        text=presentation_status,
        source=materialisation_source,
    )

    speaker_concept_id: str | None = None
    created_speaker_concept = False
    speaker_name_text = _clean_text(speaker_name)
    if speaker_name_text:
        created_speaker_concept = not _speaker_exists_by_name(speaker_name_text)
        speaker_concept_id = resolve_or_create_scholarly_author_concept_id(
            user_concept_id=actor_user_id,
            author_name=speaker_name_text,
            logger=logger,
        )
        add_relationship(
            source_id=speaker_concept_id,
            predicate=PRESENTER_OF_PRESENTATION_PREDICATE_ID,
            target=resolved_presentation_concept_id,
        )

    return {
        "presentation_concept_id": resolved_presentation_concept_id,
        "presentation_display_name": display_name,
        "speaker_concept_id": speaker_concept_id,
        "speaker_name": speaker_name_text or None,
        "verification_profile": profile,
        "presentation_type_ids": list(type_ids),
        "created_presentation_concept": created_presentation_concept,
        "created_speaker_concept": created_speaker_concept,
        "materialisation_source": materialisation_source,
    }


def verify_talk_representation(
    *,
    presentation_concept_id: str | None,
    speaker_concept_id: str | None = None,
    summary: str | None = None,
    start_time: str | None = None,
    meeting_link: str | None = None,
    meeting_id: str | None = None,
    meeting_passcode: str | None = None,
    presentation_status: str | None = None,
    presentation_type_ids: Sequence[str] | str | None = None,
    verification_profile: str | None = None,
) -> dict[str, Any]:
    """Verify talk/presentation representation postconditions."""

    concept_id = _clean_text(presentation_concept_id)
    if not concept_id:
        return {
            "result": False,
            "talk_representation_verified": False,
            "verification_failures": ["presentation_concept_id_missing"],
        }

    concept_doc = load_concept(concept_id)
    verification_failures: list[str] = []
    if concept_doc is None:
        verification_failures.append("presentation_concept_missing")

    profile = _normalise_verification_profile(verification_profile)
    expected_type_ids = _normalise_type_ids(
        presentation_type_ids,
        verification_profile=profile,
    )
    observed_type_ids = normalise_relationship_targets(
        (concept_doc.get("relationships") or {}).get("is_an_instance_of")
        if isinstance(concept_doc, Mapping)
        else None
    )
    for type_id in expected_type_ids:
        if type_id not in observed_type_ids:
            verification_failures.append(f"type_missing:{type_id}")

    name_rows = (
        get_texts_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            limit=50,
        )
        if concept_doc is not None
        else []
    )
    if not name_rows and not _clean_text((concept_doc or {}).get("name")):
        verification_failures.append("presentation_name_missing")

    if not _has_text_value(
        concept_id=concept_id,
        predicate="hasDescription",
        expected_text=summary,
    ):
        verification_failures.append("summary_missing")
    if not _has_text_value(
        concept_id=concept_id,
        predicate=HAS_START_TIME_PREDICATE_ID,
        expected_text=start_time,
    ):
        verification_failures.append("start_time_missing")
    if not _has_text_value(
        concept_id=concept_id,
        predicate=PRESENTATION_MEETING_LINK_PREDICATE_ID,
        expected_text=meeting_link,
    ):
        verification_failures.append("meeting_link_missing")
    if not _has_text_value(
        concept_id=concept_id,
        predicate=PRESENTATION_MEETING_ID_PREDICATE_ID,
        expected_text=meeting_id,
    ):
        verification_failures.append("meeting_id_missing")
    if not _has_text_value(
        concept_id=concept_id,
        predicate=PRESENTATION_MEETING_PASSCODE_PREDICATE_ID,
        expected_text=meeting_passcode,
    ):
        verification_failures.append("meeting_passcode_missing")
    if not _has_text_value(
        concept_id=concept_id,
        predicate=PRESENTATION_STATUS_PREDICATE_ID,
        expected_text=presentation_status,
    ):
        verification_failures.append("presentation_status_missing")

    speaker_id = _clean_text(speaker_concept_id)
    if speaker_id:
        speaker_doc = load_concept(speaker_id)
        if not speaker_doc:
            verification_failures.append("speaker_concept_missing")
        elif not _relation_contains_target(
            speaker_doc,
            PRESENTER_OF_PRESENTATION_PREDICATE_ID,
            concept_id,
        ):
            verification_failures.append("presenter_relationship_missing")

    verified = not verification_failures
    return {
        "result": verified,
        "talk_representation_verified": verified,
        "presentation_concept_id": concept_id,
        "speaker_concept_id": speaker_id or None,
        "verification_profile": profile,
        "expected_type_ids": list(expected_type_ids),
        "observed_type_ids": observed_type_ids,
        "verification_failures": verification_failures,
    }


__all__ = [
    "ACADEMIC_PRESENTATION_VERIFICATION_PROFILE",
    "GENERIC_TALK_VERIFICATION_PROFILE",
    "TECHNICAL_SCIENTIFIC_TALK_VERIFICATION_PROFILE",
    "ensure_talk_representation_primitives",
    "materialise_talk_representation",
    "verify_talk_representation",
]
