"""Deterministic target-contract validation for symbolic tool arguments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .tool_metadata_service import get_tool_required_obligation_metadata
from ..workflows.turn_target_contract import (
    TARGET_BINDING_ENTITY,
    TARGET_BINDING_TYPE,
    TARGET_BINDING_UNKNOWN,
    TARGET_MATCH_EXACT,
    TurnTargetContract,
    dedupe_target_contracts,
    extract_target_contracts_from_payload,
)

TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION = "tool_target_contract_validation.v1"

TARGET_CONTRACT_SYMBOLIC_MISMATCH = "target_contract_symbolic_mismatch"
TARGET_CONTRACT_SYMBOLIC_TARGET_MISSING = "target_contract_symbolic_target_missing"
TARGET_CONTRACT_UNRESOLVED_FOR_SYMBOLIC_TOOL = (
    "target_contract_unresolved_for_symbolic_tool"
)
TARGET_CONTRACT_AMBIGUOUS_FOR_SYMBOLIC_TOOL = (
    "target_contract_ambiguous_for_symbolic_tool"
)

TYPE_TARGET_ARGUMENT_NAMES = frozenset(
    {
        "class_id",
        "class_ids",
        "instance_of",
        "instance_type",
        "instance_type_id",
        "requested_extent_type_id",
        "requested_extent_type_ids",
        "requested_type_id",
        "requested_type_ids",
        "target_type",
        "target_type_id",
        "target_type_ids",
        "type",
        "type_id",
        "type_ids",
    }
)

ENTITY_TARGET_ARGUMENT_NAMES = frozenset(
    {
        "argument",
        "concept",
        "concept_id",
        "concept_ids",
        "focal_concept",
        "focal_concept_id",
        "source",
        "source_id",
        "target",
        "target_concept",
        "target_concept_id",
        "target_id",
    }
)

HierarchyMatchResolver = Callable[[str, str, str], bool]


@dataclass(frozen=True)
class PlannedTargetValue:
    field: str
    value: str

    def to_payload(self) -> dict[str, str]:
        return {"field": self.field, "value": self.value}


@dataclass(frozen=True)
class ToolTargetContractValidationResult:
    ok: bool
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def errors(self) -> tuple[dict[str, Any], ...]:
        return self.diagnostics

    def first_error_code(self) -> str:
        for diagnostic in self.diagnostics:
            error_code = str(diagnostic.get("error_code") or "").strip()
            if error_code:
                return error_code
        return ""

    def first_message(self) -> str:
        for diagnostic in self.diagnostics:
            message = str(diagnostic.get("message") or "").strip()
            if message:
                return message
        return ""


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value or "").strip()


def _normalise_sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return (value,)


def _value_at_path(source: Mapping[str, Any], path: str) -> Any:
    current: Any = source
    for part in path.split("."):
        key = part.strip()
        if not key:
            return None
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _extract_symbolic_values(value: Any) -> tuple[str, ...]:
    values: list[str] = []
    for item in _normalise_sequence(value):
        if isinstance(item, Mapping):
            for key in ("concept_id", "target_concept_id", "id", "instance_of"):
                nested = item.get(key)
                if isinstance(nested, str) and nested.strip().startswith("#V#"):
                    values.append(nested.strip())
        elif isinstance(item, str) and item.strip().startswith("#V#"):
            values.append(item.strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(value)
    return tuple(ordered)


def _planned_target_values(
    payload: Mapping[str, Any],
    *,
    field_names: Sequence[str],
) -> tuple[PlannedTargetValue, ...]:
    values: list[PlannedTargetValue] = []
    seen: set[tuple[str, str]] = set()
    for raw_field_name in field_names:
        field_name = _safe_str(raw_field_name)
        if not field_name:
            continue
        raw_value = _value_at_path(payload, field_name)
        for value in _extract_symbolic_values(raw_value):
            key = (field_name.lower(), value.lower())
            if key in seen:
                continue
            seen.add(key)
            values.append(PlannedTargetValue(field=field_name, value=value))
    return tuple(values)


def _contract_payloads(
    contracts: Sequence[TurnTargetContract],
) -> list[dict[str, Any]]:
    return [contract.to_state_payload() for contract in contracts]


def _target_argument_names(tool_name: str) -> tuple[str, ...]:
    metadata = get_tool_required_obligation_metadata(tool_name)
    return tuple(metadata.target_argument_names or metadata.target_payload_field_names)


def _field_matches_binding_kind(field_name: str, binding_kind: str) -> bool:
    lowered = field_name.strip().lower()
    if binding_kind == TARGET_BINDING_UNKNOWN:
        return True
    if binding_kind == TARGET_BINDING_TYPE:
        return lowered in TYPE_TARGET_ARGUMENT_NAMES
    if binding_kind == TARGET_BINDING_ENTITY:
        if lowered in TYPE_TARGET_ARGUMENT_NAMES:
            return False
        return lowered in ENTITY_TARGET_ARGUMENT_NAMES or bool(lowered)
    return True


def _symbolic_value_matches_contract(
    *,
    planned: PlannedTargetValue,
    contract: TurnTargetContract,
    hierarchy_match_resolver: HierarchyMatchResolver | None,
) -> bool:
    if not _field_matches_binding_kind(planned.field, contract.binding_kind):
        return False
    planned_lower = planned.value.lower()
    for expected in contract.concept_ids:
        if planned_lower == expected.lower():
            return True
        if contract.matching_policy == TARGET_MATCH_EXACT:
            continue
        if hierarchy_match_resolver is None:
            continue
        if hierarchy_match_resolver(planned.value, expected, contract.matching_policy):
            return True
    return False


def _diagnostic(
    *,
    tool_name: str,
    error_code: str,
    message: str,
    payload: Mapping[str, Any],
    target_argument_names: Sequence[str],
    target_contracts: Sequence[TurnTargetContract],
    planned_targets: Sequence[PlannedTargetValue] = (),
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "schema_version": TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION,
        "status": "invalid",
        "tool": tool_name,
        "error_code": error_code,
        "message": message,
        "payload": dict(payload),
        "contract": {
            "target_contracts": _contract_payloads(target_contracts),
        },
        "target_argument_names": list(target_argument_names),
        "target_contracts": _contract_payloads(target_contracts),
    }
    if planned_targets:
        diagnostic["planned_targets"] = [
            planned_target.to_payload() for planned_target in planned_targets
        ]
    return diagnostic


def _unresolved_error_code(contracts: Sequence[TurnTargetContract]) -> str:
    for contract in contracts:
        if (
            contract.resolution_status == "ambiguous"
            or len(contract.candidate_concept_ids) > 1
        ):
            return TARGET_CONTRACT_AMBIGUOUS_FOR_SYMBOLIC_TOOL
    return TARGET_CONTRACT_UNRESOLVED_FOR_SYMBOLIC_TOOL


def validate_tool_target_contract(
    *,
    tool_name: str,
    payload: Mapping[str, Any],
    target_contract_state: Any,
    hierarchy_match_resolver: HierarchyMatchResolver | None = None,
) -> ToolTargetContractValidationResult:
    """Validate planned symbolic target arguments against authored target state."""

    clean_tool_name = _safe_str(tool_name)
    if not clean_tool_name or not isinstance(payload, Mapping):
        return ToolTargetContractValidationResult(ok=True)

    target_argument_names = _target_argument_names(clean_tool_name)
    if not target_argument_names:
        return ToolTargetContractValidationResult(ok=True)

    contracts = extract_target_contracts_from_payload(target_contract_state)
    if not contracts:
        return ToolTargetContractValidationResult(ok=True)

    unresolved_contracts = [
        contract
        for contract in contracts
        if contract.requires_resolution_for_symbolic_tool()
    ]
    if unresolved_contracts:
        error_code = _unresolved_error_code(unresolved_contracts)
        diagnostic = _diagnostic(
            tool_name=clean_tool_name,
            error_code=error_code,
            message=(
                f"Tool '{clean_tool_name}' requires a resolved symbolic target, "
                "but the expected target contract is unresolved or ambiguous."
            ),
            payload=payload,
            target_argument_names=target_argument_names,
            target_contracts=unresolved_contracts,
        )
        return ToolTargetContractValidationResult(ok=False, diagnostics=(diagnostic,))

    resolved_contracts = [
        contract for contract in contracts if contract.is_symbolically_resolved()
    ]
    if not resolved_contracts:
        return ToolTargetContractValidationResult(ok=True)

    planned_targets = _planned_target_values(
        payload,
        field_names=target_argument_names,
    )
    if not planned_targets:
        diagnostic = _diagnostic(
            tool_name=clean_tool_name,
            error_code=TARGET_CONTRACT_SYMBOLIC_TARGET_MISSING,
            message=(
                f"Tool '{clean_tool_name}' requires one of "
                f"{list(target_argument_names)} to carry the resolved target."
            ),
            payload=payload,
            target_argument_names=target_argument_names,
            target_contracts=resolved_contracts,
        )
        return ToolTargetContractValidationResult(ok=False, diagnostics=(diagnostic,))

    mismatches = [
        planned
        for planned in planned_targets
        if not any(
            _symbolic_value_matches_contract(
                planned=planned,
                contract=contract,
                hierarchy_match_resolver=hierarchy_match_resolver,
            )
            for contract in resolved_contracts
        )
    ]
    if mismatches:
        diagnostic = _diagnostic(
            tool_name=clean_tool_name,
            error_code=TARGET_CONTRACT_SYMBOLIC_MISMATCH,
            message=(
                f"Tool '{clean_tool_name}' planned target arguments do not match "
                "the resolved expected target contract."
            ),
            payload=payload,
            target_argument_names=target_argument_names,
            target_contracts=resolved_contracts,
            planned_targets=mismatches,
        )
        return ToolTargetContractValidationResult(ok=False, diagnostics=(diagnostic,))

    return ToolTargetContractValidationResult(ok=True)


def target_contract_state_from_context(context: Any) -> dict[str, Any] | None:
    """Return the merged target-contract state visible in a workflow context."""

    if not isinstance(context, Mapping):
        return None
    contracts: list[TurnTargetContract] = []
    for raw_state in (
        context.get("turn_expected_outcome_contract_state"),
        context.get("turn_expected_outcome_profile"),
        context.get("turn_expected_outcome_contract"),
        context,
    ):
        contracts.extend(extract_target_contracts_from_payload(raw_state))
    deduped = dedupe_target_contracts(contracts)
    if not deduped:
        return None
    return {
        "schema_version": "turn_target_contract_context.v1",
        "target_contracts": [contract.to_state_payload() for contract in deduped],
    }


__all__ = [
    "TARGET_CONTRACT_AMBIGUOUS_FOR_SYMBOLIC_TOOL",
    "TARGET_CONTRACT_SYMBOLIC_MISMATCH",
    "TARGET_CONTRACT_SYMBOLIC_TARGET_MISSING",
    "TARGET_CONTRACT_UNRESOLVED_FOR_SYMBOLIC_TOOL",
    "TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION",
    "ToolTargetContractValidationResult",
    "target_contract_state_from_context",
    "validate_tool_target_contract",
]
