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

# JVNAUTOSCI-1175: centralise canonical starter renderer profiles so bootstrap
# and diagnostics use one authoritative seed set.
_CANONICAL_RENDERER_PROFILE_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "renderer_id": "#V#timeline_renderer",
        "renderer_type": "timeline",
        "modalities": ["visual"],
        "applies_to_object_kinds": ["concept"],
        "applies_to_concept_type_ids": ["#V#task"],
        "required_predicates": ["#V#has_start_time"],
        "required_context_tags": ["workflow:tool_calling"],
        "priority": 90,
        "fallback_renderer_ids": ["#V#table_renderer"],
        "screen_element_families": ["timeline"],
    },
    {
        "renderer_id": "#V#workflow_renderer",
        "renderer_type": "workflow_view",
        "modalities": ["visual"],
        "applies_to_object_kinds": ["concept", "transient_microtheory"],
        "required_context_tags": ["workflow:tool_calling"],
        "priority": 80,
        "fallback_renderer_ids": ["#V#table_renderer"],
        "screen_element_families": ["workflow_view"],
    },
    {
        "renderer_id": "#V#table_renderer",
        "renderer_type": "table",
        "modalities": ["visual"],
        "applies_to_object_kinds": ["concept", "transient_microtheory"],
        "priority": 60,
        "screen_element_families": ["table"],
    },
    {
        "renderer_id": "#V#narration_renderer",
        "renderer_type": "narration",
        "modalities": ["narrated_audio", "textual"],
        "applies_to_object_kinds": ["concept", "transient_microtheory"],
        "required_context_tags": ["presenter_mode"],
        "priority": 95,
        "screen_element_families": [],
    },
)
_CANONICAL_RENDERER_PROFILE_BY_ID: dict[str, dict[str, Any]] = {
    str(item["renderer_id"]): dict(item) for item in _CANONICAL_RENDERER_PROFILE_BLUEPRINTS
}

_STRUCTURAL_RELATIONSHIP_KEYS: frozenset[str] = frozenset(
    {
        "is_a_type_of",
        "is_an_instance_of",
        "has_subtype",
        "has_instance",
        "related_to",
    }
)


def canonical_renderer_profile_concept_ids() -> tuple[str, ...]:
    """Return canonical renderer profile concept IDs in deterministic order."""
    return tuple(str(item["renderer_id"]) for item in _CANONICAL_RENDERER_PROFILE_BLUEPRINTS)


def canonical_renderer_profile_blueprints() -> tuple[dict[str, Any], ...]:
    """Return copy-on-read canonical renderer starter profiles."""
    return tuple(dict(item) for item in _CANONICAL_RENDERER_PROFILE_BLUEPRINTS)


