"""Concept-page summary renderer support surfaces.

This service resolves a concept-page renderer for an individual concept using
Vontology-backed renderer applicability metadata, then builds a stable payload
for the concept tab's top summary panel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from ..db.repositories.concepts_repository import ConceptsRepository
from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback
from .chat_history_service import ChatHistoryServiceError
from .concept_predicate_metadata_service import get_relationship_kinds_set
from .concept_service import (
    ConceptNotFoundError,
    enrich_concept_with_text_relations,
    get_concept_by_concept_id,
    resolve_concept_display_names,
)
from .concept_summary_field_resolver import get_concept_summary_field_resolver
from .concept_type_closure_service import load_type_closure
from .conversation_projection_service import (
    ConversationProjectionAccessChangedError,
    ConversationProjectionNotFoundError,
    get_conversation_projection_for_session,
    list_focal_conversation_backlinks,
)
from .renderer_applicability_service import resolve_renderer_applicability_from_metadata
from .renderer_applicability_vontology_service import (
    load_renderer_definitions_from_concept_ids,
)
from .text_value_service import get_texts_for_concept

CONCEPT_PAGE_RENDERER_CONCEPT_IDS: tuple[str, ...] = (
    "#V#concept_page_conversation_renderer",
    "#V#concept_page_identity_renderer",
    "#V#concept_page_document_renderer",
    "#V#concept_page_task_renderer",
    "#V#concept_page_event_renderer",
    "#V#concept_page_location_renderer",
    "#V#concept_page_text_summary_renderer",
)

_CONVERSATION_RENDERER_ID = "#V#concept_page_conversation_renderer"
_CONVERSATION_TYPE_ID = "#V#conversation"
_CONVERSATION_SESSION_ID_PREDICATES = frozenset(
    {"#V#hasSessionId", "hasSessionId"}
)

_GENERIC_TYPE_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "#V#thing",
        "#V#person",
        "#V#meeting",
        "#V#room",
        "#V#task_specification",
        "#V#research_paper",
        "#V#scholarly_work",
        "#V#mentioned_in_von_code",
        "#V#mentioned_in_von_test",
        "#V#von_user",
        "#V#effort_unit",
    }
)

_PERSON_ROLE_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "#V#person",
        "#V#researcher",
        "#V#thing",
        "#V#mentioned_in_von_code",
        "#V#mentioned_in_von_test",
        "#V#von_user",
    }
)


def _normalise_strings(raw: Any) -> list[str]:
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)
    return values


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _first_sentence(text: str, *, max_chars: int) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    sentence_match = re.search(r"(.+?[.!?])(?:\s|$)", stripped)
    sentence = sentence_match.group(1).strip() if sentence_match else stripped
    if len(sentence) <= max_chars:
        return sentence
    shortened = sentence[: max_chars - 1].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return f"{shortened}…"


def _plain_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _display_name(concept: Mapping[str, Any]) -> str:
    label = get_concept_display_name_with_names_fallback(
        cast(dict[Any, Any], dict(concept))
    )
    return str(label or concept.get("concept_id") or "").strip()


def _concept_preview(concept_id: str, cache: dict[str, str]) -> str:
    cached = cache.get(concept_id)
    if cached is not None:
        return cached
    try:
        concept = get_concept_by_concept_id(concept_id)
    except Exception:
        cache[concept_id] = concept_id
        return concept_id
    if not isinstance(concept, Mapping):
        cache[concept_id] = concept_id
        return concept_id
    enriched = enrich_concept_with_text_relations(dict(concept))
    label = _display_name(enriched)
    text = str(label or concept_id).strip() or concept_id
    cache[concept_id] = text
    return text


def _collect_ancestor_type_ids(type_ids: list[str]) -> list[str]:
    return list(load_type_closure(type_ids).get("ancestor_type_ids") or [])


def _apply_relation_backed_names(
    concept: Mapping[str, Any],
    text_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    projected = dict(concept)
    relation_names = [
        {
            "name": str(row.get("text") or ""),
            "language": str(row.get("lang") or "en-NZ"),
            "type": str((row.get("context") or {}).get("name_type") or "NL"),
        }
        for row in text_rows
        if row.get("predicate") == "hasName" and str(row.get("text") or "").strip()
    ]
    if relation_names:
        projected["names"] = relation_names
    return projected


def _prime_preview_cache(
    *,
    concept: Mapping[str, Any],
    resolved_type_ids: list[str],
    type_documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    candidate_ids = list(resolved_type_ids)
    relationships = concept.get("relationships")
    if isinstance(relationships, Mapping):
        for raw in relationships.values():
            candidate_ids.extend(
                value for value in _normalise_strings(raw) if value.startswith("#V#")
            )
    candidate_ids = _dedupe_preserve_order(candidate_ids)
    docs_by_id: dict[str, dict[str, Any]] = {
        concept_id: dict(doc)
        for concept_id, doc in type_documents.items()
        if isinstance(doc, Mapping)
    }
    missing_ids = [item for item in candidate_ids if item not in docs_by_id]
    if missing_ids:
        for doc in ConceptsRepository.find(
            {"concept_id": {"$in": missing_ids}},
            {"concept_id": 1, "name": 1, "names": 1},
            limit=len(missing_ids),
        ):
            if not isinstance(doc, Mapping):
                continue
            doc_id = str(doc.get("concept_id") or "").strip()
            if doc_id:
                docs_by_id[doc_id] = dict(doc)
    display_names = resolve_concept_display_names(docs_by_id.values())
    return {
        concept_id: display_names.get(concept_id) or _display_name(doc) or concept_id
        for concept_id, doc in docs_by_id.items()
    }


def _collect_present_predicates(
    relationships: Mapping[str, Any],
    text_rows: list[dict[str, Any]],
) -> list[str]:
    structural_relationship_keys = get_relationship_kinds_set()
    predicates: list[str] = []
    for predicate, raw in relationships.items():
        if not isinstance(predicate, str) or predicate in structural_relationship_keys:
            continue
        if _normalise_strings(raw):
            predicates.append(predicate)
    for row in text_rows:
        predicate = row.get("predicate")
        if isinstance(predicate, str) and predicate.strip():
            predicates.append(predicate.strip())
    return _dedupe_preserve_order(predicates)


def _index_text_rows(text_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    indexed: dict[str, list[str]] = {}
    for row in text_rows:
        predicate = row.get("predicate")
        text = row.get("text")
        if not isinstance(predicate, str) or not predicate.strip():
            continue
        value = str(text or "").strip()
        if not value:
            continue
        indexed.setdefault(predicate.strip(), []).append(value)
    return indexed


def _summary_field_enabled(summary_field_keys: frozenset[str], field_key: str) -> bool:
    return field_key in summary_field_keys


def _first_text_value(
    indexed: Mapping[str, list[str]],
    alias_key: str,
    summary_field_keys: frozenset[str],
) -> str | None:
    if not _summary_field_enabled(summary_field_keys, alias_key):
        return None
    aliases = get_concept_summary_field_resolver().get_text_predicates_for_field(alias_key)
    for alias in aliases:
        values = indexed.get(alias) or []
        if values:
            return str(values[0]).strip()
    return None


def _all_text_values(
    indexed: Mapping[str, list[str]],
    alias_key: str,
    summary_field_keys: frozenset[str],
) -> list[str]:
    if not _summary_field_enabled(summary_field_keys, alias_key):
        return []
    aliases = get_concept_summary_field_resolver().get_text_predicates_for_field(alias_key)
    values: list[str] = []
    for alias in aliases:
        values.extend(indexed.get(alias) or [])
    return _dedupe_preserve_order([str(value).strip() for value in values if str(value or "").strip()])


def _relationship_values(
    relationships: Mapping[str, Any],
    alias_key: str,
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> list[str]:
    if not _summary_field_enabled(summary_field_keys, alias_key):
        return []
    aliases = get_concept_summary_field_resolver().get_relationship_predicates_for_field(alias_key)
    values: list[str] = []
    for alias in aliases:
        for raw in _normalise_strings(relationships.get(alias)):
            if raw.startswith("#V#"):
                values.append(_concept_preview(raw, preview_cache))
            else:
                values.append(raw)
    return _dedupe_preserve_order(values)


def _type_labels(
    type_ids: list[str],
    preview_cache: dict[str, str],
    *,
    exclude: frozenset[str] = frozenset(),
    limit: int = 6,
) -> list[str]:
    labels: list[str] = []
    for type_id in type_ids:
        if type_id in exclude:
            continue
        labels.append(_concept_preview(type_id, preview_cache))
        if len(labels) >= limit:
            break
    return _dedupe_preserve_order(labels)


def _description_sources(
    concept: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    summary_field_keys: frozenset[str],
) -> list[str]:
    values: list[str] = []
    values.extend(_all_text_values(indexed_text_rows, "description", summary_field_keys))
    concept_description = concept.get("description")
    if (
        _summary_field_enabled(summary_field_keys, "description")
        and isinstance(concept_description, str)
        and concept_description.strip()
    ):
        values.append(concept_description.strip())
    preserved_description = (
        ((concept.get("concept_data") or {}).get("preserved_fields") or {}).get("description")
    )
    if (
        _summary_field_enabled(summary_field_keys, "description")
        and isinstance(preserved_description, str)
        and preserved_description.strip()
    ):
        values.append(preserved_description.strip())
    note = concept.get("note") or concept.get("notes")
    if (
        _summary_field_enabled(summary_field_keys, "description")
        and isinstance(note, str)
        and note.strip()
    ):
        values.append(note.strip())
    content_values = _all_text_values(indexed_text_rows, "content", summary_field_keys)
    if content_values:
        values.append(content_values[0])
    return _dedupe_preserve_order(values)


def _summary_text(
    concept: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    summary_field_keys: frozenset[str],
) -> str:
    description_values = _description_sources(
        concept,
        indexed_text_rows,
        summary_field_keys,
    )
    for value in description_values:
        plain = _plain_text(value)
        if plain:
            return _first_sentence(plain, max_chars=240)
    return ""


def _facts(*items: tuple[str, str | None]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for label, value in items:
        text = str(value or "").strip()
        if not text:
            continue
        rows.append({"label": label, "value": text})
    return rows


def _conversation_session_id(
    concept: Mapping[str, Any], text_rows: list[dict[str, Any]]
) -> str | None:
    """Read the source locator without exposing the concept's stored metadata."""

    metadata = concept.get("metadata")
    raw_session_id = metadata.get("session_id") if isinstance(metadata, Mapping) else None
    if isinstance(raw_session_id, str) and raw_session_id.strip():
        return raw_session_id.strip()
    for row in text_rows:
        if row.get("predicate") not in _CONVERSATION_SESSION_ID_PREDICATES:
            continue
        text = row.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _project_focal_concepts(raw: Any) -> list[dict[str, Any]]:
    """Copy only renderer-safe fields from already actor-authorised focus cards."""

    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:4]:
        if not isinstance(item, Mapping):
            continue
        concept_id = item.get("concept_id")
        if (
            not isinstance(concept_id, str)
            or not concept_id.strip().startswith("#V#")
            or concept_id.strip() in seen
        ):
            continue
        concept_id = concept_id.strip()
        seen.add(concept_id)
        display_name = item.get("display_name")
        type_ids = item.get("type_ids")
        result.append(
            {
                "concept_id": concept_id,
                "display_name": (
                    display_name.strip()
                    if isinstance(display_name, str) and display_name.strip()
                    else concept_id
                ),
                "type_ids": [
                    type_id.strip()
                    for type_id in type_ids[:12]
                    if isinstance(type_id, str) and type_id.strip().startswith("#V#")
                ]
                if isinstance(type_ids, list)
                else [],
            }
        )
    return result


