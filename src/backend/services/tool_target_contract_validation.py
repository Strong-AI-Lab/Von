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
    extract_vontology_concept_ids_from_text,
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
    resolution_evidence: tuple[dict[str, Any], ...] = ()

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


_SUCCESSFUL_TOOL_INVOCATION_STATUSES = frozenset(
    {"ok", "success", "succeeded", "completed"}
)
_STRUCTURED_TOOL_RESULT_FIELDS = (
    "effective_payload",
    "result",
    "result_payload",
    "result_preview",
    "output",
    "outputs",
)
_STRUCTURED_FOCAL_CONCEPT_ID_FIELD_NAMES = frozenset(
    {
        "concept_id",
        "concept_ids",
        "focal_concept_id",
        "focal_concept_ids",
        "resolved_concept_id",
        "resolved_concept_ids",
    }
)
_STRUCTURED_FOCAL_RESULT_CONTAINER_FIELD_NAMES = frozenset({"result", "results"})


def _structured_result_mapping_failed(value: Mapping[str, Any]) -> bool:
    result_status = _safe_str(value.get("status")).lower()
    return bool(
        result_status in {"error", "failed", "failure", "blocked"}
        or value.get("success") is False
        or value.get("ok") is False
        or _safe_str(value.get("error"))
        or _safe_str(value.get("error_code"))
    )


def _invocation_completed_successfully(invocation: Mapping[str, Any]) -> bool:
    if bool(invocation.get("blocked")):
        return False
    if _safe_str(invocation.get("error")):
        return False

    status = _safe_str(invocation.get("status")).lower()
    if status and status not in _SUCCESSFUL_TOOL_INVOCATION_STATUSES:
        return False

    explicit_result_success = False
    for field_name in _STRUCTURED_TOOL_RESULT_FIELDS:
        result = invocation.get(field_name)
        if not isinstance(result, Mapping):
            continue
        if _structured_result_mapping_failed(result):
            return False
        result_status = _safe_str(result.get("status")).lower()
        if (
            result.get("success") is True
            or result.get("ok") is True
            or result_status in _SUCCESSFUL_TOOL_INVOCATION_STATUSES
        ):
            explicit_result_success = True
    return status in _SUCCESSFUL_TOOL_INVOCATION_STATUSES or explicit_result_success


def _structured_concept_id_evidence(
    value: Any,
    *,
    path: str,
    parent_field: str | None = None,
    focal_path_allowed: bool = True,
    depth: int = 0,
) -> list[tuple[str, str]]:
    if depth > 8:
        return []
    evidence: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        # A successful outer tool call may contain failed per-item result
        # branches. IDs inside those branches are not grounding evidence.
        if _structured_result_mapping_failed(value):
            return []
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 256 or not isinstance(raw_key, str):
                break
            key = raw_key.strip()
            if not key:
                continue
            item_path = f"{path}.{key}"
            item_is_mapping = isinstance(item, Mapping)
            item_is_sequence = isinstance(item, Sequence) and not isinstance(
                item, (str, bytes, bytearray)
            )
            child_focal_path_allowed = focal_path_allowed
            if item_is_mapping:
                child_focal_path_allowed = (
                    focal_path_allowed
                    and key.lower()
                    in _STRUCTURED_FOCAL_RESULT_CONTAINER_FIELD_NAMES
                )
            elif item_is_sequence:
                child_focal_path_allowed = focal_path_allowed and (
                    key.lower() in _STRUCTURED_FOCAL_RESULT_CONTAINER_FIELD_NAMES
                    or key.lower() in _STRUCTURED_FOCAL_CONCEPT_ID_FIELD_NAMES
                )
            evidence.extend(
                _structured_concept_id_evidence(
                    item,
                    path=item_path,
                    parent_field=key,
                    focal_path_allowed=child_focal_path_allowed,
                    depth=depth + 1,
                )
            )
        return evidence
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value[:256]):
            evidence.extend(
                _structured_concept_id_evidence(
                    item,
                    path=f"{path}[{index}]",
                    parent_field=parent_field,
                    focal_path_allowed=focal_path_allowed,
                    depth=depth + 1,
                )
            )
        return evidence
    if (
        isinstance(value, str)
        and focal_path_allowed
        and parent_field is not None
        and parent_field.strip().lower()
        in _STRUCTURED_FOCAL_CONCEPT_ID_FIELD_NAMES
    ):
        cleaned_value = value.strip()
        concept_ids = extract_vontology_concept_ids_from_text(cleaned_value)
        if len(concept_ids) == 1 and concept_ids[0] == cleaned_value:
            evidence.append((cleaned_value, path))
    return evidence


