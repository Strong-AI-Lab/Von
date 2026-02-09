"""Concept-reference metadata for chat debug/context payloads.

This module builds cartouche-style metadata (exists/kind/name, plus optional
direct supertypes) for ``#V#...`` references in chat messages.

Design note (JVNAUTOSCI-958):
- Reuse the same authoritative concept source as UI cartouches
  (``get_vontology_node_content``).
- Keep payloads deterministic and bounded so debug data cannot grow
  unboundedly as conversations get longer.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import get_texts_for_concept
from ..utils.concept_id_utils import canonicalise_vontology_concept_id
from ..vontology.utils_vontology import (
    get_concept_display_name_with_names_fallback,
    get_vontology_node_content,
)

_TOKEN_RE = re.compile(
    r"#V#([A-Za-z0-9_./:\-\u2013\u2014]+?)(?=[\s\"'`.,:;!?\)\]\}…]|$)"
)
_CARTOUCHE_ATTR_RE = re.compile(
    r"data-(?:full-)?concept-id=[\"'](#V#[A-Za-z0-9_./:\-\u2013\u2014]+)[\"']",
    flags=re.IGNORECASE,
)

_DEFAULT_MAX_CONCEPTS = 24
_DEFAULT_MAX_SUPERTYPES = 4


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bool_from_any(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _max_concepts_default() -> int:
    return _bounded_int(
        os.getenv("VON_CONTEXT_CONCEPT_METADATA_MAX_CONCEPTS"),
        _DEFAULT_MAX_CONCEPTS,
        minimum=1,
        maximum=200,
    )


def _max_supertypes_default() -> int:
    return _bounded_int(
        os.getenv("VON_CONTEXT_CONCEPT_METADATA_MAX_SUPERTYPES"),
        _DEFAULT_MAX_SUPERTYPES,
        minimum=0,
        maximum=20,
    )


def _include_supertypes_default() -> bool:
    return _bool_from_any(
        os.getenv("VON_CONTEXT_CONCEPT_METADATA_INCLUDE_SUPERTYPES"), True
    )


def _fallback_name_from_concept_id(concept_id: str) -> str:
    slug = concept_id[3:] if concept_id.startswith("#V#") else concept_id
    return slug.replace("_", " ").strip() or concept_id


def _normalise_reference_id(raw: str) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None

    if not cleaned.startswith("#V#"):
        cleaned = f"#V#{cleaned}"

    canonical = canonicalise_vontology_concept_id(cleaned)
    if isinstance(canonical, str) and canonical.startswith("#V#"):
        return canonical
    if cleaned.startswith("#V#"):
        return cleaned
    return None


def _extract_ids_from_text(text: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for match in _TOKEN_RE.finditer(text):
        concept_id = _normalise_reference_id(f"#V#{match.group(1)}")
        if concept_id and concept_id not in seen:
            seen.add(concept_id)
            found.append(concept_id)

    for match in _CARTOUCHE_ATTR_RE.finditer(text):
        concept_id = _normalise_reference_id(match.group(1))
        if concept_id and concept_id not in seen:
            seen.add(concept_id)
            found.append(concept_id)

    return found


def extract_concept_reference_ids_from_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    roles: Iterable[str] = ("user", "assistant"),
    max_concepts: int,
) -> tuple[list[str], int, bool]:
    """Extract unique concept IDs in first-seen order from chat messages."""

    allowed_roles = {str(role).strip().lower() for role in roles if str(role).strip()}

    concept_ids: list[str] = []
    seen: set[str] = set()
    messages_scanned = 0
    capped = False

    cap = _bounded_int(max_concepts, _DEFAULT_MAX_CONCEPTS, minimum=1, maximum=200)

    for message in messages:
        if not isinstance(message, Mapping):
            continue

        role = str(message.get("role", "")).strip().lower()
        if role not in allowed_roles:
            continue

        messages_scanned += 1
        content = message.get("content")
        if not isinstance(content, str):
            continue

        for concept_id in _extract_ids_from_text(content):
            if concept_id in seen:
                continue
            seen.add(concept_id)
            concept_ids.append(concept_id)
            if len(concept_ids) >= cap:
                capped = True
                return concept_ids, messages_scanned, capped

    return concept_ids, messages_scanned, capped


def _best_name_from_text_relations(concept_id: str) -> str | None:
    try:
        relation_rows = get_texts_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            limit=50,
        )
    except Exception:
        relation_rows = []

    if not isinstance(relation_rows, list) or not relation_rows:
        return None

    enriched_names = []
    for row in relation_rows:
        if not isinstance(row, Mapping):
            continue
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        context = row.get("context")
        context = context if isinstance(context, Mapping) else {}
        enriched_names.append(
            {
                "name": text.strip(),
                "language": row.get("lang", "en-NZ"),
                "type": context.get("name_type", "NL"),
            }
        )

    if not enriched_names:
        return None

    nl_names_en = [
        n
        for n in enriched_names
        if n.get("type") == "NL" and n.get("language") == "en-NZ"
    ]
    nl_names_any = [n for n in enriched_names if n.get("type") == "NL"]
    abbr_names = [n for n in enriched_names if n.get("type") == "ABBR"]

    if nl_names_en:
        return str(nl_names_en[0].get("name") or "").strip() or None
    if nl_names_any:
        return str(nl_names_any[0].get("name") or "").strip() or None
    if abbr_names:
        return str(abbr_names[0].get("name") or "").strip() or None

    fallback = str(enriched_names[0].get("name") or "").strip()
    return fallback or None


def _resolve_parent_name_map(parent_ids: Iterable[str]) -> dict[str, str]:
    unique_parent_ids = [
        cid
        for cid in dict.fromkeys(parent_ids)
        if isinstance(cid, str) and cid.startswith("#V#")
    ]
    if not unique_parent_ids:
        return {}

    parent_name_map: dict[str, str] = {}

    try:
        cursor = ConceptsRepository.find(
            {"concept_id": {"$in": unique_parent_ids}},
            {"concept_id": 1, "name": 1, "names": 1},
            limit=len(unique_parent_ids),
        )
    except Exception:
        cursor = []

    for row in cursor:
        if not isinstance(row, Mapping):
            continue
        concept_id = row.get("concept_id")
        if not isinstance(concept_id, str):
            continue
        try:
            display_name = get_concept_display_name_with_names_fallback(dict(row))
        except Exception:
            display_name = None
        if isinstance(display_name, str) and display_name.strip():
            parent_name_map[concept_id] = display_name.strip()

    for concept_id in unique_parent_ids:
        if concept_id in parent_name_map:
            continue
        text_name = _best_name_from_text_relations(concept_id)
        if text_name:
            parent_name_map[concept_id] = text_name
            continue
        parent_name_map[concept_id] = _fallback_name_from_concept_id(concept_id)

    return parent_name_map


def _lookup_concept_entry(
    concept_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return (entry, direct_parent_ids_for_types)."""

    try:
        node = get_vontology_node_content(concept_id, reconstruct_md=False)
    except Exception:
        node = {"error": "lookup_failed"}

    if not isinstance(node, Mapping) or node.get("error"):
        return None, []

    kind_raw = node.get("kind") or node.get("computed_kind")
    kind = str(kind_raw).strip().lower() if isinstance(kind_raw, str) else ""
    if kind not in {"type", "predicate", "individual"}:
        kind = "individual"

    display_name = (
        str(node.get("display_name")).strip()
        if isinstance(node.get("display_name"), str)
        else ""
    )
    preferred_name = _best_name_from_text_relations(concept_id) or display_name
    if not preferred_name:
        preferred_name = _fallback_name_from_concept_id(concept_id)

    parent_ids_raw = node.get("is_a_type_of")
    if isinstance(parent_ids_raw, list):
        direct_parent_ids = [
            parent_id
            for parent_id in parent_ids_raw
            if isinstance(parent_id, str) and parent_id.startswith("#V#")
        ]
    elif isinstance(parent_ids_raw, str) and parent_ids_raw.startswith("#V#"):
        direct_parent_ids = [parent_ids_raw]
    else:
        direct_parent_ids = []

    entry = {
        "concept_id": concept_id,
        "exists": True,
        "kind": kind,
        "name": preferred_name,
    }
    return entry, direct_parent_ids