def _project_open_conversation_action(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, Mapping) or raw.get("kind") != "open_conversation":
        return None
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return {"kind": "open_conversation", "session_id": session_id.strip()}


def _project_conversation_card(raw: Any) -> dict[str, Any] | None:
    """Map a Stage 2 card to the renderer contract through an explicit allow-list."""

    if not isinstance(raw, Mapping):
        return None
    action = _project_open_conversation_action(raw.get("open_action"))
    title = raw.get("title")
    if action is None or not isinstance(title, str) or not title.strip():
        return None
    concept_id = raw.get("conversation_concept_id")
    if not isinstance(concept_id, str) or not concept_id.strip().startswith("#V#"):
        concept_id = None
    last_activity_at = raw.get("last_activity_at")
    access_mode = raw.get("access_mode")
    projection_status = raw.get("projection_status")
    return {
        "schema_version": "conversation_projection.v1",
        "conversation_concept_id": concept_id.strip() if concept_id else None,
        "title": title.strip(),
        "last_activity_at": (
            last_activity_at.strip()
            if isinstance(last_activity_at, str) and last_activity_at.strip()
            else None
        ),
        "access_mode": access_mode if access_mode in {"owner", "shared"} else None,
        "focal_concepts": _project_focal_concepts(raw.get("focal_concepts")),
        "projection_status": (
            projection_status
            if projection_status in {"materialised", "source_backed", "pending"}
            else None
        ),
        "open_action": action,
    }


