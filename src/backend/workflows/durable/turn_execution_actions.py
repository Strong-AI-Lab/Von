"""Action handlers for the Master Conversation-Turn workflow.

JVNAUTOSCI-1763:
- Cede turn control from Python routes to durable VWL workflows.
- Support supervised execution of capability workflows (e.g. arXiv).
- Keep selected-workflow answer content separate from execution reporting.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping, Sequence

from ..action_registry import (
    ActionSpec,
    ActionRegistry,
    WorkflowActionResult,
    WorkflowActionRequest,
)
from ..mcp_tool_bridge import (
    apply_runtime_defaults_to_mcp_payload,
    resolve_internal_mcp_tool_name,
)
from ..subworkflow_contracts import WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE
from ..workflow_selector import build_selector_call_prompt
from .turn_execution_runtime_support import (
    _bounded_snapshot,
    build_turn_execution_selected_workflow_outputs,
    build_turn_recovery_tool_batch_outputs,
    _coerce_non_empty_text,
    _summarise_tool_batch_result_payload,
    run_turn_execution_completion_gate,
    run_turn_execution_critic,
)
from ...services.tool_target_contract_validation import (
    successful_tool_result_concept_evidence,
    target_contract_state_from_context,
    validate_tool_target_contract,
)
from ..turn_target_contract import (
    TurnTargetContract,
    dedupe_target_contracts,
    extract_target_contracts_from_payload,
)

logger = logging.getLogger(__name__)

TURN_EXECUTION_ROUTE_ACTION_ID = "turn_execution.route"
TURN_EXECUTION_PREPARE_SELECTOR_CONTEXT_ACTION_ID = (
    "turn_execution.prepare_selector_context"
)
TURN_EXECUTION_EXECUTE_SELECTED_ACTION_ID = "turn_execution.execute_selected"
TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID = "turn_execution.execute_tool_batch"
TURN_EXECUTION_PREPARE_RECOVERY_RETRY_ACTION_ID = (
    "turn_execution.prepare_recovery_retry"
)
TURN_EXECUTION_CRITIC_ACTION_ID = "turn_execution.critic"
TURN_EXECUTION_COMPLETION_GATE_ACTION_ID = "turn_execution.completion_gate"
_DEFAULT_RECOVERY_TOOL_BATCH_CAP = 4
_MAX_RECOVERY_TOOL_BATCH_CAP = 8
_DISALLOWED_DIRECT_TOOL_BATCH_ACTION_PREFIXES: tuple[str, ...] = (
    "turn_execution.",
    "workflow_control.",
)
_DISALLOWED_DIRECT_TOOL_BATCH_ACTION_IDS: frozenset[str] = frozenset(
    {"workflow_invoke_subworkflow", "llm.action"}
)
_SELECTED_WORKFLOW_LAUNCH_INPUT_RESERVED_KEYS: frozenset[str] = frozenset(
    {"workflow_id", "failure_mode"}
)


def _normalise_selected_workflow_launch_inputs(raw_value: Any) -> dict[str, Any]:
    if not isinstance(raw_value, Mapping):
        return {}

    normalised: dict[str, Any] = {}
    for key, value in raw_value.items():
        if not isinstance(key, str):
            continue
        clean_key = key.strip()
        if (
            not clean_key
            or clean_key.startswith("__")
            or clean_key in _SELECTED_WORKFLOW_LAUNCH_INPUT_RESERVED_KEYS
        ):
            continue
        normalised[clean_key] = value
    return normalised


def _normalise_tool_batch_cap(
    raw_value: Any,
    *,
    environment_cap: Any,
) -> int:
    try:
        requested = int(raw_value)
    except (TypeError, ValueError):
        requested = _DEFAULT_RECOVERY_TOOL_BATCH_CAP
    requested = max(1, min(_MAX_RECOVERY_TOOL_BATCH_CAP, requested))
    try:
        env_cap = int(environment_cap)
    except (TypeError, ValueError):
        env_cap = _DEFAULT_RECOVERY_TOOL_BATCH_CAP
    env_cap = max(1, min(_MAX_RECOVERY_TOOL_BATCH_CAP, env_cap))
    return max(1, min(requested, env_cap))


def _normalise_tool_batch_calls(raw_value: Any) -> list[dict[str, Any]]:
    raw_calls = raw_value
    if isinstance(raw_value, Mapping) and isinstance(
        raw_value.get("tool_calls"), Sequence
    ):
        raw_calls = raw_value.get("tool_calls")
    if not isinstance(raw_calls, Sequence) or isinstance(
        raw_calls, (str, bytes, bytearray)
    ):
        return []

    calls: list[dict[str, Any]] = []
    for item in raw_calls:
        if not isinstance(item, Mapping):
            continue
        tool_name = _coerce_non_empty_text(
            item.get("tool")
            or item.get("action_id")
            or item.get("name")
            or item.get("method")
        )
        if not tool_name:
            continue
        payload = item.get("arguments")
        if not isinstance(payload, Mapping):
            payload = item.get("payload")
        payload_map = dict(payload) if isinstance(payload, Mapping) else {}
        calls.append(
            {
                "tool": tool_name,
                "payload": payload_map,
            }
        )
    return calls


_RECOVERY_TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION = (
    "recovery_target_contract_validation.v1"
)
_RECOVERY_TARGET_CONTRACT_EVIDENCE_MISSING = "recovery_target_contract_evidence_missing"


def _resolved_original_target_contract_evidence(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for context_key in (
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_profile",
        "turn_expected_outcome_contract",
    ):
        for contract in extract_target_contracts_from_payload(data.get(context_key)):
            if not contract.is_symbolically_resolved():
                continue
            for concept_id in contract.concept_ids:
                lowered = concept_id.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                receipts.append(
                    {
                        "concept_id": concept_id,
                        "source": "resolved_original_target_contract",
                        "context_key": context_key,
                    }
                )
    return tuple(receipts)


def _recovery_target_contract_validation_state(
    *,
    data: Mapping[str, Any],
    context_key: str,
) -> tuple[Any, dict[str, Any]]:
    raw_contracts = data.get(context_key)
    if raw_contracts is None:
        return target_contract_state_from_context(data), {
            "schema_version": _RECOVERY_TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION,
            "status": "not_provided",
            "context_key": context_key,
        }
    if not isinstance(raw_contracts, Sequence) or isinstance(
        raw_contracts, (str, bytes, bytearray)
    ):
        return {
            "target_contracts": [
                {
                    "kind": "natural_language",
                    "binding_kind": "unknown",
                    "text": "the malformed recovery target agreement",
                    "resolution_status": "unresolved",
                    "source": "recovery_target_contract_validation",
                }
            ]
        }, {
            "schema_version": _RECOVERY_TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION,
            "status": "invalid",
            "context_key": context_key,
            "error_code": "target_contract_shape_invalid",
            "errors": ["target_contract_shape_invalid"],
        }
    if not raw_contracts:
        return target_contract_state_from_context(data), {
            "schema_version": _RECOVERY_TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION,
            "status": "not_provided",
            "context_key": context_key,
        }

    proposed_state = {"target_contracts": list(raw_contracts)}
    contracts = extract_target_contracts_from_payload(proposed_state)
    errors: list[str] = []
    if len(contracts) != len(raw_contracts):
        errors.append("target_contract_shape_invalid")
    for contract in contracts:
        if not contract.is_symbolically_resolved() or not contract.concept_ids:
            errors.append("target_contract_not_resolved")
        if not contract.resolution_lineage:
            errors.append("target_contract_resolution_lineage_missing")

    evidence = [
        *successful_tool_result_concept_evidence(
            data.get("invocations")
            if isinstance(data.get("invocations"), Sequence)
            and not isinstance(data.get("invocations"), (str, bytes, bytearray))
            else ()
        ),
        *_resolved_original_target_contract_evidence(data),
    ]
    evidence_by_id: dict[str, list[dict[str, Any]]] = {}
    for receipt in evidence:
        concept_id = _coerce_non_empty_text(receipt.get("concept_id"))
        if concept_id:
            evidence_by_id.setdefault(concept_id.lower(), []).append(dict(receipt))

    matched_evidence: list[dict[str, Any]] = []
    truthful_contract_payloads: list[dict[str, Any]] = []
    for contract in contracts:
        contract_evidence: list[dict[str, Any]] = []
        for concept_id in contract.concept_ids:
            matches = evidence_by_id.get(concept_id.lower()) or []
            if not matches:
                errors.append(_RECOVERY_TARGET_CONTRACT_EVIDENCE_MISSING)
                continue
            matched_evidence.extend(matches)
            contract_evidence.extend(matches)
        truthful_payload = contract.to_state_payload()
        if contract_evidence:
            truthful_lineage: list[dict[str, Any]] = []
            seen_lineage: set[tuple[str, str, str, str, str]] = set()
            for receipt in contract_evidence:
                concept_id = _coerce_non_empty_text(receipt.get("concept_id")) or ""
                source = (
                    _coerce_non_empty_text(receipt.get("source"))
                    or "tool_invocation_result"
                )
                tool = _coerce_non_empty_text(receipt.get("tool")) or ""
                call_id = _coerce_non_empty_text(receipt.get("call_id")) or ""
                result_path = _coerce_non_empty_text(receipt.get("result_path")) or ""
                context_key = _coerce_non_empty_text(receipt.get("context_key")) or ""
                fingerprint = (
                    source,
                    concept_id.lower(),
                    tool.lower(),
                    call_id,
                    result_path or context_key,
                )
                if fingerprint in seen_lineage:
                    continue
                seen_lineage.add(fingerprint)
                lineage_item: dict[str, Any] = {
                    "source": source,
                    "concept_id": concept_id,
                }
                if tool:
                    lineage_item["tool"] = tool
                if call_id:
                    lineage_item["call_id"] = call_id
                if result_path:
                    lineage_item["result_path"] = result_path
                if context_key:
                    lineage_item["context_key"] = context_key
                truthful_lineage.append(lineage_item)
            truthful_payload["resolution_lineage"] = truthful_lineage
        truthful_contract_payloads.append(truthful_payload)

    deduped_errors = list(dict.fromkeys(errors))
    report: dict[str, Any] = {
        "schema_version": _RECOVERY_TARGET_CONTRACT_VALIDATION_SCHEMA_VERSION,
        "status": "invalid" if deduped_errors else "valid",
        "context_key": context_key,
        "proposed_contract_count": len(raw_contracts),
        "normalised_contract_count": len(contracts),
        "evidence": matched_evidence,
    }
    if deduped_errors:
        report["error_code"] = (
            _RECOVERY_TARGET_CONTRACT_EVIDENCE_MISSING
            if _RECOVERY_TARGET_CONTRACT_EVIDENCE_MISSING in deduped_errors
            else deduped_errors[0]
        )
        report["errors"] = deduped_errors
        return {
            "target_contracts": [
                {
                    "kind": "natural_language",
                    "binding_kind": "unknown",
                    "text": "the unverified recovery target agreement",
                    "resolution_status": "unresolved",
                    "source": "recovery_target_contract_validation",
                }
            ]
        }, report

    normalised_state = {
        "schema_version": "turn_target_contract_context.v1",
        "target_contracts": truthful_contract_payloads,
    }
    report["target_contracts"] = list(normalised_state["target_contracts"])
    return normalised_state, report


def _target_contract_ids(contract: TurnTargetContract) -> set[str]:
    return {
        concept_id.lower()
        for concept_id in (
            *contract.concept_ids,
            *contract.candidate_concept_ids,
        )
        if concept_id
    }


def _merge_refined_target_contracts(
    original_contracts: Any,
    *,
    target_contracts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    original = list(
        extract_target_contracts_from_payload({"target_contracts": original_contracts})
    )
    refinements = list(
        extract_target_contracts_from_payload(
            {"target_contracts": list(target_contracts)}
        )
    )
    if not refinements:
        return [contract.to_state_payload() for contract in original]
    if not original:
        return [contract.to_state_payload() for contract in refinements]

    merged: list[TurnTargetContract] = []
    applied_refinement_indexes: set[int] = set()
    for original_contract in original:
        original_ids = _target_contract_ids(original_contract)
        matching_indexes = [
            index
            for index, refinement in enumerate(refinements)
            if index not in applied_refinement_indexes
            and original_ids
            and bool(original_ids & _target_contract_ids(refinement))
        ]
        # With one original target, the represented recovery decision has only
        # one turn-level target agreement it can refine. For multi-target turns,
        # require an explicit concept/candidate overlap and preserve every
        # unrelated contract rather than silently collapsing the turn scope.
        if not matching_indexes and len(original) == 1:
            matching_indexes = list(range(len(refinements)))
        if matching_indexes:
            for index in matching_indexes:
                merged.append(refinements[index])
                applied_refinement_indexes.add(index)
            continue
        merged.append(original_contract)

    merged.extend(
        refinement
        for index, refinement in enumerate(refinements)
        if index not in applied_refinement_indexes
    )
    return [contract.to_state_payload() for contract in dedupe_target_contracts(merged)]


def _replace_target_contracts_in_mapping(
    value: Any,
    *,
    target_contracts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    updated = {str(key): item for key, item in value.items() if isinstance(key, str)}
    raw_original_contracts = updated.get("target_contracts")
    fields = updated.get("fields")
    if raw_original_contracts is None and isinstance(fields, Mapping):
        for field_name in (
            "target_contracts",
            "turn_expected_target_contracts",
            "targets",
            "turn_targets",
        ):
            if field_name in fields:
                raw_original_contracts = fields.get(field_name)
                break
    merged_target_contracts = _merge_refined_target_contracts(
        raw_original_contracts,
        target_contracts=target_contracts,
    )
    updated["target_contracts"] = merged_target_contracts
    if isinstance(fields, Mapping):
        updated_fields = {
            str(key): item for key, item in fields.items() if isinstance(key, str)
        }
        for field_name in (
            "target_contracts",
            "turn_expected_target_contracts",
            "targets",
            "turn_targets",
        ):
            if field_name in updated_fields:
                updated_fields[field_name] = list(merged_target_contracts)
        updated["fields"] = updated_fields
    return updated


def _refined_expected_target_contract_outputs(
    *,
    data: Mapping[str, Any],
    validation_report: Mapping[str, Any],
) -> dict[str, Any]:
    if validation_report.get("status") != "valid":
        return {}
    raw_contracts = validation_report.get("target_contracts")
    if not isinstance(raw_contracts, Sequence) or isinstance(
        raw_contracts, (str, bytes, bytearray)
    ):
        return {}
    target_contracts = [
        {str(key): item for key, item in contract.items() if isinstance(key, str)}
        for contract in raw_contracts
        if isinstance(contract, Mapping)
    ]
    if not target_contracts:
        return {}

    outputs: dict[str, Any] = {
        "turn_expected_target_contracts": _merge_refined_target_contracts(
            data.get("turn_expected_target_contracts"),
            target_contracts=target_contracts,
        ),
        "turn_target_contract_resolution_validation": dict(validation_report),
    }
    for context_key in (
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_profile",
        "turn_expected_outcome_contract",
    ):
        updated = _replace_target_contracts_in_mapping(
            data.get(context_key),
            target_contracts=target_contracts,
        )
        if updated is not None:
            outputs[context_key] = updated
    return outputs


def _is_disallowed_direct_tool_batch_action(tool_name: str | None) -> bool:
    cleaned = (
        str(tool_name).strip()
        if isinstance(tool_name, str) and str(tool_name).strip()
        else ""
    )
    if not cleaned:
        return True
    if cleaned in _DISALLOWED_DIRECT_TOOL_BATCH_ACTION_IDS:
        return True
    return any(
        cleaned.startswith(prefix)
        for prefix in _DISALLOWED_DIRECT_TOOL_BATCH_ACTION_PREFIXES
    )


def _normalise_recovery_concept_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.startswith("#V#"):
        return cleaned
    return None


def _extract_recovery_target_type_ids(data: Mapping[str, Any]) -> list[str]:
    containers: list[Mapping[str, Any]] = []
    for key in (
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_contract",
        "expected_outcome_contract_state",
        "expected_outcome_contract",
    ):
        value = data.get(key)
        if isinstance(value, Mapping):
            containers.append(value)

    selected_trace = data.get("selected_workflow_trace")
    if isinstance(selected_trace, Mapping):
        for key in (
            "expected_outcome_contract_state",
            "expected_outcome_contract",
        ):
            value = selected_trace.get(key)
            if isinstance(value, Mapping):
                containers.append(value)

    ordered: list[str] = []
    seen: set[str] = set()
    for container in containers:
        raw_values = container.get("target_type_ids")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes, bytearray)
        ):
            continue
        for raw_value in raw_values:
            concept_id = _normalise_recovery_concept_id(raw_value)
            if not concept_id:
                continue
            lowered = concept_id.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(concept_id)
    return ordered


def _apply_target_bound_tool_defaults(
    *,
    tool_name: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        from ...services.tool_metadata_service import get_tool_metadata

        metadata = get_tool_metadata(tool_name)
    except Exception:
        metadata = None
    default_payload = getattr(metadata, "default_payload", None)
    if not isinstance(default_payload, Mapping):
        return []

    bindings: list[dict[str, Any]] = []
    for key, value in default_payload.items():
        if not isinstance(key, str) or key in payload:
            continue
        payload[key] = value
        bindings.append(
            {
                "field": key,
                "source": "tool_metadata_default_payload",
                "value_present": True,
            }
        )
    return bindings


def _bind_recovery_payload_to_turn_contract(
    *,
    request: WorkflowActionRequest,
    tool_name: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Repair ambiguous LLM-authored payload aliases using represented targets."""

    if str(tool_name or "").strip().lower() != "get_predicate_incidence":
        return []
    if _normalise_recovery_concept_id(payload.get("concept_id")):
        return []
    if _normalise_recovery_concept_id(payload.get("instance_of")):
        return []

    target_type_ids = _extract_recovery_target_type_ids(request.data)
    if not target_type_ids:
        return []
    target_type_lookup = {target.lower(): target for target in target_type_ids}
    source_field = ""
    source_value: str | None = None
    for field_name in (
        "target_type",
        "target_type_id",
        "instance_type",
        "instance_type_id",
        "target",
    ):
        candidate = _normalise_recovery_concept_id(payload.get(field_name))
        if candidate and candidate.lower() in target_type_lookup:
            source_field = field_name
            source_value = target_type_lookup[candidate.lower()]
            break
    if not source_value:
        return []

    payload["instance_of"] = source_value
    if source_field != "instance_of":
        payload.pop(source_field, None)
    bindings = [
        {
            "field": "instance_of",
            "source": "turn_expected_outcome.target_type_ids_alias",
            "source_field": source_field,
            "value_present": True,
        }
    ]
    bindings.extend(
        _apply_target_bound_tool_defaults(tool_name=tool_name, payload=payload)
    )
    return bindings