def build_context_concept_reference_metadata(
    messages: Sequence[Mapping[str, Any]],
    *,
    source: str = "sent_context_user_assistant",
    max_concepts: int | None = None,
    include_direct_supertypes: bool | None = None,
    max_direct_supertypes: int | None = None,
) -> dict[str, Any]:
    """Build bounded concept metadata for references in message context."""

    effective_max_concepts = (
        _max_concepts_default() if max_concepts is None else max_concepts
    )
    effective_include_supertypes = (
        _include_supertypes_default()
        if include_direct_supertypes is None
        else include_direct_supertypes
    )
    effective_max_supertypes = (
        _max_supertypes_default()
        if max_direct_supertypes is None
        else max_direct_supertypes
    )

    bounded_max_concepts = _bounded_int(
        effective_max_concepts,
        _DEFAULT_MAX_CONCEPTS,
        minimum=1,
        maximum=200,
    )
    bounded_max_supertypes = _bounded_int(
        effective_max_supertypes,
        _DEFAULT_MAX_SUPERTYPES,
        minimum=0,
        maximum=20,
    )

    concept_ids, messages_scanned, concept_count_capped = (
        extract_concept_reference_ids_from_messages(
            messages,
            max_concepts=bounded_max_concepts,
        )
    )

    entries: list[dict[str, Any]] = []
    parent_ids_needed: list[str] = []
    type_parent_map: dict[str, list[str]] = {}

    for concept_id in concept_ids:
        entry, parent_ids = _lookup_concept_entry(concept_id)
        if entry is None:
            entries.append(
                {
                    "concept_id": concept_id,
                    "exists": False,
                    "kind": None,
                    "name": None,
                }
            )
            continue

        entries.append(entry)

        if (
            effective_include_supertypes
            and entry.get("kind") == "type"
            and bounded_max_supertypes > 0
        ):
            limited_parent_ids = parent_ids[:bounded_max_supertypes]
            type_parent_map[concept_id] = limited_parent_ids
            parent_ids_needed.extend(limited_parent_ids)

    parent_name_map = (
        _resolve_parent_name_map(parent_ids_needed)
        if effective_include_supertypes and parent_ids_needed
        else {}
    )

    if effective_include_supertypes and bounded_max_supertypes > 0:
        for entry in entries:
            if entry.get("kind") != "type" or entry.get("exists") is not True:
                continue
            concept_id = entry.get("concept_id")
            if not isinstance(concept_id, str):
                continue
            parent_ids = type_parent_map.get(concept_id, [])
            entry["direct_supertypes"] = [
                {
                    "concept_id": parent_id,
                    "name": parent_name_map.get(
                        parent_id, _fallback_name_from_concept_id(parent_id)
                    ),
                }
                for parent_id in parent_ids
            ]

    return {
        "source": source,
        "metadata_version": 1,
        "message_roles": ["user", "assistant"],
        "messages_scanned": messages_scanned,
        "concept_count": len(entries),
        "concept_count_capped": concept_count_capped,
        "max_concepts": bounded_max_concepts,
        "include_direct_supertypes": bool(effective_include_supertypes),
        "max_direct_supertypes": bounded_max_supertypes,
        "concepts": entries,
    }