def _project_conversation_backlinks(
    raw: Any, *, source_concept_id: str
) -> dict[str, Any]:
    """Copy a bounded backlink projection without legacy or source-store fields."""

    raw_mapping = raw if isinstance(raw, Mapping) else {}
    cards: list[dict[str, Any]] = []
    raw_items = raw_mapping.get("items")
    if isinstance(raw_items, list):
        for item in raw_items[:25]:
            card = _project_conversation_card(item)
            if card is not None:
                cards.append(card)
    more_count = raw_mapping.get("more_count")
    return {
        "schema_version": "conversation_backlinks.v1",
        "source_concept_id": source_concept_id,
        "items": cards,
        "more_count": (
            max(more_count, 0)
            if isinstance(more_count, int) and not isinstance(more_count, bool)
            else 0
        ),
    }


def _build_conversation_panel(card: Mapping[str, Any]) -> dict[str, Any]:
    last_activity_at = card.get("last_activity_at")
    return {
        "variant": "conversation",
        "eyebrow": "Conversation",
        "title": str(card.get("title") or "Conversation").strip() or "Conversation",
        "subtitle": "Focused discussion" if card.get("focal_concepts") else "Discussion",
        "badges": [],
        "summary": "",
        "facts": _facts(
            (
                "Last activity",
                last_activity_at if isinstance(last_activity_at, str) else None,
            )
        ),
        "expanded_sections": [],
        "focal_concepts": list(card.get("focal_concepts") or []),
        "open_action": card.get("open_action"),
    }