def successful_tool_result_concept_evidence(
    tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], ...]:
    """Return exact focal concept-ID evidence from successful tool results.

    Request arguments, result summaries, tool-message prose, and failed calls are
    intentionally excluded. Related type, class, parent, source, and target IDs
    are also excluded: their presence does not resolve the focal entity. This is
    a generic support boundary for represented target-resolution agreements; it
    does not decide whether an evidenced focal concept is semantically the right
    target.
    """

    receipts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for invocation in tool_invocations or ():
        if not isinstance(
            invocation, Mapping
        ) or not _invocation_completed_successfully(invocation):
            continue
        tool_name = _safe_str(invocation.get("tool")) or "unknown"
        call_id = _safe_str(invocation.get("call_id"))
        for field_name in _STRUCTURED_TOOL_RESULT_FIELDS:
            result = invocation.get(field_name)
            if not isinstance(result, (Mapping, Sequence)) or isinstance(
                result, (str, bytes, bytearray)
            ):
                continue
            for concept_id, result_path in _structured_concept_id_evidence(
                result,
                path=field_name,
            ):
                fingerprint = (
                    concept_id.lower(),
                    tool_name.lower(),
                    call_id,
                    result_path,
                )
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                receipt: dict[str, Any] = {
                    "concept_id": concept_id,
                    "tool": tool_name,
                    "result_path": result_path,
                }
                if call_id:
                    receipt["call_id"] = call_id
                receipts.append(receipt)
    return tuple(receipts)


def _provisional_resolution_evidence(
    *,
    tool_name: str,
    planned_targets: Sequence[PlannedTargetValue],
    unresolved_contracts: Sequence[TurnTargetContract],
    prior_tool_invocations: Sequence[Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], ...]:
    metadata = get_tool_required_obligation_metadata(tool_name)
    if metadata.operation_class != "verification_read" or not planned_targets:
        return ()
    if any(
        contract.resolution_status == "ambiguous"
        or contract.candidate_concept_ids
        or contract.kind != "natural_language"
        for contract in unresolved_contracts
    ):
        return ()
    evidence = successful_tool_result_concept_evidence(prior_tool_invocations)
    by_concept_id: dict[str, list[dict[str, Any]]] = {}
    for receipt in evidence:
        concept_id = _safe_str(receipt.get("concept_id"))
        if concept_id:
            by_concept_id.setdefault(concept_id.lower(), []).append(receipt)
    matched_receipts: list[dict[str, Any]] = []
    for planned in planned_targets:
        matches = by_concept_id.get(planned.value.lower()) or []
        if not matches:
            return ()
        matched_receipts.extend(matches)
    return tuple(matched_receipts)


def validate_tool_target_contract(
    *,
    tool_name: str,
    payload: Mapping[str, Any],
    target_contract_state: Any,
    hierarchy_match_resolver: HierarchyMatchResolver | None = None,
    prior_tool_invocations: Sequence[Mapping[str, Any]] | None = None,
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
        planned_targets = _planned_target_values(
            payload,
            field_names=target_argument_names,
        )
        provisional_evidence = _provisional_resolution_evidence(
            tool_name=clean_tool_name,
            planned_targets=planned_targets,
            unresolved_contracts=unresolved_contracts,
            prior_tool_invocations=prior_tool_invocations,
        )
        if provisional_evidence:
            return ToolTargetContractValidationResult(
                ok=True,
                resolution_evidence=(
                    {
                        "schema_version": "provisional_target_resolution.v1",
                        "status": "valid",
                        "tool": clean_tool_name,
                        "resolution_scope": "verification_read_only",
                        "planned_targets": [
                            planned.to_payload() for planned in planned_targets
                        ],
                        "evidence": [dict(item) for item in provisional_evidence],
                    },
                ),
            )
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
    "successful_tool_result_concept_evidence",
    "target_contract_state_from_context",
    "validate_tool_target_contract",
]
