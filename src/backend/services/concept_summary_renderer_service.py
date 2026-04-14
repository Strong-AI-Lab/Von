"""Concept-page summary renderer support surfaces.

This service resolves a concept-page renderer for an individual concept using
Vontology-backed renderer applicability metadata, then builds a stable payload
for the concept tab's top summary panel.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, cast

from .concept_service import (
    ConceptNotFoundError,
    enrich_concept_with_text_relations,
    get_concept_by_concept_id,
)
from .renderer_applicability_service import resolve_renderer_applicability_from_metadata
from .renderer_applicability_vontology_service import (
    load_renderer_definitions_from_concept_ids,
)
from .task_ontology_service import TASK_SOURCE_RELATIONSHIP_PREDICATES
from .text_value_service import get_texts_for_concept
from ..vontology.utils_vontology import get_concept_display_name_with_names_fallback

CONCEPT_PAGE_RENDERER_CONCEPT_IDS: tuple[str, ...] = (
    "#V#concept_page_identity_renderer",
    "#V#concept_page_document_renderer",
    "#V#concept_page_task_renderer",
    "#V#concept_page_event_renderer",
    "#V#concept_page_location_renderer",
    "#V#concept_page_text_summary_renderer",
)

_STRUCTURAL_RELATIONSHIP_KEYS: frozenset[str] = frozenset(
    {
        "is_a_type_of",
        "is_an_instance_of",
        "has_subtype",
        "has_instance",
        "related_to",
    }
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

_TEXT_PREDICATE_ALIASES: dict[str, tuple[str, ...]] = {
    "description": ("hasDescription", "#V#hasDescription"),
    "content": ("hasContent", "#V#hasContent"),
    "email": ("#V#has_email", "has_email"),
    "publication_date": ("#V#has_publication_date", "has_publication_date"),
    "task_status": ("#V#hasTaskStatus", "hasTaskStatus"),
    "priority": ("#V#hasPriority", "hasPriority"),
    "due_date": (
        "#V#hasDueDate",
        "#V#has_due_date",
        "#V#has_due_time",
        "#V#has_due",
        "hasDueDate",
        "has_due_date",
    ),
    "event_date": (
        "#V#date_of_event",
        "#V#has_start_time",
        "date_of_event",
        "has_start_time",
    ),
    "capacity": ("#V#has_capacity", "has_capacity", "#V#capacity", "capacity"),
    "url": ("#V#has_url", "has_url"),
}

_RELATIONSHIP_PREDICATE_ALIASES: dict[str, tuple[str, ...]] = {
    "affiliation": (
        "#V#has_affiliation",
        "#V#member_of_organisation",
        "#V#member_of_faculty",
        "#V#homeresearchorganisation",
    ),
    "author": ("#V#has_author", "#V#has_first_author"),
    "meeting_participant": ("#V#meeting_participant", "#V#performed_by"),
    "meeting_location": ("#V#meeting_location", "#V#has_location"),
    "meeting_host": ("#V#meeting_host_organisation",),
    "task_source": TASK_SOURCE_RELATIONSHIP_PREDICATES,
    "authored_work": ("#V#author_of",),
}


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
    queue = list(type_ids)
    visited: set[str] = set()
    ordered: list[str] = []
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            concept = get_concept_by_concept_id(current)
        except Exception:
            continue
        if not isinstance(concept, Mapping):
            continue
        parents = _normalise_strings((concept.get("relationships") or {}).get("is_a_type_of"))
        for parent_id in parents:
            if parent_id not in visited and parent_id not in queue:
                queue.append(parent_id)
                ordered.append(parent_id)
    return _dedupe_preserve_order(ordered)


def _collect_present_predicates(
    relationships: Mapping[str, Any],
    text_rows: list[dict[str, Any]],
) -> list[str]:
    predicates: list[str] = []
    for predicate, raw in relationships.items():
        if not isinstance(predicate, str) or predicate in _STRUCTURAL_RELATIONSHIP_KEYS:
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


def _first_text_value(indexed: Mapping[str, list[str]], alias_key: str) -> str | None:
    aliases = _TEXT_PREDICATE_ALIASES.get(alias_key, ())
    for alias in aliases:
        values = indexed.get(alias) or []
        if values:
            return str(values[0]).strip()
    return None


def _all_text_values(indexed: Mapping[str, list[str]], alias_key: str) -> list[str]:
    aliases = _TEXT_PREDICATE_ALIASES.get(alias_key, ())
    values: list[str] = []
    for alias in aliases:
        values.extend(indexed.get(alias) or [])
    return _dedupe_preserve_order([str(value).strip() for value in values if str(value or "").strip()])


def _relationship_values(
    relationships: Mapping[str, Any],
    alias_key: str,
    preview_cache: dict[str, str],
) -> list[str]:
    aliases = _RELATIONSHIP_PREDICATE_ALIASES.get(alias_key, ())
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


def _description_sources(concept: Mapping[str, Any], indexed_text_rows: Mapping[str, list[str]]) -> list[str]:
    values: list[str] = []
    values.extend(_all_text_values(indexed_text_rows, "description"))
    concept_description = concept.get("description")
    if isinstance(concept_description, str) and concept_description.strip():
        values.append(concept_description.strip())
    preserved_description = (
        ((concept.get("concept_data") or {}).get("preserved_fields") or {}).get("description")
    )
    if isinstance(preserved_description, str) and preserved_description.strip():
        values.append(preserved_description.strip())
    note = concept.get("note") or concept.get("notes")
    if isinstance(note, str) and note.strip():
        values.append(note.strip())
    content_values = _all_text_values(indexed_text_rows, "content")
    if content_values:
        values.append(content_values[0])
    return _dedupe_preserve_order(values)


def _summary_text(concept: Mapping[str, Any], indexed_text_rows: Mapping[str, list[str]]) -> str:
    description_values = _description_sources(concept, indexed_text_rows)
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


def _build_person_panel(
    *,
    concept: Mapping[str, Any],
    relationships: Mapping[str, Any],
    indexed_text_rows: Mapping[str, list[str]],
    direct_type_ids: list[str],
    resolved_type_ids: list[str],
    preview_cache: dict[str, str],
) -> dict[str, Any]:
    emails = _all_text_values(indexed_text_rows, "email")
    affiliations = _relationship_values(relationships, "affiliation", preview_cache)
    roles = _type_labels(
        direct_type_ids or resolved_type_ids,
        preview_cache,
        exclude=_PERSON_ROLE_EXCLUSIONS,
        limit=4,
    )
    authored_works = _relationship_values(relationships, "authored_work", preview_cache)
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
        "summary": _summary_text(concept, indexed_text_rows),
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
) -> dict[str, Any]:
    authors = _relationship_values(relationships, "author", preview_cache)
    publication_date = _first_text_value(indexed_text_rows, "publication_date")
    type_labels = _type_labels(
        resolved_type_ids,
        preview_cache,
        exclude=_GENERIC_TYPE_EXCLUSIONS,
        limit=4,
    )
    summary = _summary_text(concept, indexed_text_rows)
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
) -> dict[str, Any]:
    status = _first_text_value(indexed_text_rows, "task_status")
    priority = _first_text_value(indexed_text_rows, "priority")
    due = _first_text_value(indexed_text_rows, "due_date")
    source = _relationship_values(relationships, "task_source", preview_cache)
    return {
        "variant": "task",
        "eyebrow": "Task",
        "title": _display_name(concept),
        "subtitle": " · ".join([value for value in (status, priority) if value]),
        "badges": [value for value in (status, priority) if value],
        "summary": _summary_text(concept, indexed_text_rows),
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
) -> dict[str, Any]:
    when = _first_text_value(indexed_text_rows, "event_date")
    participants = _relationship_values(relationships, "meeting_participant", preview_cache)
    location = _relationship_values(relationships, "meeting_location", preview_cache)
    hosts = _relationship_values(relationships, "meeting_host", preview_cache)
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
        "summary": _summary_text(concept, indexed_text_rows),
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
) -> dict[str, Any]:
    aliases = [
        str(item.get("name") or "").strip()
        for item in (concept.get("names") or [])
        if isinstance(item, Mapping)
        and str(item.get("name") or "").strip()
        and str(item.get("type") or "").strip() == "NL"
    ]
    aliases = _dedupe_preserve_order(aliases)
    capacity = _first_text_value(indexed_text_rows, "capacity")
    url = _first_text_value(indexed_text_rows, "url")
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
        "summary": _summary_text(concept, indexed_text_rows),
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
        "summary": _summary_text(concept, indexed_text_rows),
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
) -> dict[str, Any]:
    if renderer_id == "#V#concept_page_identity_renderer":
        return _build_person_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            direct_type_ids=direct_type_ids,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
        )
    if renderer_id == "#V#concept_page_document_renderer":
        return _build_document_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
        )
    if renderer_id == "#V#concept_page_task_renderer":
        return _build_task_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            preview_cache=preview_cache,
        )
    if renderer_id == "#V#concept_page_event_renderer":
        return _build_event_panel(
            concept=concept,
            relationships=relationships,
            indexed_text_rows=indexed_text_rows,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
        )
    if renderer_id == "#V#concept_page_location_renderer":
        return _build_location_panel(
            concept=concept,
            indexed_text_rows=indexed_text_rows,
            resolved_type_ids=resolved_type_ids,
            preview_cache=preview_cache,
        )
    return _build_generic_panel(
        concept=concept,
        indexed_text_rows=indexed_text_rows,
        resolved_type_ids=resolved_type_ids,
        preview_cache=preview_cache,
    )


def load_concept_summary_renderer(concept_id: str) -> dict[str, Any]:
    """Resolve a concept-page summary renderer and build its payload."""

    concept = get_concept_by_concept_id(concept_id)
    if not isinstance(concept, Mapping):
        raise ConceptNotFoundError(f"Concept not found: {concept_id}")
    concept = enrich_concept_with_text_relations(dict(concept))
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        relationships = {}

    text_rows = get_texts_for_concept(subject_concept_id=concept_id, limit=250)
    direct_type_ids = _normalise_strings(relationships.get("is_an_instance_of"))
    ancestor_type_ids = _collect_ancestor_type_ids(direct_type_ids)
    resolved_type_ids = _dedupe_preserve_order(direct_type_ids + ancestor_type_ids)
    present_predicates = _collect_present_predicates(relationships, text_rows)

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
    preview_cache: dict[str, str] = {}
    indexed_text_rows = _index_text_rows(text_rows)
    panel = _build_panel_for_renderer(
        renderer_id=selected.renderer_id if selected else None,
        concept=concept,
        relationships=relationships,
        indexed_text_rows=indexed_text_rows,
        direct_type_ids=direct_type_ids,
        resolved_type_ids=resolved_type_ids,
        preview_cache=preview_cache,
    )

    return {
        "success": True,
        "concept_id": concept_id,
        "display_name": _display_name(concept),
        "direct_type_ids": direct_type_ids,
        "resolved_type_ids": resolved_type_ids,
        "selected_renderer": selected.to_dict() if selected else None,
        "panel": panel,
        "diagnostics": {
            "renderer_definition_loading": loading_diagnostics,
            "renderer_resolution": resolution.to_dict(),
        },
    }