def _text_preview(value: Any, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _format_parse_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if len(message) > 160:
        message = f"{message[:157]}..."
    return f"{exc.__class__.__name__}: {message}"


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
    profiles, _errors = _parse_renderer_profile_json_with_diagnostics(raw_text)
    return profiles


def _parse_renderer_profile_json_with_diagnostics(
    raw_text: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse JSON renderer profile payloads and return parse diagnostics."""
    if not isinstance(raw_text, str):
        return [], ["profile_text_not_string"]
    text = raw_text.strip()
    if not text:
        return [], ["profile_text_empty"]

    def _coerce_parsed(parsed: Any) -> list[dict[str, Any]]:
        if isinstance(parsed, Mapping):
            return [dict(parsed)]
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, Mapping)]
        return []

    parse_errors: list[str] = []
    try:
        parsed = json.loads(text)
        coerced = _coerce_parsed(parsed)
        if coerced:
            return coerced, []
        parse_errors.append("json_payload_not_mapping_or_list")
    except Exception as exc:
        parse_errors.append(f"json_decode_failed:{_format_parse_error(exc)}")

    # Accept fenced JSON blocks inside hasContent text.
    fenced_block_seen = False
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
        fenced_block_seen = True
        block = match.group(1).strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except Exception as exc:
            parse_errors.append(
                f"fenced_json_decode_failed:{_format_parse_error(exc)}"
            )
            continue
        coerced = _coerce_parsed(parsed)
        if coerced:
            return coerced, []
        parse_errors.append("fenced_json_payload_not_mapping_or_list")
    if fenced_block_seen and not parse_errors:
        parse_errors.append("fenced_json_block_empty")
    return [], parse_errors


def _serialise_renderer_profile(profile: RendererProfile) -> dict[str, Any]:
    return {
        "renderer_id": profile.renderer_id,
        "renderer_type": profile.renderer_type,
        "modalities": list(profile.modalities),
        "applies_to_object_kinds": list(profile.applies_to_object_kinds),
        "applies_to_concept_type_ids": list(profile.applies_to_concept_type_ids),
        "required_predicates": list(profile.required_predicates),
        "required_context_tags": list(profile.required_context_tags),
        "minimum_confidence": profile.minimum_confidence,
        "priority": profile.priority,
        "fallback_renderer_ids": list(profile.fallback_renderer_ids),
        "screen_element_families": list(profile.screen_element_families),
    }


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
    malformed_profile_concept_ids: list[str] = []
    malformed_profile_entries: list[dict[str, Any]] = []

    for concept_id in ordered_concept_ids:
        try:
            concept = get_concept_by_concept_id(concept_id)
        except Exception:
            concept = None

        if not isinstance(concept, Mapping):
            unresolved_concept_ids.append(concept_id)
            continue

        concept_definitions: list[dict[str, Any]] = []
        concept_malformed_entries: list[dict[str, Any]] = []
        profile_rows_found = False
        for predicate in predicates:
            try:
                text_rows = get_texts_for_concept(concept_id, predicate=predicate, limit=20)
            except Exception:
                text_rows = []
            if not text_rows:
                continue
            profile_rows_found = True
            for row in text_rows:
                parsed_profiles, parse_errors = _parse_renderer_profile_json_with_diagnostics(
                    row.get("text")
                )
                if parse_errors:
                    concept_malformed_entries.append(
                        {
                            "concept_id": concept_id,
                            "predicate": predicate,
                            "error": "; ".join(parse_errors),
                            "text_preview": _text_preview(row.get("text")),
                        }
                    )
                    continue
                for profile in parsed_profiles:
                    normalised = dict(profile)
                    normalised.setdefault("renderer_id", concept_id)
                    try:
                        validated = RendererProfile.from_mapping(normalised)
                    except Exception as exc:
                        concept_malformed_entries.append(
                            {
                                "concept_id": concept_id,
                                "predicate": predicate,
                                "error": str(exc),
                                "profile_preview": {
                                    key: normalised.get(key)
                                    for key in sorted(
                                        normalised.keys()
                                        & {
                                            "renderer_id",
                                            "renderer_type",
                                            "modalities",
                                            "applies_to_object_kinds",
                                            "required_predicates",
                                            "required_context_tags",
                                            "priority",
                                        }
                                    )
                                },
                            }
                        )
                        continue
                    serialised = _serialise_renderer_profile(validated)
                    serialised["_source_concept_id"] = concept_id
                    serialised["_source_predicate"] = predicate
                    concept_definitions.append(serialised)
            if concept_definitions:
                break

        if concept_definitions:
            definitions.extend(concept_definitions)
            loaded_concept_ids.append(concept_id)
        elif concept_malformed_entries:
            malformed_profile_concept_ids.append(concept_id)
            malformed_profile_entries.extend(concept_malformed_entries)
        elif profile_rows_found:
            # Text rows existed but none produced usable definitions.
            malformed_profile_concept_ids.append(concept_id)
        else:
            missing_profile_concept_ids.append(concept_id)

    diagnostics = {
        "renderer_definition_source": "vontology_concept_text_relations",
        "requested_concept_ids": list(ordered_concept_ids),
        "profile_text_predicates_checked": list(predicates),
        "loaded_concept_ids": loaded_concept_ids,
        "unresolved_concept_ids": unresolved_concept_ids,
        "missing_profile_concept_ids": missing_profile_concept_ids,
        "malformed_profile_concept_ids": malformed_profile_concept_ids,
        "malformed_profile_count": len(malformed_profile_entries),
        "malformed_profile_entries": malformed_profile_entries[:25],
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


def bootstrap_canonical_renderer_profiles(
    *,
    concept_ids: Sequence[str] | None = None,
    predicate: str = DEFAULT_RENDERER_PROFILE_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
) -> dict[str, Any]:
    """Persist canonical starter renderer profiles on existing concepts."""
    requested_concept_ids = _normalise_strings(concept_ids) or canonical_renderer_profile_concept_ids()

    persisted_profile_concept_ids: list[str] = []
    unknown_canonical_profile_concept_ids: list[str] = []
    missing_concept_ids: list[str] = []
    errors_by_concept_id: dict[str, str] = {}

    for concept_id in requested_concept_ids:
        seed_profile = _CANONICAL_RENDERER_PROFILE_BY_ID.get(concept_id)
        if not isinstance(seed_profile, Mapping):
            unknown_canonical_profile_concept_ids.append(concept_id)
            continue
        try:
            concept = get_concept_by_concept_id(concept_id)
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"concept_lookup_failed:{exc}"
            continue

        if not isinstance(concept, Mapping):
            missing_concept_ids.append(concept_id)
            continue

        try:
            result = upsert_renderer_profile(
                renderer_concept_id=concept_id,
                renderer_profile=dict(seed_profile),
                predicate=predicate,
                language=language,
                policy=policy,
                provenance=provenance,
                context=context,
                garbage_collect=garbage_collect,
            )
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"profile_upsert_failed:{exc}"
            continue
        if bool(result.get("success")):
            persisted_profile_concept_ids.append(concept_id)
        else:
            errors_by_concept_id[concept_id] = "profile_upsert_unsuccessful"

    return {
        "success": not (
            unknown_canonical_profile_concept_ids
            or missing_concept_ids
            or errors_by_concept_id
        ),
        "requested_profile_concept_ids": list(requested_concept_ids),
        "canonical_profile_concept_ids": list(canonical_renderer_profile_concept_ids()),
        "persisted_profile_concept_ids": persisted_profile_concept_ids,
        "unknown_canonical_profile_concept_ids": unknown_canonical_profile_concept_ids,
        "missing_concept_ids": missing_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
        "counts": {
            "requested": len(requested_concept_ids),
            "persisted": len(persisted_profile_concept_ids),
            "missing_concepts": len(missing_concept_ids),
            "unknown_canonical_ids": len(unknown_canonical_profile_concept_ids),
            "errors": len(errors_by_concept_id),
        },
    }


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

    serialisable_profile = _serialise_renderer_profile(validated_profile)

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