def _prepare_recovery_tool_payload(
    *,
    request: WorkflowActionRequest,
    tool_name: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    contract_bindings = _bind_recovery_payload_to_turn_contract(
        request=request,
        tool_name=tool_name,
        payload=payload,
    )
    gateway = getattr(request.environment, "gateway", None)
    if gateway is None:
        runtime_bindings = apply_runtime_defaults_to_mcp_payload(
            payload,
            tool_name=tool_name,
            input_schema=None,
            user_namespace=getattr(request.environment, "user_namespace", None),
            default_gmail_profile=getattr(
                request.environment, "default_gmail_profile", None
            ),
        )
        return [*contract_bindings, *runtime_bindings]

    try:
        available_tool_names = tuple(gateway.describe_methods().keys())
    except Exception:
        available_tool_names = ()
    resolved_tool_name = (
        resolve_internal_mcp_tool_name(
            tool_name,
            available_tool_names=available_tool_names,
        )
        or tool_name
    )
    try:
        method_definition = gateway.get_method_definition(resolved_tool_name)
    except Exception:
        method_definition = None
    runtime_bindings = apply_runtime_defaults_to_mcp_payload(
        payload,
        tool_name=resolved_tool_name,
        input_schema=getattr(method_definition, "input_schema", None),
        user_namespace=getattr(request.environment, "user_namespace", None),
        default_gmail_profile=getattr(
            request.environment, "default_gmail_profile", None
        ),
        strip_unknown_fields=True,
    )
    return [*contract_bindings, *runtime_bindings]


def _extract_tool_batch_result_payload(outputs: Mapping[str, Any]) -> Any:
    for key in ("result", "mcp_result"):
        payload = outputs.get(key)
        if payload is not None:
            return payload
    return outputs


def _coerce_required_effect_targets(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, Sequence) or isinstance(
        raw_value, (str, bytes, bytearray)
    ):
        return []
    targets: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        text = _coerce_non_empty_text(item)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        targets.append(text)
    return targets


def _select_unresolved_recovery_target(
    required_effects: Sequence[Mapping[str, Any]] | None,
    *,
    previous_target_token: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    unresolved: list[tuple[dict[str, Any], str]] = []
    for effect in required_effects or ():
        if not isinstance(effect, Mapping):
            continue
        status = (_coerce_non_empty_text(effect.get("status")) or "").lower()
        if status not in {"not_executed", "not_satisfied"}:
            continue
        for target in _coerce_required_effect_targets(effect.get("targets")):
            unresolved.append((dict(effect), target))

    if not unresolved:
        return None, None

    previous_target = (_coerce_non_empty_text(previous_target_token) or "").lower()
    if previous_target:
        for effect, target in unresolved:
            if target.lower() != previous_target:
                return effect, target
    effect, target = unresolved[0]
    return effect, target


def _build_recovery_retry_launch_inputs(target_token: str | None) -> dict[str, Any]:
    launch_inputs: dict[str, Any] = {
        "source_uri": None,
        "arxiv_id": None,
        "file_copy_concept_id": None,
        "concept_id": None,
        "paper_concept_id": None,
    }
    target = _coerce_non_empty_text(target_token)
    if not target:
        return launch_inputs

    lowered = target.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        launch_inputs["source_uri"] = target
        try:
            from ...services.arxiv_paper_link_service import extract_arxiv_id_candidates

            candidates = extract_arxiv_id_candidates(target)
        except Exception:
            candidates = []
        if candidates:
            launch_inputs["arxiv_id"] = candidates[0]
        return launch_inputs

    try:
        from ...services.arxiv_paper_link_service import extract_arxiv_id_candidates

        arxiv_candidates = extract_arxiv_id_candidates(target)
    except Exception:
        arxiv_candidates = []
    if arxiv_candidates:
        arxiv_id = arxiv_candidates[0]
        launch_inputs["arxiv_id"] = arxiv_id
        launch_inputs["source_uri"] = f"https://arxiv.org/abs/{arxiv_id}"
        return launch_inputs

    if lowered.startswith("#v#") and "file_copy" in lowered:
        launch_inputs["file_copy_concept_id"] = target
        launch_inputs["concept_id"] = target
        return launch_inputs

    if lowered.startswith("#v#"):
        launch_inputs["concept_id"] = target
    return launch_inputs


def _normalise_turn_prompt(data: Mapping[str, Any]) -> str:
    prompt = data.get("user_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = data.get("prompt_preview")
    return prompt.strip() if isinstance(prompt, str) else ""


def _turn_discovery_sequence_values(
    data: Mapping[str, Any],
    *keys: str,
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for key in keys:
        raw_value = data.get(key)
        raw_items: Sequence[Any]
        if isinstance(raw_value, Sequence) and not isinstance(
            raw_value, (str, bytes, bytearray)
        ):
            raw_items = raw_value
        else:
            raw_items = (raw_value,)
        for item in raw_items:
            clean_item = str(item or "").strip() if item is not None else ""
            if not clean_item or clean_item in seen:
                continue
            seen.add(clean_item)
            values.append(clean_item)
    return values


def _build_turn_discovery_query_text(data: Mapping[str, Any]) -> str:
    prompt = _normalise_turn_prompt(data)
    structural_lines: list[str] = []
    required_tools = _turn_discovery_sequence_values(
        data,
        "turn_expected_required_tools",
        "required_tools",
    )
    if required_tools:
        structural_lines.append("- Required tools: " + ", ".join(required_tools))
    target_concept_ids = _turn_discovery_sequence_values(
        data,
        "turn_expected_target_concept_ids",
        "target_concept_ids",
        "target_concept_id",
    )
    if target_concept_ids:
        structural_lines.append("- Target concept IDs: " + ", ".join(target_concept_ids))
    target_type_ids = _turn_discovery_sequence_values(
        data,
        "turn_expected_target_type_ids",
        "target_type_ids",
        "target_type_id",
    )
    if target_type_ids:
        structural_lines.append("- Target type IDs: " + ", ".join(target_type_ids))
    workflow_concept_ids = _turn_discovery_sequence_values(
        data,
        "turn_expected_workflow_concept_ids",
        "workflow_concept_ids",
        "workflow_concept_id",
    )
    if workflow_concept_ids:
        structural_lines.append(
            "- Workflow concept IDs: " + ", ".join(workflow_concept_ids)
        )
    if not structural_lines:
        return prompt
    return "\n".join([prompt, "", "Turn-intent routing guidance:", *structural_lines])


def _build_turn_primary_discovery_query_text(data: Mapping[str, Any]) -> str:
    return _normalise_turn_prompt(data)


def _build_turn_workflow_discovery_memo_scope(data: Mapping[str, Any]) -> str | None:
    for key in ("turn_id", "request_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    session_id = data.get("conversation_session_id") or data.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        prompt = _normalise_turn_prompt(data)
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        return f"{session_id.strip()}:{prompt_digest}"
    if isinstance(data, dict):
        return f"request_data:{id(data)}"
    return None


def _turn_expected_outcome_contract_for_discovery_memo(
    data: Mapping[str, Any],
) -> dict[str, Any]:
    for key in (
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_contract",
        "expected_outcome_contract_state",
        "expected_outcome_contract",
    ):
        value = data.get(key)
        if isinstance(value, Mapping):
            return {str(k): v for k, v in value.items() if isinstance(k, str)}
    contract: dict[str, Any] = {}
    for key in (
        "turn_expected_outcome_summary",
        "turn_expected_grounding_requirement",
        "turn_expected_precision_policy",
        "turn_selector_guidance",
        "turn_answering_guidance",
        "turn_expected_required_tools",
        "turn_expected_target_contracts",
    ):
        value = data.get(key)
        if value is not None:
            contract[key] = value
    return contract


def _discovery_candidate_entries(discovery: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_candidates = discovery.get("matches")
    if not isinstance(raw_candidates, Sequence) or isinstance(
        raw_candidates, (str, bytes, bytearray)
    ):
        raw_candidates = discovery.get("routing_matches")
    if not isinstance(raw_candidates, Sequence) or isinstance(
        raw_candidates, (str, bytes, bytearray)
    ):
        raw_candidates = discovery.get("candidates")
    if not isinstance(raw_candidates, Sequence) or isinstance(
        raw_candidates, (str, bytes, bytearray)
    ):
        return []

    entries: list[dict[str, Any]] = []
    for item in raw_candidates:
        if not isinstance(item, Mapping):
            continue
        entry = {str(key): value for key, value in item.items() if isinstance(key, str)}
        entry.setdefault("candidate_source", "workflow_discovery")
        entries.append(entry)
    return entries


def _discover_turn_workflows_for_durable_action(
    request: WorkflowActionRequest,
) -> dict[str, Any]:
    discovery = request.data.get("workflow_discovery_result")
    if not isinstance(discovery, Mapping) or not discovery:
        discovery = request.data.get("workflow_discovery")
    if isinstance(discovery, Mapping) and discovery:
        from ...services.workflow_discovery_access_service import (
            WorkflowDiscoveryActorScopeError,
            bind_workflow_discovery_actor,
            build_workflow_discovery_actor_scope_failure,
            project_workflow_discovery_payload_for_current_actor,
        )

        try:
            with bind_workflow_discovery_actor(
                request.environment.user_namespace
            ):
                reused = project_workflow_discovery_payload_for_current_actor(
                    discovery
                )
        except WorkflowDiscoveryActorScopeError as exc:
            reused = build_workflow_discovery_actor_scope_failure(
                query=_build_turn_primary_discovery_query_text(request.data),
                requested_query=_normalise_turn_prompt(request.data),
                reason=exc.reason,
                origin="durable_action_carried_workflow_discovery_actor_scope_rejected",
            )
        prior_origin = reused.get("discovery_payload_origin")
        if isinstance(prior_origin, str) and prior_origin.strip():
            reused.setdefault("discovery_payload_origin_prior", prior_origin)
        reused["discovery_payload_origin"] = (
            "durable_action_reused_cached_workflow_discovery"
        )
        return reused

    from ...services.workflow_discovery_memo_service import (
        discover_workflows_for_turn_memoized,
    )

    raw_discovery_timeout_seconds = request.data.get(
        "workflow_discovery_timeout_seconds"
    )
    discovery_timeout_seconds = (
        raw_discovery_timeout_seconds
        if isinstance(raw_discovery_timeout_seconds, (int, float, str))
        else None
    )
    primary_query_text = _build_turn_primary_discovery_query_text(request.data)
    enriched_query_text = _build_turn_discovery_query_text(request.data)
    query_text = primary_query_text or enriched_query_text

    def _run_discovery(query: str, *, query_source: str) -> dict[str, Any]:
        if not query.strip():
            return {
                "discovery_payload_origin": (
                    "durable_action_skipped_no_query"
                ),
            }
        result = discover_workflows_for_turn_memoized(
            query,
            namespace=request.environment.user_namespace,
            turn_scope=_build_turn_workflow_discovery_memo_scope(request.data),
            requested_query=_normalise_turn_prompt(request.data),
            expected_outcome_contract=(
                _turn_expected_outcome_contract_for_discovery_memo(request.data)
            ),
            timeout_seconds=discovery_timeout_seconds,
        )
        discovery_result = dict(result) if isinstance(result, Mapping) else {}
        if discovery_result:
            discovery_result.setdefault("query", query)
            discovery_result.setdefault("requested_query", query)
            discovery_result["query_source"] = query_source
            # `discover_workflows_for_turn` already stamps origin on its own
            # returns; fall back to this branch name if missing.
            discovery_result.setdefault(
                "discovery_payload_origin",
                "durable_action_discover_workflows_for_turn",
            )
        return discovery_result

    discovery_result = _run_discovery(
        query_text,
        query_source="user_prompt" if primary_query_text else "turn_enriched_context",
    )
    if _discovery_candidate_entries(discovery_result):
        if enriched_query_text and enriched_query_text != query_text:
            discovery_result["fallback_query"] = enriched_query_text
        return discovery_result
    if enriched_query_text and enriched_query_text != query_text:
        enriched_discovery_result = _run_discovery(
            enriched_query_text,
            query_source="turn_enriched_context",
        )
        enriched_discovery_result["primary_query"] = query_text
        enriched_discovery_result["primary_match_absence_reason"] = (
            discovery_result.get("match_absence_reason")
        )
        if _discovery_candidate_entries(enriched_discovery_result):
            return enriched_discovery_result
        discovery_result = enriched_discovery_result
    return {
        "matches": [],
        "candidates": [],
        "routing_matches": [],
        "query": query_text,
        "requested_query": query_text,
        "match_count": 0,
        "candidate_count": 0,
        "search_sources": (
            discovery_result.get("search_sources")
            if isinstance(discovery_result.get("search_sources"), list)
            else ["workflow_discovery_service"]
        ),
        "match_absence_reason": discovery_result.get("match_absence_reason")
        or "durable_workflow_discovery_no_match",
        "errors": discovery_result.get("errors"),
        "discovery_payload_origin": "durable_action_no_match_fallback",
    }


def _select_workflow_from_discovery(discovery: Mapping[str, Any]) -> str | None:
    explicit = _coerce_non_empty_text(discovery.get("selected_workflow_id"))
    if explicit:
        return explicit
    for entry in _discovery_candidate_entries(discovery):
        concept_id = _coerce_non_empty_text(
            entry.get("concept_id") or entry.get("workflow_id") or entry.get("id")
        )
        if concept_id:
            return concept_id
    return None


def _build_turn_execution_route_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Resolve the workflow routing for the current turn."""
        discovery = _discover_turn_workflows_for_durable_action(request)
        # Persist on request.data so downstream early-exit paths still see
        # the populated discovery payload.
        if isinstance(request.data, dict):
            request.data["workflow_discovery_result"] = discovery
            request.data["workflow_discovery"] = discovery
        selected_workflow_id = _select_workflow_from_discovery(discovery)
        candidate_entries = _discovery_candidate_entries(discovery)
        candidate_ids = [
            concept_id
            for concept_id in (
                _coerce_non_empty_text(
                    entry.get("concept_id")
                    or entry.get("workflow_id")
                    or entry.get("id")
                )
                for entry in candidate_entries
            )
            if concept_id
        ]

        return WorkflowActionResult(
            status="success",
            outputs={
                "selected_workflow_id": selected_workflow_id,
                "workflow_id": selected_workflow_id,
                "workflow_discovery": discovery,
                "workflow_discovery_result": discovery,
                "workflow_routing": {
                    "schema_version": "durable_turn_execution_routing.v1",
                    "selected_workflow_id": selected_workflow_id,
                    "candidate_workflow_ids": candidate_ids,
                    "selector_source": "durable_discovery_fallback",
                    "selection_rationale": (
                        "first_routing_match"
                        if selected_workflow_id
                        else "no_routing_match"
                    ),
                },
            },
        )

    return _handle


def _build_turn_execution_prepare_selector_context_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Preserve any already-prepared selector context for durable execution.

        The authoritative preparation logic lives in the orchestrator override
        action. This registry-level fallback keeps the action ID executable on
        pure workflow-engine paths without inventing a second policy surface.
        """

        discovery = _discover_turn_workflows_for_durable_action(request)
        candidate_entries = _discovery_candidate_entries(discovery)
        candidate_ids = [
            concept_id
            for concept_id in (
                _coerce_non_empty_text(
                    entry.get("concept_id")
                    or entry.get("workflow_id")
                    or entry.get("id")
                )
                for entry in candidate_entries
            )
            if concept_id
        ]

        passthrough_keys = (
            "workflow_discovery_result",
            "workflow_discovery",
            "turn_expected_outcome_profile",
            "turn_expected_outcome_contract",
            "turn_expected_outcome_contract_state",
            "turn_expected_outcome_summary",
            "turn_expected_grounding_requirement",
            "turn_expected_precision_policy",
            "turn_selector_guidance",
            "turn_answering_guidance",
            "turn_expected_outcome_reasoning",
            "turn_expected_target_contracts",
            "turn_context_handoff_decision",
            "turn_context_handoff_mode",
            "turn_context_handoff_summary",
            "turn_context_handoff_messages",
            "turn_context_handoff_lineage",
            "turn_context_handoff_risks",
            "turn_context_handoff_omitted_context_reasons",
            "turn_context_handoff_routing_evidence_scope",
            "turn_context_handoff_expected_outcome_scope",
            "turn_context_handoff_answer_scope",
            "selector_prompt_available",
            "selector_prompt_id",
            "selector_prompt_text",
            "selector_call_prompt_text",
            "selector_requested_prompt_ids",
            "selector_prompt_provenance",
            "selector_prompt_failure_reason",
            "selector_prompt_failure_detail",
            "selector_authoritative_candidate_entries",
            "selector_authoritative_candidate_ids",
            "selector_authoritative_candidate_source",
            "selector_candidate_entries",
            "selector_candidate_ids",
            "selector_excluded_candidate_entries",
            "selector_excluded_candidate_ids",
            "selector_discovered_workflow_ids",
            "selector_context_messages",
            "selector_context_lineage",
            "selector_candidate_count",
            "selector_excluded_candidate_count",
            "selector_policy_recommendation",
            "selector_continuation_routing_context_text",
        )
        outputs = {
            key: request.data.get(key)
            for key in passthrough_keys
            if key in request.data
        }
        if "workflow_discovery_result" not in outputs and isinstance(
            request.data.get("workflow_discovery"),
            Mapping,
        ):
            outputs["workflow_discovery_result"] = dict(
                request.data["workflow_discovery"]
            )
        if "workflow_discovery" not in outputs and isinstance(
            request.data.get("workflow_discovery_result"),
            Mapping,
        ):
            outputs["workflow_discovery"] = dict(
                request.data["workflow_discovery_result"]
            )
        outputs["workflow_discovery_result"] = discovery
        outputs["workflow_discovery"] = discovery
        # Persist on request.data so downstream early-exit paths still see
        # the populated discovery payload.
        if isinstance(request.data, dict):
            request.data["workflow_discovery_result"] = discovery
            request.data["workflow_discovery"] = discovery
        outputs.setdefault("selector_prompt_available", False)
        outputs.setdefault("selector_prompt_id", "durable_selector_prompt_unavailable")
        outputs.setdefault(
            "selector_prompt_text",
            "Durable selector prompt unavailable; routing uses workflow discovery directly.",
        )
        outputs.setdefault(
            "selector_call_prompt_text",
            build_selector_call_prompt(
                _coerce_non_empty_text(
                    request.data.get("user_prompt") or request.data.get("prompt")
                )
            ),
        )
        outputs.setdefault("selector_requested_prompt_ids", [])
        outputs.setdefault("selector_prompt_provenance", {})
        outputs.setdefault(
            "selector_prompt_failure_reason",
            "durable_selector_prompt_unavailable",
        )
        outputs.setdefault("selector_prompt_failure_detail", None)
        outputs.setdefault("selector_authoritative_candidate_entries", candidate_entries)
        outputs.setdefault("selector_authoritative_candidate_ids", candidate_ids)
        outputs.setdefault(
            "selector_authoritative_candidate_source",
            "durable_workflow_discovery_pre_policy",
        )
        outputs.setdefault("selector_candidate_entries", candidate_entries)
        outputs.setdefault("selector_candidate_ids", candidate_ids)
        outputs.setdefault("selector_excluded_candidate_entries", [])
        outputs.setdefault("selector_excluded_candidate_ids", [])
        outputs.setdefault("selector_discovered_workflow_ids", candidate_ids)
        outputs.setdefault("selector_context_messages", [])
        outputs.setdefault("selector_context_lineage", {})
        outputs.setdefault("selector_candidate_count", len(candidate_entries))
        outputs.setdefault("selector_excluded_candidate_count", 0)
        outputs.setdefault("selector_policy_recommendation", {})
        outputs.setdefault("selector_continuation_routing_context_text", "")
        return WorkflowActionResult(outputs=outputs)

    return _handle


def _build_turn_execution_execute_selected_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        """Execute the selected capability workflow with supervision."""
        from .registry_factory import get_shared_durable_action_registry

        selected_workflow_id_raw = request.data.get("selected_workflow_id")
        selected_workflow_id = (
            str(selected_workflow_id_raw).strip()
            if isinstance(selected_workflow_id_raw, str)
            and str(selected_workflow_id_raw).strip()
            else None
        )
        if not selected_workflow_id:
            outputs = build_turn_execution_selected_workflow_outputs(
                selected_workflow_id=None,
                child_completed=False,
                final_state="no_selected_workflow",
                failure_detail="turn_execution_no_workflow_selected",
                child_outputs={},
                child_result_snapshot={},
                selected_workflow_trace=request.data.get("selected_workflow_trace"),
                turn_expected_outcome_contract=(
                    request.data.get("turn_expected_outcome_contract_state")
                    or request.data.get("turn_expected_outcome_contract")
                ),
                workflow_routing=request.data.get("workflow_routing"),
                workflow_discovery=(
                    request.data.get("workflow_discovery_result")
                    if isinstance(
                        request.data.get("workflow_discovery_result"), Mapping
                    )
                    else request.data.get("workflow_discovery")
                ),
                parent_aux_llm_calls=(
                    request.data.get("aux_llm_calls")
                    if isinstance(request.data.get("aux_llm_calls"), list)
                    else None
                ),
                parent_llm_calls=(
                    request.data.get("llm_calls")
                    if isinstance(request.data.get("llm_calls"), list)
                    else None
                ),
            )
            return WorkflowActionResult(outputs=outputs)

        from .subworkflow_actions import (
            get_subworkflow_invocation_budget_state,
            is_subworkflow_resource_exhaustion_error,
            summarise_subworkflow_invocation_ledger,
        )

        budget_state = get_subworkflow_invocation_budget_state(request.data)
        if budget_state.get("exhausted"):
            # The per-turn subworkflow budget is monotonic, so launching the
            # selected workflow again cannot succeed; fail fast with the
            # ledger naming what consumed the budget (JVNAUTOSCI-2502).
            outputs = build_turn_execution_selected_workflow_outputs(
                selected_workflow_id=selected_workflow_id,
                child_completed=False,
                final_state="subworkflow_budget_exhausted",
                failure_detail=(
                    "subworkflow_invocation_budget_exceeded:pre_execution_guard:"
                    f"max_invocations={budget_state.get('invocation_limit')}"
                ),
                child_outputs={},
                child_result_snapshot={},
                selected_workflow_trace=request.data.get("selected_workflow_trace"),
                turn_expected_outcome_contract=(
                    request.data.get("turn_expected_outcome_contract_state")
                    or request.data.get("turn_expected_outcome_contract")
                ),
                workflow_routing=request.data.get("workflow_routing"),
                workflow_discovery=(
                    request.data.get("workflow_discovery_result")
                    if isinstance(
                        request.data.get("workflow_discovery_result"), Mapping
                    )
                    else request.data.get("workflow_discovery")
                ),
                parent_aux_llm_calls=(
                    request.data.get("aux_llm_calls")
                    if isinstance(request.data.get("aux_llm_calls"), list)
                    else None
                ),
                parent_llm_calls=(
                    request.data.get("llm_calls")
                    if isinstance(request.data.get("llm_calls"), list)
                    else None
                ),
            )
            outputs["subworkflow_invocation_budget_state"] = budget_state
            outputs["subworkflow_invocation_ledger_summary"] = (
                summarise_subworkflow_invocation_ledger(request.data)
            )
            outputs["turn_recovery_retry_viable"] = False
            outputs["turn_recovery_retry_block_reason"] = (
                "subworkflow_budget_exhausted"
            )
            return WorkflowActionResult(outputs=outputs)

        request_data = {
            str(key): value
            for key, value in request.data.items()
            if isinstance(key, str)
        }
        selected_workflow_launch_inputs = _normalise_selected_workflow_launch_inputs(
            request_data.get("workflow_launch_inputs")
        )
        if selected_workflow_launch_inputs:
            request_data["selected_workflow_launch_inputs"] = dict(
                selected_workflow_launch_inputs
            )
            for key, value in selected_workflow_launch_inputs.items():
                request_data.setdefault(key, value)
        continuation_context = request_data.get("continuation_context")
        projected_continuation_launch_inputs: dict[str, Any] = {}
        if isinstance(continuation_context, Mapping) and bool(
            continuation_context.get("applied")
        ):
            try:
                from ...services.workflow_continuation_service import (
                    project_launch_inputs_from_continuation_context,
                )

                projected_continuation_launch_inputs = (
                    project_launch_inputs_from_continuation_context(
                        continuation_context,
                        selected_workflow_id=selected_workflow_id,
                    )
                )
            except Exception:
                projected_continuation_launch_inputs = {}
        if projected_continuation_launch_inputs:
            if isinstance(request.data, dict):
                request.data.setdefault(
                    "workflow_continuation_launch_inputs",
                    dict(projected_continuation_launch_inputs),
                )
                for key, value in projected_continuation_launch_inputs.items():
                    request.data.setdefault(key, value)
            request_data["workflow_continuation_launch_inputs"] = dict(
                projected_continuation_launch_inputs
            )
            for key, value in projected_continuation_launch_inputs.items():
                request_data.setdefault(key, value)

        subworkflow_inputs = {
            "workflow_id": selected_workflow_id,
            "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
            **request_data,
        }
        subworkflow_inputs["__workflow_ambient_input_keys"] = sorted(
            key for key in request_data.keys() if isinstance(key, str)
        )
        subworkflow_result = get_shared_durable_action_registry().execute(
            "workflow_invoke_subworkflow",
            inputs=subworkflow_inputs,
            context=request.data,
            env=request.environment,
            trace=request.trace,
            workflow_id=request.workflow_id,
            workflow_state_id=request.workflow_state_id,
        )
        child_payload: dict[str, Any] = {}
        child_completed = False
        child_final_state: str | None = None
        child_error: str | None = None

        if subworkflow_result.ok:
            child_result = subworkflow_result.outputs.get("result")
            child_payload = (
                dict(child_result) if isinstance(child_result, Mapping) else {}
            )
            invocation = subworkflow_result.outputs.get("subworkflow_invocation")
            invocation_payload = (
                dict(invocation) if isinstance(invocation, Mapping) else {}
            )
            child_completed = not bool(
                subworkflow_result.outputs.get("child_workflow_failed")
            )
            child_final_state = (
                str(invocation_payload.get("child_final_state")).strip()
                if isinstance(invocation_payload.get("child_final_state"), str)
                and str(invocation_payload.get("child_final_state")).strip()
                else None
            )
            child_error = (
                str(subworkflow_result.outputs.get("subworkflow_error")).strip()
                if isinstance(subworkflow_result.outputs.get("subworkflow_error"), str)
                and str(subworkflow_result.outputs.get("subworkflow_error")).strip()
                else (
                    str(invocation_payload.get("child_error")).strip()
                    if isinstance(invocation_payload.get("child_error"), str)
                    and str(invocation_payload.get("child_error")).strip()
                    else None
                )
            )
        else:
            child_error = (
                str(subworkflow_result.error).strip()
                if isinstance(subworkflow_result.error, str)
                and str(subworkflow_result.error).strip()
                else "selected_workflow_execution_failed"
            )
            child_final_state = "subworkflow_invocation_failed"

        retry_block_reason: str | None = None
        if child_error and is_subworkflow_resource_exhaustion_error(child_error):
            retry_block_reason = "subworkflow_resource_exhausted"
        else:
            post_budget_state = get_subworkflow_invocation_budget_state(
                request.data
            )
            if not child_completed and post_budget_state.get("exhausted"):
                retry_block_reason = "subworkflow_budget_exhausted"

        outputs = build_turn_execution_selected_workflow_outputs(
            selected_workflow_id=selected_workflow_id,
            child_completed=child_completed,
            final_state=child_final_state,
            failure_detail=child_error,
            child_outputs=child_payload,
            child_result_snapshot=child_payload,
            selected_workflow_trace=request.data.get("selected_workflow_trace"),
            turn_expected_outcome_contract=(
                request.data.get("turn_expected_outcome_contract_state")
                or request.data.get("turn_expected_outcome_contract")
            ),
            workflow_routing=request.data.get("workflow_routing"),
            workflow_discovery=(
                request.data.get("workflow_discovery_result")
                if isinstance(request.data.get("workflow_discovery_result"), Mapping)
                else request.data.get("workflow_discovery")
            ),
            parent_aux_llm_calls=(
                request.data.get("aux_llm_calls")
                if isinstance(request.data.get("aux_llm_calls"), list)
                else None
            ),
            parent_llm_calls=(
                request.data.get("llm_calls")
                if isinstance(request.data.get("llm_calls"), list)
                else None
            ),
        )
        outputs["subworkflow_invocation_budget_state"] = (
            get_subworkflow_invocation_budget_state(request.data)
        )
        if retry_block_reason:
            outputs["turn_recovery_retry_viable"] = False
            outputs["turn_recovery_retry_block_reason"] = retry_block_reason
            outputs["subworkflow_invocation_ledger_summary"] = (
                summarise_subworkflow_invocation_ledger(request.data)
            )

        raw_existing_invocations = request.data.get("invocations")
        existing_invocations = (
            list(raw_existing_invocations)
            if isinstance(raw_existing_invocations, list)
            else []
        )
        raw_new_invocations = outputs.get("invocations")
        new_invocations = (
            list(raw_new_invocations) if isinstance(raw_new_invocations, list) else []
        )
        if existing_invocations:
            outputs["invocations"] = [*existing_invocations, *new_invocations]

        raw_existing_tool_messages = request.data.get("tool_messages")
        existing_tool_messages = (
            list(raw_existing_tool_messages)
            if isinstance(raw_existing_tool_messages, list)
            else []
        )
        raw_new_tool_messages = outputs.get("tool_messages")
        new_tool_messages = (
            list(raw_new_tool_messages)
            if isinstance(raw_new_tool_messages, list)
            else []
        )
        if existing_tool_messages:
            outputs["tool_messages"] = [*existing_tool_messages, *new_tool_messages]
        return WorkflowActionResult(outputs=outputs)

    return _handle


def _build_turn_execution_execute_tool_batch_handler(
    registry: ActionRegistry,
) -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        raw_tool_calls = request.inputs.get("tool_calls")
        if raw_tool_calls is None:
            tool_calls_context_key = (
                _coerce_non_empty_text(request.inputs.get("tool_calls_context_key"))
                or "turn_next_action_tool_calls"
            )
            raw_tool_calls = request.data.get(tool_calls_context_key)
        requested_tool_calls = _normalise_tool_batch_calls(raw_tool_calls)
        max_calls = _normalise_tool_batch_cap(
            request.inputs.get("tool_batch_cap"),
            environment_cap=request.environment.max_tool_invocations,
        )
        target_contracts_context_key = (
            _coerce_non_empty_text(request.inputs.get("target_contracts_context_key"))
            or "turn_recovery_target_contracts"
        )
        target_contract_state, recovery_target_contract_validation = (
            _recovery_target_contract_validation_state(
                data=request.data,
                context_key=target_contracts_context_key,
            )
        )
        refined_target_contract_outputs = _refined_expected_target_contract_outputs(
            data=request.data,
            validation_report=recovery_target_contract_validation,
        )
        declared_target_contract_outputs: dict[str, Any] = {
            "turn_target_contract_resolution_validation": dict(
                recovery_target_contract_validation
            ),
        }
        for context_key in (
            "turn_expected_target_contracts",
            "turn_expected_outcome_contract_state",
            "turn_expected_outcome_profile",
            "turn_expected_outcome_contract",
        ):
            if (
                context_key in request.data
                and request.data.get(context_key) is not None
            ):
                declared_target_contract_outputs[context_key] = request.data[
                    context_key
                ]
        declared_target_contract_outputs.update(refined_target_contract_outputs)
        prior_invocations = (
            request.data.get("invocations")
            if isinstance(request.data.get("invocations"), Sequence)
            and not isinstance(request.data.get("invocations"), (str, bytes, bytearray))
            else ()
        )

        execution_records: list[dict[str, Any]] = []
        for tool_call in requested_tool_calls[:max_calls]:
            tool_name = _coerce_non_empty_text(tool_call.get("tool"))
            raw_payload = tool_call.get("payload")
            payload = (
                {
                    str(key): value
                    for key, value in raw_payload.items()
                    if isinstance(key, str)
                }
                if isinstance(raw_payload, Mapping)
                else {}
            )
            if _is_disallowed_direct_tool_batch_action(tool_name):
                execution_records.append(
                    {
                        "tool": tool_name or "unknown",
                        "payload": _bounded_snapshot(payload),
                        "status": "failed",
                        "error": "recovery_tool_batch_disallowed_action",
                    }
                )
                continue
            payload_bindings = _prepare_recovery_tool_payload(
                request=request,
                tool_name=tool_name or "",
                payload=payload,
            )
            target_validation = validate_tool_target_contract(
                tool_name=tool_name or "",
                payload=payload,
                target_contract_state=target_contract_state,
                prior_tool_invocations=(
                    prior_invocations
                    if recovery_target_contract_validation.get("status") != "invalid"
                    else ()
                ),
            )
            if not target_validation.ok:
                record: dict[str, Any] = {
                    "tool": tool_name or "unknown",
                    "payload": _bounded_snapshot(payload),
                    "status": "failed",
                    "error": (
                        target_validation.first_error_code()
                        or "target_contract_validation_failed"
                    ),
                    "tool_call_validation_diagnostics": list(
                        target_validation.diagnostics
                    ),
                }
                if payload_bindings:
                    record["payload_bindings"] = _bounded_snapshot(payload_bindings)
                if recovery_target_contract_validation.get("status") == "invalid":
                    record["recovery_target_contract_validation"] = _bounded_snapshot(
                        recovery_target_contract_validation,
                        max_depth=5,
                    )
                execution_records.append(record)
                continue

            tool_context = {
                str(key): value
                for key, value in request.data.items()
                if isinstance(key, str)
            }
            result = registry.execute(
                tool_name or "",
                inputs=payload,
                context=tool_context,
                env=request.environment,
                trace=request.trace,
                workflow_id=request.workflow_id,
                workflow_state_id=request.workflow_state_id,
                workflow_state_metadata=request.workflow_state_metadata,
            )
            result_payload = _extract_tool_batch_result_payload(result.outputs)
            result_summary = _summarise_tool_batch_result_payload(result_payload)
            error_text = (
                _coerce_non_empty_text(result.error)
                or (
                    _coerce_non_empty_text(result_payload.get("error"))
                    if isinstance(result_payload, Mapping)
                    else None
                )
                or (
                    _coerce_non_empty_text(result_payload.get("error_code"))
                    if isinstance(result_payload, Mapping)
                    else None
                )
            )

            record: dict[str, Any] = {
                "tool": tool_name or "unknown",
                "payload": _bounded_snapshot(payload),
                "status": "ok" if result.ok else "failed",
            }
            if target_validation.resolution_evidence:
                record["target_contract_resolution_evidence"] = [
                    dict(item) for item in target_validation.resolution_evidence
                ]
            if payload_bindings:
                record["payload_bindings"] = _bounded_snapshot(payload_bindings)
            if result.duration_ms is not None:
                record["duration_ms"] = float(result.duration_ms)
            if result_summary:
                record["result_summary"] = result_summary
            if error_text:
                record["error"] = error_text
            if result_payload is not None:
                record["result_preview"] = _bounded_snapshot(
                    result_payload, max_depth=5
                )
            execution_records.append(record)

        outputs = build_turn_recovery_tool_batch_outputs(
            requested_tool_calls=requested_tool_calls[:max_calls],
            invocation_records=execution_records,
            existing_invocations=(
                request.data.get("invocations")
                if isinstance(request.data.get("invocations"), list)
                else []
            ),
            existing_tool_messages=(
                request.data.get("tool_messages")
                if isinstance(request.data.get("tool_messages"), list)
                else []
            ),
            selected_workflow_trace=(
                request.data.get("selected_workflow_trace")
                if isinstance(request.data.get("selected_workflow_trace"), Mapping)
                else None
            ),
            selected_workflow_id=request.data.get("selected_workflow_id"),
            turn_expected_outcome_contract=(
                refined_target_contract_outputs.get(
                    "turn_expected_outcome_contract_state"
                )
                or refined_target_contract_outputs.get("turn_expected_outcome_contract")
                or request.data.get("turn_expected_outcome_contract_state")
                or request.data.get("turn_expected_outcome_contract")
            ),
            reasoning=_coerce_non_empty_text(
                request.data.get("turn_next_action_reasoning")
            ),
            omitted_call_count=max(0, len(requested_tool_calls) - max_calls),
        )
        outputs.update(
            {
                "turn_recovery_attempted": True,
                "turn_recovery_last_decision": (
                    _coerce_non_empty_text(request.data.get("turn_next_action_type"))
                    or "execute_tool_batch"
                ),
                "turn_recovery_last_reasoning": _coerce_non_empty_text(
                    request.data.get("turn_next_action_reasoning")
                )
                or "",
                "turn_recovery_last_target_workflow_id": _coerce_non_empty_text(
                    request.data.get("turn_next_action_target_workflow_id")
                )
                or "",
                "turn_recovery_target_contract_validation": dict(
                    recovery_target_contract_validation
                ),
            }
        )
        outputs.update(declared_target_contract_outputs)
        for context_key, empty_value in (
            ("turn_expected_target_contracts", []),
            ("turn_expected_outcome_contract_state", {}),
            ("turn_expected_outcome_profile", {}),
            ("turn_expected_outcome_contract", {}),
        ):
            outputs.setdefault(context_key, empty_value)
        for output_key in (
            "completion_report",
            "turn_recovery_tool_batch_execution",
        ):
            output_payload = outputs.get(output_key)
            if isinstance(output_payload, dict):
                output_payload["target_contract_validation"] = dict(
                    recovery_target_contract_validation
                )
        selected_trace = outputs.get("selected_workflow_trace")
        if isinstance(selected_trace, dict):
            selected_trace["recovery_target_contract_validation"] = dict(
                recovery_target_contract_validation
            )
            if refined_target_contract_outputs:
                refined_state = refined_target_contract_outputs.get(
                    "turn_expected_outcome_contract_state"
                )
                refined_contract = refined_target_contract_outputs.get(
                    "turn_expected_outcome_contract"
                )
                if isinstance(refined_state, Mapping):
                    selected_trace["expected_outcome_contract_state"] = dict(
                        refined_state
                    )
                if isinstance(refined_contract, Mapping):
                    selected_trace["expected_outcome_contract"] = dict(refined_contract)
        return WorkflowActionResult(outputs=outputs)

    return _handle


def _build_turn_execution_prepare_recovery_retry_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        if request.data.get("turn_recovery_retry_viable") is False:
            # Structural execution contract: the prior failure was resource
            # exhaustion, so a retry shares the exhausted budget and cannot
            # succeed. Fail the retry preparation so the represented
            # transitions terminate the turn promptly (JVNAUTOSCI-2502).
            block_reason = (
                _coerce_non_empty_text(
                    request.data.get("turn_recovery_retry_block_reason")
                )
                or "retry_not_viable"
            )
            return WorkflowActionResult(
                status="failed",
                error=f"turn_recovery_retry_not_viable:{block_reason}",
                outputs={
                    "turn_recovery_retry_blocked": True,
                    "turn_recovery_retry_block_reason": block_reason,
                    "subworkflow_invocation_ledger_summary": request.data.get(
                        "subworkflow_invocation_ledger_summary"
                    ),
                },
            )

        target_workflow_id = _coerce_non_empty_text(
            request.data.get("turn_next_action_target_workflow_id")
            or request.data.get("selected_workflow_id")
        )
        if not target_workflow_id:
            return WorkflowActionResult(
                status="failed",
                error="turn_recovery_retry_target_workflow_missing",
            )

        required_effects = (
            request.data.get("required_effects")
            if isinstance(request.data.get("required_effects"), list)
            else []
        )
        selected_effect, target_token = _select_unresolved_recovery_target(
            required_effects,
            previous_target_token=_coerce_non_empty_text(
                request.data.get("turn_recovery_last_target_token")
            ),
        )
        launch_inputs = _build_recovery_retry_launch_inputs(target_token)
        selection_payload = {
            "selected_target_token": target_token,
            "selected_effect_id": (
                _coerce_non_empty_text(selected_effect.get("effect_id"))
                if isinstance(selected_effect, Mapping)
                else None
            ),
            "selected_effect_type": (
                _coerce_non_empty_text(selected_effect.get("effect_type"))
                if isinstance(selected_effect, Mapping)
                else None
            ),
            "launch_inputs": dict(launch_inputs),
            "selection_reason": (
                "selected_next_unresolved_target"
                if target_token
                else "no_unresolved_target_found"
            ),
        }
        previous_response_text = (
            _coerce_non_empty_text(
                request.data.get("completion_gate_preserved_response")
            )
            or _coerce_non_empty_text(
                request.data.get("turn_recovery_previous_response_text")
            )
            or _coerce_non_empty_text(
                request.data.get("turn_recovery_previous_final_response")
            )
            or _coerce_non_empty_text(
                request.data.get("turn_recovery_previous_selected_workflow_response")
            )
            or _coerce_non_empty_text(request.data.get("turn_recovery_response_text"))
            or _coerce_non_empty_text(
                request.data.get("turn_next_action_response_text")
            )
            or _coerce_non_empty_text(
                request.data.get("completion_report_response_text")
            )
            or _coerce_non_empty_text(request.data.get("response_text"))
            or _coerce_non_empty_text(request.data.get("final_response"))
            or _coerce_non_empty_text(
                request.data.get("selected_workflow_user_response")
            )
            or _coerce_non_empty_text(request.data.get("current_response"))
        )
        previous_final_response = _coerce_non_empty_text(
            request.data.get("final_response")
        )
        previous_selected_workflow_response = _coerce_non_empty_text(
            request.data.get("selected_workflow_user_response")
        )

        try:
            recovery_attempt_count = (
                int(request.data.get("turn_recovery_attempt_count") or 0) + 1
            )
        except (TypeError, ValueError):
            recovery_attempt_count = 1

        outputs: dict[str, Any] = {
            "selected_workflow_id": target_workflow_id,
            "workflow_continuation_launch_inputs": dict(launch_inputs),
            "turn_recovery_attempted": True,
            "turn_recovery_attempt_count": recovery_attempt_count,
            "turn_recovery_last_decision": (
                _coerce_non_empty_text(request.data.get("turn_next_action_type"))
                or "retry_execution"
            ),
            "turn_recovery_last_reasoning": _coerce_non_empty_text(
                request.data.get("turn_next_action_reasoning")
            )
            or "",
            "turn_recovery_last_target_workflow_id": target_workflow_id,
            "turn_recovery_last_target_token": target_token or "",
            "turn_recovery_last_effect_id": _coerce_non_empty_text(
                selection_payload.get("selected_effect_id")
            )
            or "",
            "turn_recovery_retry_selection": selection_payload,
            "turn_recovery_previous_response_text": previous_response_text or "",
            "turn_recovery_previous_final_response": previous_final_response or "",
            "turn_recovery_previous_selected_workflow_response": (
                previous_selected_workflow_response or ""
            ),
            "response_text": "",
            "final_response": "",
            "current_response": "",
            "selected_workflow_user_response": "",
        }
        outputs.update(launch_inputs)
        return WorkflowActionResult(outputs=outputs)

    return _handle


def _build_turn_execution_critic_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        return run_turn_execution_critic(
            request,
            annotation_component="durable_turn_execution_actions",
            annotation_function="_build_turn_execution_critic_handler",
        )

    return _handle


def _build_turn_execution_completion_gate_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        return run_turn_execution_completion_gate(
            request,
            annotation_component="durable_turn_execution_actions",
            annotation_function="_build_turn_execution_completion_gate_handler",
            introspection_auto_apply_env="VON_WORKFLOW_INTROSPECTION_AUTO_APPLY",
        )

    return _handle


def register_turn_execution_actions(registry: ActionRegistry) -> None:
    """Register all master-turn control plane actions."""

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_ROUTE_ACTION_ID,
            handler=_build_turn_execution_route_handler(),
            description="Resolve workflow routing for the current turn.",
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_PREPARE_SELECTOR_CONTEXT_ACTION_ID,
            handler=_build_turn_execution_prepare_selector_context_handler(),
            description=(
                "Preserve prepared selector prompt/context surfaces for the "
                "authoritative selector decision state."
            ),
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_EXECUTE_SELECTED_ACTION_ID,
            handler=_build_turn_execution_execute_selected_handler(),
            description="Execute the selected capability workflow with supervision.",
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_EXECUTE_TOOL_BATCH_ACTION_ID,
            handler=_build_turn_execution_execute_tool_batch_handler(registry),
            description=(
                "Execute a bounded direct recovery tool batch and return the turn "
                "to narration/verification surfaces."
            ),
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_PREPARE_RECOVERY_RETRY_ACTION_ID,
            handler=_build_turn_execution_prepare_recovery_retry_handler(),
            description=(
                "Prepare the next bounded recovery retry by selecting the next "
                "unresolved target and projecting singular launch inputs."
            ),
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_CRITIC_ACTION_ID,
            handler=_build_turn_execution_critic_handler(),
            description="Evaluate required effects and postcondition checks for the turn.",
        )
    )

    registry.register_if_absent(
        ActionSpec(
            action_id=TURN_EXECUTION_COMPLETION_GATE_ACTION_ID,
            handler=_build_turn_execution_completion_gate_handler(),
            description="Apply completion gate and prevent false completion claims.",
        )
    )
