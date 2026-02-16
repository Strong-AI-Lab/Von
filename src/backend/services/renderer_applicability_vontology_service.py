"""Vontology-backed helpers for renderer applicability resolution.

This module keeps ontology fetch/normalisation separate from the pure
deterministic resolver in ``renderer_applicability_service``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .concept_service import get_concept_by_concept_id
from .renderer_applicability_service import RendererProfile
from .text_value_service import get_texts_for_concept
from .text_value_service import upsert_singleton_text_relation

_DEFAULT_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_renderer_profile_json",
    "has_renderer_profile_json",
    "hasRendererProfileJson",
    "hasContent",
)

DEFAULT_RENDERER_PROFILE_PREDICATE = "#V#has_renderer_profile_json"

_STRUCTURAL_RELATIONSHIP_KEYS: frozenset[str] = frozenset(
    {
        "is_a_type_of",
        "is_an_instance_of",
        "has_subtype",
        "has_instance",
        "related_to",
    }
)


def _normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    seen: set[str] = set()
    normalised: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalised.append(text)
    return tuple(normalised)


def _relationship_has_values(raw: Any) -> bool:
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, list):
        return any(isinstance(item, str) and item.strip() for item in raw)
    if isinstance(raw, tuple):
        return any(isinstance(item, str) and item.strip() for item in raw)
    return raw is not None


def _parse_renderer_profile_json(raw_text: Any) -> list[dict[str, Any]]:
    """Parse JSON renderer profile payloads from text relation values."""
    if not isinstance(raw_text, str):
        return []
    text = raw_text.strip()
    if not text:
        return []

    def _coerce_parsed(parsed: Any) -> list[dict[str, Any]]:
        if isinstance(parsed, Mapping):
            return [dict(parsed)]
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, Mapping)]
        return []

    try:
        parsed = json.loads(text)
        coerced = _coerce_parsed(parsed)
        if coerced:
            return coerced
    except Exception:
        pass

    # Accept fenced JSON blocks inside hasContent text.
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except Exception:
            continue
        coerced = _coerce_parsed(parsed)
        if coerced:
            return coerced
    return []


def load_renderer_definitions_from_concept_ids(
    concept_ids: Sequence[str],
    *,
    profile_text_predicates: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load renderer definitions from Vontology concept text relations."""
    predicates = _normalise_strings(profile_text_predicates) or _DEFAULT_PROFILE_TEXT_PREDICATES
    ordered_concept_ids = _normalise_strings(concept_ids)

    definitions: list[dict[str, Any]] = []
    loaded_concept_ids: list[str] = []
    unresolved_concept_ids: list[str] = []
    missing_profile_concept_ids: list[str] = []

    for concept_id in ordered_concept_ids:
        try:
            concept = get_concept_by_concept_id(concept_id)
        except Exception:
            concept = None

        if not isinstance(concept, Mapping):
            unresolved_concept_ids.append(concept_id)
            continue

        concept_definitions: list[dict[str, Any]] = []
        for predicate in predicates:
            try:
                text_rows = get_texts_for_concept(concept_id, predicate=predicate, limit=20)
            except Exception:
                text_rows = []
            if not text_rows:
                continue
            for row in text_rows:
                for profile in _parse_renderer_profile_json(row.get("text")):
                    normalised = dict(profile)
                    normalised.setdefault("renderer_id", concept_id)
                    normalised.setdefault("_source_concept_id", concept_id)
                    normalised.setdefault("_source_predicate", predicate)
                    concept_definitions.append(normalised)
            if concept_definitions:
                break

        if concept_definitions:
            definitions.extend(concept_definitions)
            loaded_concept_ids.append(concept_id)
        else:
            missing_profile_concept_ids.append(concept_id)

    diagnostics = {
        "renderer_definition_source": "vontology_concept_text_relations",
        "requested_concept_ids": list(ordered_concept_ids),
        "profile_text_predicates_checked": list(predicates),
        "loaded_concept_ids": loaded_concept_ids,
        "unresolved_concept_ids": unresolved_concept_ids,
        "missing_profile_concept_ids": missing_profile_concept_ids,
        "loaded_definition_count": len(definitions),
    }
    return definitions, diagnostics


