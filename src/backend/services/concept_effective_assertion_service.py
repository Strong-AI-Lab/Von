"""Bounded actor-effective assertion projection for concept pages.

The scoped-assertion store remains authoritative for scope, provenance, and
lifecycle.  This module adds only a presentation-oriented read projection:
it obtains the already authority-filtered page and resolves the small set of
predicate, object, and organisation labels in one concept batch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from .concept_service import resolve_concept_display_names
from .paper_recommendation_constants import (
    GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID,
)
from .scoped_assertion_service import (
    MAX_PAGE_LIMIT,
    STANDALONE_TEXT_ASSERTION_FORM,
    get_visible_scoped_assertion_by_id,
    list_visible_scoped_assertions_page,
)

DEFAULT_PAGE_LIMIT = 25
MAX_CONCEPT_PAGE_LIMIT = min(100, MAX_PAGE_LIMIT)


def _normalise_page_value(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")  # noqa: TRY004
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if resolved < minimum or resolved > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return resolved


def _humanise_identifier(value: Any, *, fallback: str) -> str:
    token = str(value or "").strip()
    if not token:
        return fallback
    token = token.removeprefix("#V#")
    return " ".join(token.replace("_", " ").split()) or fallback


def _referenced_concept_ids(items: list[Mapping[str, Any]]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        scope = item.get("scope")
        candidates = [
            item.get("subject_concept_id"),
            item.get("predicate"),
            item.get("object_concept_id"),
            (
                scope.get("organisation_concept_id")
                if isinstance(scope, Mapping)
                else None
            ),
        ]
        for link in item.get("concept_links") or ():
            if not isinstance(link, Mapping) or link.get("status") != "active":
                continue
            candidates.append(link.get("concept_id"))
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.startswith("#V#"):
                continue
            if candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
    return ordered


def _concept_relevance(
    item: Mapping[str, Any],
    *,
    focal_concept_id: str | None,
) -> dict[str, Any] | None:
    """Describe why an assertion appears on one concept page.

    A standalone text assertion linked through ``concept_links`` is evidence
    *about* the focal concept, not a subject-predicate-object assertion whose
    subject can be silently inferred.  Keeping that distinction in the
    projection lets the UI surface the exact claim without overstating its
    formalisation.
    """

    focal_id = str(focal_concept_id or "").strip()
    if not focal_id:
        return None
    subject_match = item.get("subject_concept_id") == focal_id
    object_match = item.get("object_concept_id") == focal_id
    if subject_match and object_match:
        kind = "grounded_subject_and_object"
    elif subject_match:
        kind = "grounded_subject"
    elif object_match:
        kind = "grounded_object"
    else:
        linked = any(
            isinstance(link, Mapping)
            and link.get("status") == "active"
            and link.get("concept_id") == focal_id
            for link in item.get("concept_links") or ()
        )
        kind = "aboutness_only" if linked else "unrelated"
    return {
        "kind": kind,
        "concept_id": focal_id,
        "aboutness_only": kind == "aboutness_only",
    }


def _display_names_for_ids(concept_ids: list[str]) -> dict[str, str]:
    if not concept_ids:
        return {}
    concept_docs = list(
        ConceptsRepository.find(
            {"concept_id": {"$in": concept_ids}},
            {"concept_id": 1, "name": 1, "names": 1},
            limit=len(concept_ids),
        )
    )
    return resolve_concept_display_names(concept_docs)


def _profile_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if not isinstance(value, (list, tuple)):
        return []
    return [cleaned for item in value if (cleaned := str(item or "").strip())]


def _project_item(
    item: Mapping[str, Any],
    *,
    display_names: Mapping[str, str],
    focal_concept_id: str | None = None,
) -> dict[str, Any]:
    assertion_form = str(item.get("assertion_form") or "relation").strip()
    subject_id = str(item.get("subject_concept_id") or "").strip()
    subject_label = display_names.get(subject_id) or _humanise_identifier(
        subject_id,
        fallback="Source assertion",
    )
    predicate_id = str(item.get("predicate") or "").strip()
    predicate_label = display_names.get(predicate_id) or _humanise_identifier(
        predicate_id,
        fallback="Assertion",
    )

    object_kind = str(item.get("object_kind") or "").strip()
    if object_kind == "concept":
        object_concept_id = str(item.get("object_concept_id") or "").strip()
        object_payload: dict[str, Any] = {
            "kind": "concept",
            "concept_id": object_concept_id,
            "display_name": display_names.get(object_concept_id)
            or _humanise_identifier(object_concept_id, fallback="Concept"),
        }
    else:
        object_text = item.get("object_text")
        text_payload = object_text if isinstance(object_text, Mapping) else {}
        object_payload = {
            "kind": "text",
            "text": str(text_payload.get("text") or ""),
            "language": str(text_payload.get("language") or "") or None,
        }

    if assertion_form == STANDALONE_TEXT_ASSERTION_FORM:
        human_statement = str(object_payload.get("text") or "")
    else:
        object_label = str(
            object_payload.get("display_name") or object_payload.get("text") or ""
        ).strip()
        human_statement = " ".join(
            part for part in (subject_label, predicate_label, object_label) if part
        )

    presentation: dict[str, Any] | None = None
    if (
        predicate_id == GENERIC_PAPER_MATCH_PROFILE_JSON_PREDICATE_ID
        and object_payload.get("kind") == "text"
    ):
        try:
            profile = json.loads(str(object_payload.get("text") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            profile = None
        if isinstance(profile, Mapping):
            presentation = {
                "kind": "paper_matching_profile",
                "project_description": str(profile.get("project_description") or ""),
                "stated_interest_terms": _profile_text_list(
                    profile.get("stated_interest_terms")
                ),
                "negative_interest_terms": _profile_text_list(
                    profile.get("negative_interest_terms")
                ),
                "preferred_authors": _profile_text_list(
                    profile.get("preferred_authors")
                ),
                "preferred_venues": _profile_text_list(profile.get("preferred_venues")),
                "notes": str(profile.get("notes") or ""),
                "updated_at": profile.get("updated_at"),
            }
            human_statement = f"{subject_label} has a paper-matching profile."

    scope = item.get("scope")
    scope_payload = scope if isinstance(scope, Mapping) else {}
    scope_mode = str(scope_payload.get("mode") or "").strip()
    organisation_id = str(scope_payload.get("organisation_concept_id") or "").strip()
    if scope_mode == "organisation":
        organisation_name = display_names.get(organisation_id)
        source_context = {
            "kind": "organisation",
            "label": (
                f"Organisation — {organisation_name}"
                if organisation_name
                else "Organisation"
            ),
            "organisation_concept_id": organisation_id or None,
            "organisation_display_name": organisation_name,
        }
    else:
        source_context = {
            "kind": "personal",
            "label": "Personal — visible only to you",
            "organisation_concept_id": None,
            "organisation_display_name": None,
        }

    return {
        "assertion_id": item.get("assertion_id"),
        "assertion_revision": item.get("assertion_revision"),
        "assertion_form": assertion_form,
        "subject": {
            "concept_id": subject_id or None,
            "display_name": subject_label,
        },
        "predicate": {
            "concept_id": predicate_id if predicate_id.startswith("#V#") else None,
            "storage_id": predicate_id,
            "display_name": predicate_label,
        },
        "object": object_payload,
        "human_statement": human_statement,
        "concept_relevance": _concept_relevance(
            item,
            focal_concept_id=focal_concept_id,
        ),
        "presentation": presentation,
        "source_context": source_context,
        "status": item.get("status"),
        "epistemic_status": item.get("epistemic_status") or "asserted",
        "canonical_publication": False,
        "assertion_context": item.get("assertion_context"),
        "provenance": item.get("provenance"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def load_effective_assertion_by_id(assertion_id: Any) -> dict[str, Any] | None:
    """Return one current, presentation-ready assertion after exact actor checks."""

    item = get_visible_scoped_assertion_by_id(assertion_id)
    if not isinstance(item, Mapping):
        return None
    display_names = _display_names_for_ids(_referenced_concept_ids([item]))
    return {
        "success": True,
        "context_view": "actor_effective",
        "assertion": _project_item(item, display_names=display_names),
    }


def load_concept_effective_assertions(
    *,
    concept_id: str,
    limit: Any = DEFAULT_PAGE_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    """Return one actor-filtered, presentation-ready assertion page.

    Identity and organisation scope are deliberately absent from this API.
    ``list_visible_scoped_assertions_page`` resolves the trusted ambient actor
    and fails closed when no authorised audience exists.
    """

    subject_id = str(concept_id or "").strip()
    if not subject_id.startswith("#V#"):
        raise ValueError("concept_id must be an exact #V# concept ID")
    page_limit = _normalise_page_value(
        limit,
        field_name="limit",
        default=DEFAULT_PAGE_LIMIT,
        minimum=1,
        maximum=MAX_CONCEPT_PAGE_LIMIT,
    )
    page_offset = _normalise_page_value(
        offset,
        field_name="offset",
        default=0,
        minimum=0,
        maximum=2_147_483_647,
    )

    page = list_visible_scoped_assertions_page(
        argument_concept_id=subject_id,
        epistemic_statuses=("asserted", "tentative"),
        limit=page_limit,
        offset=page_offset,
    )
    raw_items = [item for item in page.get("items") or [] if isinstance(item, Mapping)]
    display_names = _display_names_for_ids(_referenced_concept_ids(raw_items))
    items = [
        _project_item(
            item,
            display_names=display_names,
            focal_concept_id=subject_id,
        )
        for item in raw_items
    ]
    return {
        "success": True,
        "concept_id": subject_id,
        "context_view": "actor_effective",
        "items": items,
        "returned": len(items),
        "offset": page_offset,
        "limit": page_limit,
        "has_more": bool(page.get("has_more")),
        "next_offset": page.get("next_offset"),
        "truncated": bool(page.get("truncated")),
        "counts_are_lower_bounds": bool(page.get("counts_are_lower_bounds")),
    }


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_CONCEPT_PAGE_LIMIT",
    "load_concept_effective_assertions",
    "load_effective_assertion_by_id",
]
