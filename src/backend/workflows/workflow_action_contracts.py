from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Mapping, Sequence

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import get_texts_for_concept

WORKFLOW_ACTION_CONTRACT_TYPE_ID = "#V#workflow_action_contract"
WORKFLOW_ACTION_CONTRACT_SCHEMA_VERSION = "workflow_action_contract.v1"
WORKFLOW_ACTION_CONTRACT_SPEC_CONCEPT_DATA_KEY = "workflow_action_contract"
WORKFLOW_ACTION_CONTRACT_TEXT_PREDICATE_PRECEDENCE: tuple[tuple[str, ...], ...] = (
    (
        "#V#hasWorkflowActionContractJson",
        "hasWorkflowActionContractJson",
        "#V#has_workflow_action_contract_json",
        "has_workflow_action_contract_json",
    ),
)


def _normalise_non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalise_optional_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return dict(value)


def _normalise_optional_text_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    values: list[str] = []
    for item in value:
        text = _normalise_non_empty_text(item)
        if text:
            values.append(text)
    return tuple(dict.fromkeys(values))


def build_workflow_action_contract_payload(
    *,
    concept_id: str,
    action_id: str,
    description: str | None = None,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    side_effects: str | None = None,
    postconditions: Sequence[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": WORKFLOW_ACTION_CONTRACT_SCHEMA_VERSION,
        "concept_id": str(concept_id).strip(),
        "action_id": str(action_id).strip(),
    }
    description_text = _normalise_non_empty_text(description)
    if description_text:
        payload["description"] = description_text
    input_schema_object = _normalise_optional_object(input_schema)
    if input_schema_object:
        payload["input_schema"] = input_schema_object
    output_schema_object = _normalise_optional_object(output_schema)
    if output_schema_object:
        payload["output_schema"] = output_schema_object
    side_effects_text = _normalise_non_empty_text(side_effects)
    if side_effects_text:
        payload["side_effects"] = side_effects_text
    postcondition_values = _normalise_optional_text_list(postconditions or ())
    if postcondition_values:
        payload["postconditions"] = list(postcondition_values)
    return payload


def normalise_workflow_action_contract_payload(
    payload: Mapping[str, Any],
    *,
    fallback_concept_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None

    action_id = _normalise_non_empty_text(
        payload.get("action_id") or payload.get("registry_action_id")
    )
    if not action_id:
        return None

    concept_id = _normalise_non_empty_text(payload.get("concept_id"))
    if not concept_id:
        concept_id = _normalise_non_empty_text(fallback_concept_id)
    if not concept_id:
        return None

    schema_version = _normalise_non_empty_text(payload.get("schema_version"))
    if not schema_version:
        schema_version = WORKFLOW_ACTION_CONTRACT_SCHEMA_VERSION

    normalised = build_workflow_action_contract_payload(
        concept_id=concept_id,
        action_id=action_id,
        description=_normalise_non_empty_text(payload.get("description")),
        input_schema=_normalise_optional_object(payload.get("input_schema")),
        output_schema=_normalise_optional_object(payload.get("output_schema")),
        side_effects=_normalise_non_empty_text(payload.get("side_effects")),
        postconditions=_normalise_optional_text_list(payload.get("postconditions")),
    )
    normalised["schema_version"] = schema_version
    return normalised


def _parse_json_object_text_value(text_value: Any) -> dict[str, Any] | None:
    text = _normalise_non_empty_text(text_value)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if not isinstance(parsed, Mapping):
        return None
    return dict(parsed)


@lru_cache(maxsize=2048)
def _resolve_action_contract_payload_cached(
    concept_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    concept_id_text = _normalise_non_empty_text(concept_id)
    if not concept_id_text:
        return None, None

    try:
        texts = get_texts_for_concept(concept_id_text)
    except Exception:
        texts = []

    if isinstance(texts, list):
        for predicate_aliases in WORKFLOW_ACTION_CONTRACT_TEXT_PREDICATE_PRECEDENCE:
            for item in texts:
                if not isinstance(item, Mapping):
                    continue
                predicate = _normalise_non_empty_text(item.get("predicate"))
                if predicate not in predicate_aliases:
                    continue
                parsed = _parse_json_object_text_value(item.get("text"))
                if parsed is None:
                    continue
                normalised = normalise_workflow_action_contract_payload(
                    parsed,
                    fallback_concept_id=concept_id_text,
                )
                if normalised is not None:
                    return normalised, f"text_relation:{predicate}"

    concept_doc = ConceptsRepository.find_one(
        {"concept_id": concept_id_text},
        {"concept_id": 1, "concept_data": 1},
    )
    concept_data = concept_doc.get("concept_data") if isinstance(concept_doc, Mapping) else None
    if isinstance(concept_data, Mapping):
        parsed = concept_data.get(WORKFLOW_ACTION_CONTRACT_SPEC_CONCEPT_DATA_KEY)
        if isinstance(parsed, Mapping):
            normalised = normalise_workflow_action_contract_payload(
                parsed,
                fallback_concept_id=concept_id_text,
            )
            if normalised is not None:
                return normalised, "concept_data"

    return None, None


def resolve_workflow_action_contract_payload(
    concept_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    concept_id_text = _normalise_non_empty_text(concept_id)
    if not concept_id_text or not concept_id_text.startswith("#V#"):
        return None, None
    return _resolve_action_contract_payload_cached(concept_id_text)


def resolve_workflow_action_target(
    raw_target: str,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    target = _normalise_non_empty_text(raw_target)
    if not target:
        return None, None, None

    if target.startswith("#V#"):
        contract_payload, contract_source = resolve_workflow_action_contract_payload(
            target
        )
        if contract_payload is not None:
            resolved_action_id = _normalise_non_empty_text(
                contract_payload.get("action_id")
            )
            if resolved_action_id:
                return resolved_action_id, contract_payload, contract_source

        token = target[3:].strip()
        if token.endswith("_tool"):
            token = token[: -len("_tool")]
        elif token.endswith(" tool"):
            token = token[: -len(" tool")]
        token = token.strip()
        return token or None, None, contract_source

    return target, None, None


def invalidate_workflow_action_contract_resolution_cache() -> None:
    _resolve_action_contract_payload_cached.cache_clear()


__all__ = [
    "WORKFLOW_ACTION_CONTRACT_SCHEMA_VERSION",
    "WORKFLOW_ACTION_CONTRACT_SPEC_CONCEPT_DATA_KEY",
    "WORKFLOW_ACTION_CONTRACT_TEXT_PREDICATE_PRECEDENCE",
    "WORKFLOW_ACTION_CONTRACT_TYPE_ID",
    "build_workflow_action_contract_payload",
    "invalidate_workflow_action_contract_resolution_cache",
    "normalise_workflow_action_contract_payload",
    "resolve_workflow_action_contract_payload",
    "resolve_workflow_action_target",
]
