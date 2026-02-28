"""Shared constants/helpers for workflow subworkflow composition contracts."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Tuple

WORKFLOW_SUBWORKFLOW_ACTION_ID = "workflow_invoke_subworkflow"
WORKFLOW_SUBWORKFLOW_CONTRACT_SCHEMA_VERSION = "workflow_subworkflow_contract.v1"

WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE = "propagate_as_action_failure"
WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE = "capture_child_failure"
WORKFLOW_SUBWORKFLOW_ALLOWED_FAILURE_MODES: Tuple[str, ...] = (
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
)


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    result: List[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalise_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalise_input_mappings(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        child_input_key = _normalise_text(
            item.get("child_input_key")
            or item.get("input_key")
            or item.get("tool_param")
            or item.get("tool_param_name")
        )
        parent_context_key = _normalise_text(
            item.get("parent_context_key")
            or item.get("context_key")
            or item.get("source_context_key")
        )
        mapping_concept_id = _normalise_text(item.get("mapping_concept_id"))
        if not child_input_key or not parent_context_key:
            continue
        mapping: Dict[str, str] = {
            "child_input_key": child_input_key,
            "parent_context_key": parent_context_key,
        }
        if mapping_concept_id:
            mapping["mapping_concept_id"] = mapping_concept_id
        result.append(mapping)
    return result


def _normalise_output_mappings(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        child_output_field = _normalise_text(
            item.get("child_output_field")
            or item.get("output_field")
            or item.get("tool_output_field")
            or item.get("tool_output_field_name")
        )
        parent_context_key = _normalise_text(
            item.get("parent_context_key")
            or item.get("context_key")
            or item.get("target_context_key")
        )
        mapping_concept_id = _normalise_text(item.get("mapping_concept_id"))
        if not child_output_field or not parent_context_key:
            continue
        mapping: Dict[str, str] = {
            "child_output_field": child_output_field,
            "parent_context_key": parent_context_key,
        }
        if mapping_concept_id:
            mapping["mapping_concept_id"] = mapping_concept_id
        result.append(mapping)
    return result


def normalise_subworkflow_contract(
    value: Any,
) -> tuple[Dict[str, Any] | None, str | None]:
    """Normalise and validate subworkflow contract metadata payloads."""

    if not isinstance(value, Mapping):
        return None, "subworkflow_contract_missing_or_not_mapping"

    schema_version = _normalise_text(value.get("schema_version"))
    if schema_version and schema_version != WORKFLOW_SUBWORKFLOW_CONTRACT_SCHEMA_VERSION:
        return None, "subworkflow_contract_schema_unsupported"

    workflow_id = _normalise_text(value.get("workflow_id"))
    if not workflow_id:
        return None, "subworkflow_workflow_id_missing"

    failure_mode = (
        _normalise_text(value.get("failure_mode"))
        or WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE
    )
    if failure_mode not in WORKFLOW_SUBWORKFLOW_ALLOWED_FAILURE_MODES:
        return None, "subworkflow_failure_mode_invalid"

    input_mappings = _normalise_input_mappings(value.get("input_mappings"))
    output_mappings = _normalise_output_mappings(value.get("output_mappings"))

    provided_inputs = [item["child_input_key"] for item in input_mappings]
    mapped_outputs = [item["child_output_field"] for item in output_mappings]

    required_inputs = (
        _normalise_string_list(value.get("required_inputs")) or list(provided_inputs)
    )
    required_outputs = (
        _normalise_string_list(value.get("required_outputs")) or list(mapped_outputs)
    )

    return (
        {
            "schema_version": WORKFLOW_SUBWORKFLOW_CONTRACT_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "failure_mode": failure_mode,
            "input_mappings": input_mappings,
            "output_mappings": output_mappings,
            "provided_inputs": provided_inputs,
            "mapped_outputs": mapped_outputs,
            "required_inputs": required_inputs,
            "required_outputs": required_outputs,
        },
        None,
    )


def build_subworkflow_contract(
    *,
    workflow_id: str,
    input_mappings: List[Dict[str, str]],
    output_mappings: List[Dict[str, str]],
    failure_mode: str = WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
) -> Dict[str, Any]:
    """Build a canonical subworkflow contract payload from mapping lists."""

    contract, _ = normalise_subworkflow_contract(
        {
            "schema_version": WORKFLOW_SUBWORKFLOW_CONTRACT_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "failure_mode": failure_mode,
            "input_mappings": input_mappings,
            "output_mappings": output_mappings,
        }
    )
    if contract is None:
        return {
            "schema_version": WORKFLOW_SUBWORKFLOW_CONTRACT_SCHEMA_VERSION,
            "workflow_id": _normalise_text(workflow_id),
            "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE,
            "input_mappings": [],
            "output_mappings": [],
            "provided_inputs": [],
            "mapped_outputs": [],
            "required_inputs": [],
            "required_outputs": [],
        }
    return contract


__all__ = [
    "WORKFLOW_SUBWORKFLOW_ACTION_ID",
    "WORKFLOW_SUBWORKFLOW_CONTRACT_SCHEMA_VERSION",
    "WORKFLOW_SUBWORKFLOW_FAILURE_MODE_PROPAGATE",
    "WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE",
    "WORKFLOW_SUBWORKFLOW_ALLOWED_FAILURE_MODES",
    "normalise_subworkflow_contract",
    "build_subworkflow_contract",
]