def _build_person_panel(
    *,
    concept: Mapping[str, Any],
    relationships: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    direct_type_ids: list[str],
    resolved_type_ids: list[str],
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> dict[str, Any]:
    emails = _all_text_values(indexed_text_rows, "email", summary_field_keys)
    affiliations = _relationship_values(
        relationships,
        "affiliation",
        preview_cache,
        summary_field_keys,
    )
    roles = _type_labels(
        direct_type_ids or resolved_type_ids,
        preview_cache,
        exclude=_PERSON_ROLE_EXCLUSIONS,
        limit=4,
    )
    authored_works = _relationship_values(
        relationships,
        "authored_work",
        preview_cache,
        summary_field_keys,
    )
    subtitle_parts = []
    if roles:
        subtitle_parts.append(", ".join(roles[:2]))
    if affiliations:
        subtitle_parts.append(affiliations[0])
    return {
        "variant": "identity",
        "eyebrow": "Person",
        "title": _display_name(concept),
        "subtitle": " · ".join(subtitle_parts[:2]),
        "badges": roles,
        "summary": _summary_text(concept, indexed_text_rows, summary_field_keys),
        "facts": _facts(
            ("Email", emails[0] if emails else None),
            ("Affiliation", ", ".join(affiliations[:2]) if affiliations else None),
            ("Works", str(len(authored_works)) if authored_works else None),
        ),
        "expanded_sections": [
            {"title": "Affiliations", "items": affiliations[:6]},
            {"title": "Roles", "items": roles[:6]},
            {"title": "Contact", "items": emails[:4]},
        ],
    }


def _build_document_panel(
    *,
    concept: Mapping[str, Any],
    relationships: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    resolved_type_ids: list[str],
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> dict[str, Any]:
    authors = _relationship_values(
        relationships,
        "author",
        preview_cache,
        summary_field_keys,
    )
    publication_date = _first_text_value(
        indexed_text_rows,
        "publication_date",
        summary_field_keys,
    )
    type_labels = _type_labels(
        resolved_type_ids,
        preview_cache,
        exclude=_GENERIC_TYPE_EXCLUSIONS,
        limit=4,
    )
    summary = _summary_text(concept, indexed_text_rows, summary_field_keys)
    return {
        "variant": "document",
        "eyebrow": "Paper",
        "title": _display_name(concept),
        "subtitle": ", ".join(authors[:3]),
        "badges": type_labels,
        "summary": summary,
        "facts": _facts(
            ("Authors", ", ".join(authors[:3]) if authors else None),
            ("Publication", publication_date),
            ("Type", ", ".join(type_labels[:2]) if type_labels else None),
        ),
        "expanded_sections": [
            {"title": "Authors", "items": authors[:8]},
            {"title": "Classification", "items": type_labels[:6]},
        ],
    }


def _build_task_panel(
    *,
    concept: Mapping[str, Any],
    relationships: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> dict[str, Any]:
    status = _first_text_value(indexed_text_rows, "task_status", summary_field_keys)
    priority = _first_text_value(indexed_text_rows, "priority", summary_field_keys)
    due = _first_text_value(indexed_text_rows, "due_date", summary_field_keys)
    source = _relationship_values(
        relationships,
        "task_source",
        preview_cache,
        summary_field_keys,
    )
    return {
        "variant": "task",
        "eyebrow": "Task",
        "title": _display_name(concept),
        "subtitle": " · ".join([value for value in (status, priority) if value]),
        "badges": [value for value in (status, priority) if value],
        "summary": _summary_text(concept, indexed_text_rows, summary_field_keys),
        "facts": _facts(
            ("Status", status),
            ("Priority", priority),
            ("Due", due),
            ("Source", source[0] if source else None),
        ),
        "expanded_sections": [
            {"title": "Task source", "items": source[:4]},
        ],
    }


def _build_event_panel(
    *,
    concept: Mapping[str, Any],
    relationships: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    resolved_type_ids: list[str],
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> dict[str, Any]:
    when = _first_text_value(indexed_text_rows, "event_date", summary_field_keys)
    participants = _relationship_values(
        relationships,
        "meeting_participant",
        preview_cache,
        summary_field_keys,
    )
    location = _relationship_values(
        relationships,
        "meeting_location",
        preview_cache,
        summary_field_keys,
    )
    hosts = _relationship_values(
        relationships,
        "meeting_host",
        preview_cache,
        summary_field_keys,
    )
    type_labels = _type_labels(
        resolved_type_ids,
        preview_cache,
        exclude=_GENERIC_TYPE_EXCLUSIONS,
        limit=4,
    )
    subtitle_parts = []
    if when:
        subtitle_parts.append(when)
    if location:
        subtitle_parts.append(location[0])
    return {
        "variant": "event",
        "eyebrow": "Event",
        "title": _display_name(concept),
        "subtitle": " · ".join(subtitle_parts),
        "badges": type_labels,
        "summary": _summary_text(concept, indexed_text_rows, summary_field_keys),
        "facts": _facts(
            ("When", when),
            ("Location", location[0] if location else None),
            ("Participants", ", ".join(participants[:3]) if participants else None),
        ),
        "expanded_sections": [
            {"title": "Participants", "items": participants[:8]},
            {"title": "Hosts", "items": hosts[:4]},
        ],
    }


def _build_location_panel(
    *,
    concept: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    resolved_type_ids: list[str],
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> dict[str, Any]:
    aliases = [
        str(item.get("name") or "").strip()
        for item in (concept.get("names") or [])
        if isinstance(item, Mapping)
        and str(item.get("name") or "").strip()
        and str(item.get("type") or "").strip() == "NL"
    ]
    aliases = _dedupe_preserve_order(aliases)
    capacity = _first_text_value(indexed_text_rows, "capacity", summary_field_keys)
    url = _first_text_value(indexed_text_rows, "url", summary_field_keys)
    type_labels = _type_labels(
        resolved_type_ids,
        preview_cache,
        exclude=_GENERIC_TYPE_EXCLUSIONS,
        limit=4,
    )
    return {
        "variant": "location",
        "eyebrow": "Location",
        "title": _display_name(concept),
        "subtitle": ", ".join(type_labels[:2]),
        "badges": type_labels,
        "summary": _summary_text(concept, indexed_text_rows, summary_field_keys),
        "facts": _facts(
            ("Alias", aliases[1] if len(aliases) > 1 else None),
            ("Capacity", capacity),
            ("Type", ", ".join(type_labels[:2]) if type_labels else None),
        ),
        "expanded_sections": [
            {"title": "Known aliases", "items": aliases[:6]},
            {"title": "Links", "items": [url] if url else []},
        ],
    }


def _build_generic_panel(
    *,
    concept: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    resolved_type_ids: list[str],
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> dict[str, Any]:
    type_labels = _type_labels(
        resolved_type_ids,
        preview_cache,
        exclude=_GENERIC_TYPE_EXCLUSIONS,
        limit=5,
    )
    return {
        "variant": "generic",
        "eyebrow": "Concept",
        "title": _display_name(concept),
        "subtitle": ", ".join(type_labels[:2]),
        "badges": type_labels,
        "summary": _summary_text(concept, indexed_text_rows, summary_field_keys),
        "facts": _facts(("Type", ", ".join(type_labels[:3]) if type_labels else None)),
        "expanded_sections": [],
    }


def _build_panel_for_renderer(
    *,
    renderer_id: str | None,
    concept: Mapping[str, Any],
    relationships: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    direct_type_ids: list[str],
    resolved_type_ids: list[str],
    preview_cache: dict[str, str],
    summary_field_keys: frozenset[str],
) -> dict[str, Any]:
    if renderer_id == "#V#concept_page_identity_renderer":
        return _build_person_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            direct_type_ids=direct_type_ids,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
            summary_field_keys=summary_field_keys,
        )
    if renderer_id == "#V#concept_page_document_renderer":
        return _build_document_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
            summary_field_keys=summary_field_keys,
        )
    if renderer_id == "#V#concept_page_task_renderer":
        return _build_task_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            preview_cache=preview_cache,
            summary_field_keys=summary_field_keys,
        )
    if renderer_id == "#V#concept_page_event_renderer":
        return _build_event_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
            summary_field_keys=summary_field_keys,
        )
    if renderer_id == "#V#concept_page_location_renderer":
        return _build_location_panel(
            concept=concept,
            indexed_text_rows=indexed_text_rows,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
            summary_field_keys=summary_field_keys,
        )
    return _build_generic_panel(
        concept=concept,
        indexed_text_rows=indexed_text_rows,
        resolved_type_ids=resolved_type_ids,
        preview_cache=preview_cache,
        summary_field_keys=summary_field_keys,
    )


def load_concept_summary_renderer(
    concept_id: str,
    *,
    actor_user_id: str | None = None,
    actor_namespace: str | None = None,
    organisation_concept_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a concept-page summary renderer and build its payload."""

    concept = get_concept_by_concept_id(concept_id)
    if not isinstance(concept, Mapping):
        raise ConceptNotFoundError(f"Concept not found: {concept_id}")
    text_rows = get_texts_for_concept(subject_concept_id=concept_id, limit=250)
    concept = _apply_relation_backed_names(concept, text_rows)
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        relationships = {}
    direct_type_ids = _normalise_strings(relationships.get("is_an_instance_of"))
    type_closure = load_type_closure(direct_type_ids)
    resolved_type_ids = _dedupe_preserve_order(
        list(type_closure.get("ordered_type_ids") or direct_type_ids)
    )
    present_predicates = _collect_present_predicates(relationships, text_rows)
    field_resolver = get_concept_summary_field_resolver()
    summary_field_keys = frozenset(
        field_resolver.get_summary_fields_for_types(resolved_type_ids)
    )

    definitions, loading_diagnostics = load_renderer_definitions_from_concept_ids(
        CONCEPT_PAGE_RENDERER_CONCEPT_IDS
    )
    resolution = resolve_renderer_applicability_from_metadata(
        renderer_definitions=definitions,
        request_payload={
            "concept_id": concept_id,
            "object_kind": "concept",
            "concept_type_ids": resolved_type_ids,
            "present_predicates": present_predicates,
            "context_tags": ["concept_page_summary"],
            "preferred_modalities": ["visual", "textual"],
        },
        allow_multimodal=False,
    )

    selected = resolution.selected_renderers[0] if resolution.selected_renderers else None
    preview_cache = _prime_preview_cache(
        concept=concept,
        resolved_type_ids=resolved_type_ids,
        type_documents=type_closure.get("documents_by_id") or {},
    )
    indexed_text_rows = _index_text_rows(text_rows)
    selected_renderer_id = selected.renderer_id if selected else None
    is_conversation = _CONVERSATION_TYPE_ID in resolved_type_ids
    conversation_card: dict[str, Any] | None = None
    if is_conversation:
        session_id = _conversation_session_id(concept, text_rows)
        if not actor_user_id or not session_id:
            raise ConceptNotFoundError(f"Concept not found: {concept_id}")
        try:
            projection_result = get_conversation_projection_for_session(
                actor_user_id=actor_user_id,
                session_id=session_id,
                actor_namespace=actor_namespace,
                organisation_concept_id=organisation_concept_id,
            )
        except (
            ConversationProjectionAccessChangedError,
            ConversationProjectionNotFoundError,
        ) as exc:
            raise ConceptNotFoundError(f"Concept not found: {concept_id}") from exc
        conversation_card = _project_conversation_card(
            projection_result.get("conversation_projection")
            if isinstance(projection_result, Mapping)
            else None
        )
        if (
            conversation_card is None
            or conversation_card.get("access_mode") != "owner"
            or conversation_card.get("conversation_concept_id") != concept_id
        ):
            raise ConceptNotFoundError(f"Concept not found: {concept_id}")
    if selected_renderer_id == _CONVERSATION_RENDERER_ID:
        if conversation_card is None:
            raise ConceptNotFoundError(f"Concept not found: {concept_id}")
        panel = _build_conversation_panel(conversation_card)
    else:
        panel = _build_panel_for_renderer(
            renderer_id=selected_renderer_id,
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            direct_type_ids=direct_type_ids,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
            summary_field_keys=summary_field_keys,
        )

    payload = {
        "success": True,
        "concept_id": concept_id,
        "display_name": _display_name(concept),
        "direct_type_ids": direct_type_ids,
        "resolved_type_ids": resolved_type_ids,
        "summary_field_keys": sorted(summary_field_keys),
        "selected_renderer": selected.to_dict() if selected else None,
        "panel": panel,
        "diagnostics": {
            "renderer_definition_loading": loading_diagnostics,
            "renderer_resolution": resolution.to_dict(),
        },
    }
    if actor_user_id and not is_conversation:
        try:
            backlink_result = list_focal_conversation_backlinks(
                actor_user_id=actor_user_id,
                focal_concept_id=concept_id,
                actor_namespace=actor_namespace,
            )
        except (ConversationProjectionNotFoundError, ChatHistoryServiceError):
            backlink_result = None
        if isinstance(backlink_result, Mapping):
            payload["conversation_backlinks"] = _project_conversation_backlinks(
                backlink_result.get("conversation_backlinks"),
                source_concept_id=concept_id,
            )
    return payload
