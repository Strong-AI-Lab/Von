"""Reusable launch-time input contracts for workflow execution.

This module lets a workflow concept declare how invocation-context data should
be mapped into workflow context keys before the initial state runs. The goal is
to keep selected-workflow handoff behaviour Vontology-defined rather than
hard-coded in orchestration branches.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Mapping, Tuple

WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION = "workflow_launch_input_contract.v1"

WORKFLOW_LAUNCH_INPUT_EXTRACTOR_IDENTITY = "identity"
WORKFLOW_LAUNCH_INPUT_EXTRACTOR_FIRST_QUOTED_TEXT = "first_quoted_text"
WORKFLOW_LAUNCH_INPUT_EXTRACTOR_WORKFLOW_ID_LIST = "workflow_id_list"
WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID = "arxiv_id"
_ALLOWED_EXTRACTORS: Tuple[str, ...] = (
    WORKFLOW_LAUNCH_INPUT_EXTRACTOR_IDENTITY,
    WORKFLOW_LAUNCH_INPUT_EXTRACTOR_FIRST_QUOTED_TEXT,
    WORKFLOW_LAUNCH_INPUT_EXTRACTOR_WORKFLOW_ID_LIST,
    WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID,
)

_QUOTED_TEXT_PATTERN = re.compile(
    r'"([^"]{1,8000})"|\'([^\']{1,8000})\'|“([^”]{1,8000})”|‘([^’]{1,8000})’',
    re.DOTALL,
)


@dataclass(frozen=True)
class WorkflowLaunchInputResolution:
    """Resolved launch-time input mapping outcome."""

    resolved_inputs: Mapping[str, Any]
    unresolved_required_inputs: tuple[str, ...]
    unresolved_optional_inputs: tuple[str, ...]
    diagnostics: Mapping[str, Any]


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


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _normalise_text(value).lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _lookup_path_value(root: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = root
    for segment in path.split("."):
        segment_clean = segment.strip()
        if not segment_clean:
            return False, None
        if not isinstance(current, Mapping) or segment_clean not in current:
            return False, None
        current = current.get(segment_clean)
    return True, current


def _resolve_source_expression(
    expression: str,
    *,
    inputs: Mapping[str, Any],
) -> tuple[bool, Any]:
    text = _normalise_text(expression)
    if not text:
        return False, None
    if text.startswith("inputs."):
        return _lookup_path_value(inputs, text.removeprefix("inputs."))
    if text == "inputs":
        return True, dict(inputs)
    return _lookup_path_value(inputs, text)


def _extract_first_quoted_text(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, str):
        return False, None
    for match in _QUOTED_TEXT_PATTERN.finditer(value):
        for group in match.groups():
            text = _normalise_text(group)
            if text:
                return True, text
    return False, None


def _extract_workflow_id_list(value: Any) -> tuple[bool, list[str] | None]:
    items = list(value) if isinstance(value, list) else [value]
    workflow_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        workflow_id = ""
        if isinstance(item, str):
            workflow_id = _normalise_text(item)
        elif isinstance(item, Mapping):
            for candidate_key in ("workflow_id", "concept_id", "id"):
                workflow_id = _normalise_text(item.get(candidate_key))
                if workflow_id:
                    break
        if not workflow_id:
            continue
        lowered = workflow_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        workflow_ids.append(workflow_id)
    if not workflow_ids:
        return False, None
    return True, workflow_ids


def _extract_arxiv_id(value: Any) -> tuple[bool, str | None]:
    try:
        from src.backend.services.arxiv_paper_link_service import (
            extract_arxiv_id_candidates,
        )
    except Exception:
        return False, None

    candidates = extract_arxiv_id_candidates(value)
    if not candidates:
        return False, None
    arxiv_id = _normalise_text(candidates[0])
    return bool(arxiv_id), arxiv_id or None


def _apply_extractor(
    *,
    extractor: str,
    value: Any,
) -> tuple[bool, Any, str]:
    extractor_name = (
        _normalise_text(extractor) or WORKFLOW_LAUNCH_INPUT_EXTRACTOR_IDENTITY
    )
    if extractor_name == WORKFLOW_LAUNCH_INPUT_EXTRACTOR_IDENTITY:
        if value is None:
            return False, None, "identity_value_missing"
        if isinstance(value, str) and not value.strip():
            return False, None, "identity_value_empty"
        if isinstance(value, (list, tuple, set, dict)) and len(value) == 0:
            return False, None, "identity_value_empty"
        return True, value, "resolved"
    if extractor_name == WORKFLOW_LAUNCH_INPUT_EXTRACTOR_FIRST_QUOTED_TEXT:
        found, extracted = _extract_first_quoted_text(value)
        return found, extracted, "resolved" if found else "quoted_text_not_found"
    if extractor_name == WORKFLOW_LAUNCH_INPUT_EXTRACTOR_WORKFLOW_ID_LIST:
        found, extracted = _extract_workflow_id_list(value)
        return found, extracted, "resolved" if found else "workflow_id_list_empty"
    if extractor_name == WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID:
        found, extracted = _extract_arxiv_id(value)
        return found, extracted, "resolved" if found else "arxiv_id_not_found"
    return False, None, "extractor_invalid"


def normalise_workflow_launch_input_contract(
    value: Any,
) -> tuple[Dict[str, Any] | None, str | None]:
    """Normalise a workflow launch input contract payload."""

    if not isinstance(value, Mapping):
        return None, "workflow_launch_input_contract_missing_or_not_mapping"

    schema_version = _normalise_text(value.get("schema_version"))
    if schema_version and schema_version != WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION:
        return None, "workflow_launch_input_contract_schema_unsupported"

    raw_mappings = value.get("input_mappings")
    if not isinstance(raw_mappings, list):
        return None, "workflow_launch_input_mappings_missing_or_not_list"

    explicit_required_inputs = set(_normalise_string_list(value.get("required_inputs")))
    mappings: List[Dict[str, Any]] = []
    seen_mappings: set[tuple[str, str, str]] = set()

    for item in raw_mappings:
        if not isinstance(item, Mapping):
            continue
        target_context_key = _normalise_text(
            item.get("target_context_key")
            or item.get("workflow_context_key")
            or item.get("context_key")
            or item.get("target_key")
        )
        source_expression = _normalise_text(
            item.get("source_expression")
            or item.get("source")
            or item.get("source_context_key")
            or item.get("source_path")
        )
        extractor = (
            _normalise_text(item.get("extractor"))
            or WORKFLOW_LAUNCH_INPUT_EXTRACTOR_IDENTITY
        )
        if not target_context_key or not source_expression:
            continue
        if extractor not in _ALLOWED_EXTRACTORS:
            return None, "workflow_launch_input_mapping_extractor_invalid"
        mapping_signature = (target_context_key, source_expression, extractor)
        if mapping_signature in seen_mappings:
            continue
        seen_mappings.add(mapping_signature)
        required = _coerce_bool(
            item.get("required"),
            default=target_context_key in explicit_required_inputs,
        )
        mapping_payload: Dict[str, Any] = {
            "target_context_key": target_context_key,
            "source_expression": source_expression,
            "extractor": extractor,
            "required": required,
        }
        description = _normalise_text(item.get("description"))
        if description:
            mapping_payload["description"] = description
        mappings.append(mapping_payload)

    if not mappings:
        return None, "workflow_launch_input_mappings_empty"

    required_inputs = [
        mapping["target_context_key"]
        for mapping in mappings
        if bool(mapping.get("required"))
    ]
    for item in _normalise_string_list(value.get("required_inputs")):
        if item not in required_inputs:
            required_inputs.append(item)

    return (
        {
            "schema_version": WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION,
            "input_mappings": mappings,
            "required_inputs": required_inputs,
        },
        None,
    )


def resolve_workflow_launch_inputs(
    *,
    workflow_id: str,
    contract: Mapping[str, Any] | None,
    inputs: Mapping[str, Any],
    contract_source: str | None = None,
) -> WorkflowLaunchInputResolution:
    """Resolve workflow launch inputs from invocation context."""

    normalised_contract, contract_error = normalise_workflow_launch_input_contract(
        contract
    )
    contract_present = isinstance(contract, Mapping)
    if normalised_contract is None:
        diagnostics: Dict[str, Any] = {
            "schema_version": "workflow_launch_input_resolution.v1",
            "workflow_id": _normalise_text(workflow_id),
            "status": "no_contract" if not contract_present else "invalid_contract",
            "contract_source": _normalise_text(contract_source) or None,
            "required_inputs": [],
            "resolved_inputs": [],
            "unresolved_required_inputs": [],
            "unresolved_optional_inputs": [],
            "mappings": [],
        }
        if contract_error:
            diagnostics["contract_error"] = contract_error
        return WorkflowLaunchInputResolution(
            resolved_inputs={},
            unresolved_required_inputs=(),
            unresolved_optional_inputs=(),
            diagnostics=diagnostics,
        )

    required_inputs = {
        item
        for item in normalised_contract.get("required_inputs", [])
        if isinstance(item, str) and item.strip()
    }
    resolved_inputs: Dict[str, Any] = {}
    unresolved_required_inputs: list[str] = []
    unresolved_optional_inputs: list[str] = []
    mapping_diagnostics: list[Dict[str, Any]] = []
    mapping_targets: set[str] = set()
    required_targets: set[str] = set(required_inputs)
    unresolved_optional_targets: set[str] = set()

    for mapping in normalised_contract.get("input_mappings", []):
        if not isinstance(mapping, Mapping):
            continue
        target_context_key = _normalise_text(mapping.get("target_context_key"))
        source_expression = _normalise_text(mapping.get("source_expression"))
        extractor = (
            _normalise_text(mapping.get("extractor"))
            or WORKFLOW_LAUNCH_INPUT_EXTRACTOR_IDENTITY
        )
        required = bool(mapping.get("required")) or target_context_key in required_inputs
        if target_context_key:
            mapping_targets.add(target_context_key)
        if required and target_context_key:
            required_targets.add(target_context_key)
        source_found, source_value = _resolve_source_expression(
            source_expression,
            inputs=inputs,
        )
        mapping_entry: Dict[str, Any] = {
            "target_context_key": target_context_key,
            "source_expression": source_expression,
            "extractor": extractor,
            "required": required,
            "source_found": source_found,
        }
        if target_context_key in resolved_inputs:
            mapping_entry["resolved"] = True
            mapping_entry["resolution_reason"] = "target_already_resolved"
            mapping_diagnostics.append(mapping_entry)
            continue
        if not source_found:
            mapping_entry["resolved"] = False
            mapping_entry["resolution_reason"] = "source_missing"
            if not required and target_context_key:
                unresolved_optional_targets.add(target_context_key)
            mapping_diagnostics.append(mapping_entry)
            continue

        resolved, extracted_value, resolution_reason = _apply_extractor(
            extractor=extractor,
            value=source_value,
        )
        mapping_entry["resolved"] = resolved
        mapping_entry["resolution_reason"] = resolution_reason
        if resolved:
            resolved_inputs[target_context_key] = extracted_value
        else:
            if not required and target_context_key:
                unresolved_optional_targets.add(target_context_key)
        mapping_diagnostics.append(mapping_entry)

    unresolved_required_inputs = [
        target
        for target in sorted(required_targets)
        if target and target not in resolved_inputs
    ]
    unresolved_optional_inputs = [
        target
        for target in sorted(
            unresolved_optional_targets | (mapping_targets - required_targets)
        )
        if target and target not in resolved_inputs
    ]

    diagnostics = {
        "schema_version": "workflow_launch_input_resolution.v1",
        "workflow_id": _normalise_text(workflow_id),
        "status": "failed" if unresolved_required_inputs else "resolved",
        "contract_source": _normalise_text(contract_source) or None,
        "required_inputs": sorted(required_inputs),
        "resolved_inputs": sorted(resolved_inputs.keys()),
        "unresolved_required_inputs": sorted(dict.fromkeys(unresolved_required_inputs)),
        "unresolved_optional_inputs": sorted(dict.fromkeys(unresolved_optional_inputs)),
        "mappings": mapping_diagnostics,
    }
    return WorkflowLaunchInputResolution(
        resolved_inputs=resolved_inputs,
        unresolved_required_inputs=tuple(
            sorted(dict.fromkeys(unresolved_required_inputs))
        ),
        unresolved_optional_inputs=tuple(
            sorted(dict.fromkeys(unresolved_optional_inputs))
        ),
        diagnostics=diagnostics,
    )


__all__ = [
    "WORKFLOW_LAUNCH_INPUT_CONTRACT_SCHEMA_VERSION",
    "WORKFLOW_LAUNCH_INPUT_EXTRACTOR_IDENTITY",
    "WORKFLOW_LAUNCH_INPUT_EXTRACTOR_FIRST_QUOTED_TEXT",
    "WORKFLOW_LAUNCH_INPUT_EXTRACTOR_WORKFLOW_ID_LIST",
    "WORKFLOW_LAUNCH_INPUT_EXTRACTOR_ARXIV_ID",
    "WorkflowLaunchInputResolution",
    "normalise_workflow_launch_input_contract",
    "resolve_workflow_launch_inputs",
]