def enrich_request_payload_from_concept(
    request_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill concept-backed request fields using ontology concept data."""
    enriched = dict(request_payload) if isinstance(request_payload, Mapping) else {}
    concept_id = str(enriched.get("concept_id") or "").strip()
    diagnostics: dict[str, Any] = {
        "concept_lookup_attempted": bool(concept_id),
        "concept_lookup_concept_id": concept_id or None,
        "filled_fields": [],
    }
    if not concept_id:
        return enriched, diagnostics

    try:
        concept = get_concept_by_concept_id(concept_id)
    except Exception:
        concept = None

    if not isinstance(concept, Mapping):
        diagnostics["concept_lookup_succeeded"] = False
        return enriched, diagnostics

    diagnostics["concept_lookup_succeeded"] = True
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        relationships = {}

    if not _normalise_strings(enriched.get("concept_type_ids")):
        concept_type_ids = _normalise_strings(relationships.get("is_an_instance_of"))
        if concept_type_ids:
            enriched["concept_type_ids"] = list(concept_type_ids)
            diagnostics["filled_fields"].append("concept_type_ids")

    if not _normalise_strings(enriched.get("present_predicates")):
        predicate_ids: set[str] = set()
        for predicate, raw_targets in relationships.items():
            if not isinstance(predicate, str) or predicate in _STRUCTURAL_RELATIONSHIP_KEYS:
                continue
            if _relationship_has_values(raw_targets):
                predicate_ids.add(predicate)

        try:
            text_rows = get_texts_for_concept(concept_id, limit=250)
        except Exception:
            text_rows = []
        for row in text_rows:
            predicate = row.get("predicate")
            if isinstance(predicate, str) and predicate.strip():
                predicate_ids.add(predicate.strip())

        if predicate_ids:
            enriched["present_predicates"] = sorted(predicate_ids)
            diagnostics["filled_fields"].append("present_predicates")

    if not str(enriched.get("object_kind") or "").strip() and "transient_microtheory" not in enriched:
        enriched["object_kind"] = "concept"
        diagnostics["filled_fields"].append("object_kind")

    return enriched, diagnostics


def upsert_renderer_profile(
    *,
    renderer_concept_id: str,
    renderer_profile: Mapping[str, Any],
    predicate: str = DEFAULT_RENDERER_PROFILE_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
) -> dict[str, Any]:
    """Validate and persist a renderer profile as a singleton text relation."""
    concept_id = str(renderer_concept_id or "").strip()
    if not concept_id:
        raise ValueError("renderer_concept_id is required")

    concept = get_concept_by_concept_id(concept_id)
    if not isinstance(concept, Mapping):
        raise ValueError(f"Renderer concept not found: {concept_id}")

    if not isinstance(renderer_profile, Mapping):
        raise ValueError("renderer_profile must be a mapping")

    profile_payload = dict(renderer_profile)
    profile_payload.setdefault("renderer_id", concept_id)
    validated_profile = RendererProfile.from_mapping(profile_payload)

    serialisable_profile = {
        "renderer_id": validated_profile.renderer_id,
        "renderer_type": validated_profile.renderer_type,
        "modalities": list(validated_profile.modalities),
        "applies_to_object_kinds": list(validated_profile.applies_to_object_kinds),
        "applies_to_concept_type_ids": list(validated_profile.applies_to_concept_type_ids),
        "required_predicates": list(validated_profile.required_predicates),
        "required_context_tags": list(validated_profile.required_context_tags),
        "minimum_confidence": validated_profile.minimum_confidence,
        "priority": validated_profile.priority,
        "fallback_renderer_ids": list(validated_profile.fallback_renderer_ids),
    }

    json_text = json.dumps(serialisable_profile, ensure_ascii=True, sort_keys=True)
    write_result = upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=str(predicate or DEFAULT_RENDERER_PROFILE_PREDICATE),
        text=json_text,
        lang=str(language or "en-NZ"),
        policy=str(policy or "replace_others"),
        provenance=dict(provenance or {}),
        context=dict(context or {}),
        garbage_collect=bool(garbage_collect),
    )

    return {
        "success": bool(write_result.get("success")),
        "renderer_concept_id": concept_id,
        "predicate": write_result.get("predicate"),
        "language": write_result.get("language"),
        "renderer_profile": serialisable_profile,
        "text_relation": write_result,
    }
