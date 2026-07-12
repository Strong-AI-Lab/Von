"""Shared turn expected-outcome contract support surfaces.

This module provides one canonical serialisable boundary payload for the
turn-level expected-outcome contract plus a typed Python helper used by the
orchestrator, durable turn-runtime support, and turn-execution reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from src.backend.services.required_tool_identity_service import (
    canonical_required_tool_key,
)

from .turn_target_contract import (
    TARGET_BINDING_ENTITY,
    TARGET_BINDING_TYPE,
    TURN_TARGET_CONTRACTS_CONTEXT_KEY,
    TurnTargetContract,
    dedupe_target_contracts,
    extract_target_contracts_from_payload,
)

TURN_EXPECTED_OUTCOME_CONTRACT_SCHEMA_VERSION = "turn_expected_outcome_contract.v1"
TURN_EXPECTED_OUTCOME_CONTRACT_FIELDS: tuple[str, ...] = (
    "summary",
    "grounding_requirement",
    "precision_policy",
    "selector_guidance",
    "answering_guidance",
    "reasoning",
)
TURN_EXPECTED_OUTCOME_CONTEXT_FIELD_MAPPING: dict[str, str] = {
    "summary": "turn_expected_outcome_summary",
    "grounding_requirement": "turn_expected_grounding_requirement",
    "precision_policy": "turn_expected_precision_policy",
    "selector_guidance": "turn_selector_guidance",
    "answering_guidance": "turn_answering_guidance",
    "reasoning": "turn_expected_outcome_reasoning",
}
TURN_EXPECTED_OUTCOME_REQUIRED_TOOLS_CONTEXT_KEY = "turn_expected_required_tools"
TURN_EXPECTED_OUTCOME_CONDITIONAL_REQUIRED_TOOLS_CONTEXT_KEY = (
    "turn_expected_conditional_required_tools"
)
TURN_EXPECTED_OUTCOME_TARGET_CONCEPT_IDS_CONTEXT_KEY = (
    "turn_expected_target_concept_ids"
)
TURN_EXPECTED_OUTCOME_TARGET_TYPE_IDS_CONTEXT_KEY = "turn_expected_target_type_ids"
TURN_EXPECTED_OUTCOME_WORKFLOW_CONCEPT_IDS_CONTEXT_KEY = (
    "turn_expected_workflow_concept_ids"
)
TURN_EXPECTED_OUTCOME_TARGET_CONTRACTS_CONTEXT_KEY = TURN_TARGET_CONTRACTS_CONTEXT_KEY
TURN_EXPECTED_OUTCOME_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": ("expected_outcome_summary",),
    "grounding_requirement": (
        "grounding",
        "grounding_evidence",
        "grounding_relation",
        "grounding_standard",
        "evidence_standard",
        "evidence_requirement",
        "required_evidence",
        "required_grounding",
    ),
}
TURN_EXPECTED_OUTCOME_REQUIRED_TOOL_FIELDS: tuple[str, ...] = (
    "required_tools",
    TURN_EXPECTED_OUTCOME_REQUIRED_TOOLS_CONTEXT_KEY,
)
TURN_EXPECTED_OUTCOME_CONDITIONAL_REQUIRED_TOOL_FIELDS: tuple[str, ...] = (
    "conditional_required_tools",
    TURN_EXPECTED_OUTCOME_CONDITIONAL_REQUIRED_TOOLS_CONTEXT_KEY,
    "required_tools_if_target_resolved",
    "required_tools_when_target_found",
)
TURN_EXPECTED_OUTCOME_TARGET_CONCEPT_FIELDS: tuple[str, ...] = (
    "target_concept_id",
    "target_concept_ids",
    "target_concepts",
    TURN_EXPECTED_OUTCOME_TARGET_CONCEPT_IDS_CONTEXT_KEY,
    "focal_concept_id",
    "focal_concept_ids",
    "focal_concepts",
    "required_target_concept_ids",
)
TURN_EXPECTED_OUTCOME_TARGET_TYPE_FIELDS: tuple[str, ...] = (
    "target_type_id",
    "target_type_ids",
    "target_types",
    "requested_type_id",
    "requested_type_ids",
    "requested_extent_type_id",
    "requested_extent_type_ids",
    "required_target_type_ids",
    TURN_EXPECTED_OUTCOME_TARGET_TYPE_IDS_CONTEXT_KEY,
)
TURN_EXPECTED_OUTCOME_WORKFLOW_CONCEPT_FIELDS: tuple[str, ...] = (
    "workflow_concept_id",
    "workflow_concept_ids",
    "target_workflow_id",
    "target_workflow_ids",
    "preferred_workflow_id",
    "preferred_workflow_ids",
    "workflow_execute_target",
    "workflow_execute_targets",
    TURN_EXPECTED_OUTCOME_WORKFLOW_CONCEPT_IDS_CONTEXT_KEY,
)
VONTOLOGY_CONCEPT_ID_PATTERN = re.compile(r"#V#[A-Za-z0-9_][A-Za-z0-9_.:/-]*")


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _copy_string_key_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _nested_expected_outcome_payload_candidates(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    candidates: list[Mapping[str, Any]] = []
    seen_ids: set[int] = {id(payload)}

    def _append(value: Any) -> None:
        nested = _copy_string_key_mapping(value)
        if nested is None:
            return
        nested_id = id(value)
        if nested_id in seen_ids:
            return
        seen_ids.add(nested_id)
        candidates.append(nested)

    for key in (
        "validated_json",
        "turn_expected_outcome_contract_state",
        "turn_expected_outcome_contract",
        "expected_outcome_contract_state",
        "expected_outcome_contract",
        "turn_expected_outcome_profile",
    ):
        _append(payload.get(key))

    for container_key in (
        "outputs",
        "action_outputs",
        "last_action_outputs",
        "llm_step_envelope",
        "result",
    ):
        container = _copy_string_key_mapping(payload.get(container_key))
        if container is None:
            continue
        _append(container.get("validated_json"))
        _append(container.get("turn_expected_outcome_contract_state"))
        _append(container.get("turn_expected_outcome_contract"))
        _append(container.get("expected_outcome_contract_state"))
        _append(container.get("expected_outcome_contract"))

    return tuple(candidates)


def _dedupe_sources(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        cleaned = _clean_text(item)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(cleaned)
    return tuple(ordered)


def _dedupe_strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        cleaned = _clean_text(item)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(cleaned)
    return tuple(ordered)


def _normalise_required_tool_id(value: str) -> str:
    return value.strip()


def _dedupe_required_tools(values: Any) -> tuple[str, ...]:
    raw_values = _dedupe_strings(values)
    seen: set[str] = set()
    ordered: list[str] = []
    for raw_value in raw_values:
        tool_id = _normalise_required_tool_id(raw_value)
        if not tool_id:
            continue
        canonical_key = canonical_required_tool_key(tool_id)
        if not canonical_key or canonical_key in seen:
            continue
        seen.add(canonical_key)
        ordered.append(tool_id)
    return tuple(ordered)


def extract_vontology_concept_ids_from_text(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return _dedupe_strings(
        [
            match.group(0).rstrip(".,;:!?)]}'\"")
            for match in VONTOLOGY_CONCEPT_ID_PATTERN.finditer(value)
        ]
    )


def _target_concept_id_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return extract_vontology_concept_ids_from_text(value)
    if isinstance(value, Mapping):
        return _target_concept_id_values(
            value.get("concept_id") or value.get("target_concept_id") or value.get("id")
        )
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    ordered: list[str] = []
    for item in value:
        ordered.extend(_target_concept_id_values(item))
    return _dedupe_strings(ordered)


def _extract_target_concept_ids(
    *,
    payload: Mapping[str, Any],
    field_payload: Mapping[str, Any],
) -> tuple[str, ...]:
    ordered: list[str] = []
    for source_payload in (payload, field_payload):
        for field_name in TURN_EXPECTED_OUTCOME_TARGET_CONCEPT_FIELDS:
            if field_name not in source_payload:
                continue
            ordered.extend(_target_concept_id_values(source_payload.get(field_name)))
    return _dedupe_strings(ordered)


def _extract_target_type_ids(
    *,
    payload: Mapping[str, Any],
    field_payload: Mapping[str, Any],
) -> tuple[str, ...]:
    ordered: list[str] = []
    for source_payload in (payload, field_payload):
        for field_name in TURN_EXPECTED_OUTCOME_TARGET_TYPE_FIELDS:
            if field_name not in source_payload:
                continue
            ordered.extend(_target_concept_id_values(source_payload.get(field_name)))
    return _dedupe_strings(ordered)


def _extract_workflow_concept_ids(
    *,
    payload: Mapping[str, Any],
    field_payload: Mapping[str, Any],
) -> tuple[str, ...]:
    ordered: list[str] = []
    for source_payload in (payload, field_payload):
        for field_name in TURN_EXPECTED_OUTCOME_WORKFLOW_CONCEPT_FIELDS:
            if field_name not in source_payload:
                continue
            ordered.extend(_target_concept_id_values(source_payload.get(field_name)))
    return _dedupe_strings(ordered)


def _target_contracts_for_expected_outcome_payload(
    *,
    payload: Mapping[str, Any],
    field_payload: Mapping[str, Any],
    target_concept_ids: Sequence[str],
    target_type_ids: Sequence[str],
) -> tuple[TurnTargetContract, ...]:
    contracts: list[TurnTargetContract] = []
    contracts.extend(extract_target_contracts_from_payload(payload))
    if field_payload is not payload:
        contracts.extend(extract_target_contracts_from_payload(field_payload))
    concept_contract = TurnTargetContract.symbolic(
        concept_ids=target_concept_ids,
        binding_kind=TARGET_BINDING_ENTITY,
        source="target_concept_ids",
    )
    type_contract = TurnTargetContract.symbolic(
        concept_ids=target_type_ids,
        binding_kind=TARGET_BINDING_TYPE,
        source="target_type_ids",
    )
    if concept_contract is not None:
        contracts.append(concept_contract)
    if type_contract is not None:
        contracts.append(type_contract)
    return dedupe_target_contracts(contracts)


@dataclass(frozen=True)
class TurnExpectedOutcomeContract:
    summary: str | None = None
    grounding_requirement: str | None = None
    precision_policy: str | None = None
    selector_guidance: str | None = None
    answering_guidance: str | None = None
    reasoning: str | None = None
    required_tools: tuple[str, ...] = ()
    conditional_required_tools: tuple[str, ...] = ()
    target_concept_ids: tuple[str, ...] = ()
    target_type_ids: tuple[str, ...] = ()
    workflow_concept_ids: tuple[str, ...] = ()
    target_contracts: tuple[TurnTargetContract, ...] = ()
    sources: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Any,
        *,
        source: str | None = None,
    ) -> TurnExpectedOutcomeContract:
        payload = _copy_string_key_mapping(value) or {}
        nested_contracts = tuple(
            cls.from_mapping(
                nested,
                source=f"{source}.nested_expected_outcome_payload"
                if source
                else "nested_expected_outcome_payload",
            )
            for nested in _nested_expected_outcome_payload_candidates(payload)
        )
        field_payload = payload
        source_values = _dedupe_sources(payload.get("sources"))
        if payload.get(
            "schema_version"
        ) == TURN_EXPECTED_OUTCOME_CONTRACT_SCHEMA_VERSION and isinstance(
            payload.get("fields"), Mapping
        ):
            field_payload = _copy_string_key_mapping(payload.get("fields")) or {}
        if source:
            source_values = (*source_values, source)
        field_values: dict[str, Any] = {}
        for field_name in TURN_EXPECTED_OUTCOME_CONTRACT_FIELDS:
            candidate = _clean_text(field_payload.get(field_name))
            if not candidate:
                context_key = TURN_EXPECTED_OUTCOME_CONTEXT_FIELD_MAPPING.get(
                    field_name
                )
                if context_key:
                    candidate = _clean_text(field_payload.get(context_key))
            if not candidate:
                for alias in TURN_EXPECTED_OUTCOME_FIELD_ALIASES.get(field_name, ()):
                    candidate = _clean_text(field_payload.get(alias))
                    if candidate:
                        break
            field_values[field_name] = candidate
        required_tool_values: Any = None
        for source_payload in (payload, field_payload):
            for field_name in TURN_EXPECTED_OUTCOME_REQUIRED_TOOL_FIELDS:
                if field_name in source_payload:
                    required_tool_values = source_payload.get(field_name)
                    break
            if required_tool_values is not None:
                break
        required_tools = _dedupe_required_tools(required_tool_values)
        conditional_required_tool_values: Any = None
        for source_payload in (payload, field_payload):
            for field_name in TURN_EXPECTED_OUTCOME_CONDITIONAL_REQUIRED_TOOL_FIELDS:
                if field_name in source_payload:
                    conditional_required_tool_values = source_payload.get(field_name)
                    break
            if conditional_required_tool_values is not None:
                break
        conditional_required_tools = _dedupe_required_tools(
            conditional_required_tool_values
        )
        target_concept_ids = _extract_target_concept_ids(
            payload=payload,
            field_payload=field_payload,
        )
        target_type_ids = _extract_target_type_ids(
            payload=payload,
            field_payload=field_payload,
        )
        workflow_concept_ids = _extract_workflow_concept_ids(
            payload=payload,
            field_payload=field_payload,
        )
        target_contracts = _target_contracts_for_expected_outcome_payload(
            payload=payload,
            field_payload=field_payload,
            target_concept_ids=target_concept_ids,
            target_type_ids=target_type_ids,
        )
        contract = cls(
            summary=field_values["summary"],
            grounding_requirement=field_values["grounding_requirement"],
            precision_policy=field_values["precision_policy"],
            selector_guidance=field_values["selector_guidance"],
            answering_guidance=field_values["answering_guidance"],
            reasoning=field_values["reasoning"],
            required_tools=required_tools,
            conditional_required_tools=conditional_required_tools,
            target_concept_ids=target_concept_ids,
            target_type_ids=target_type_ids,
            workflow_concept_ids=workflow_concept_ids,
            target_contracts=target_contracts,
            sources=_dedupe_sources(list(source_values)),
        )
        if nested_contracts:
            return cls.merge_preferred(contract, *nested_contracts)
        return contract

    @classmethod
    def merge_preferred(
        cls,
        *contracts: TurnExpectedOutcomeContract | Mapping[str, Any] | None,
    ) -> TurnExpectedOutcomeContract:
        merged_fields: dict[str, str] = {}
        merged_sources: list[str] = []
        merged_required_tools: tuple[str, ...] = ()
        merged_conditional_required_tools: tuple[str, ...] = ()
        merged_target_concept_ids: list[str] = []
        merged_target_type_ids: list[str] = []
        merged_workflow_concept_ids: list[str] = []
        merged_target_contracts: list[TurnTargetContract] = []
        for raw_contract in contracts:
            contract = (
                raw_contract
                if isinstance(raw_contract, cls)
                else cls.from_mapping(raw_contract)
            )
            merged_sources.extend(contract.sources)
            if not merged_required_tools and contract.required_tools:
                merged_required_tools = contract.required_tools
            if (
                not merged_conditional_required_tools
                and contract.conditional_required_tools
            ):
                merged_conditional_required_tools = contract.conditional_required_tools
            merged_target_concept_ids.extend(contract.target_concept_ids)
            merged_target_type_ids.extend(contract.target_type_ids)
            merged_workflow_concept_ids.extend(contract.workflow_concept_ids)
            merged_target_contracts.extend(contract.target_contracts)
            if contract.is_empty():
                continue
            for field_name in TURN_EXPECTED_OUTCOME_CONTRACT_FIELDS:
                field_value = getattr(contract, field_name)
                if (
                    field_name not in merged_fields
                    and isinstance(field_value, str)
                    and field_value.strip()
                ):
                    merged_fields[field_name] = field_value.strip()
        return cls(
            summary=merged_fields.get("summary"),
            grounding_requirement=merged_fields.get("grounding_requirement"),
            precision_policy=merged_fields.get("precision_policy"),
            selector_guidance=merged_fields.get("selector_guidance"),
            answering_guidance=merged_fields.get("answering_guidance"),
            reasoning=merged_fields.get("reasoning"),
            required_tools=merged_required_tools,
            conditional_required_tools=merged_conditional_required_tools,
            target_concept_ids=_dedupe_strings(merged_target_concept_ids),
            target_type_ids=_dedupe_strings(merged_target_type_ids),
            workflow_concept_ids=_dedupe_strings(merged_workflow_concept_ids),
            target_contracts=dedupe_target_contracts(merged_target_contracts),
            sources=_dedupe_sources(merged_sources),
        )

    def is_empty(self) -> bool:
        return (
            not self.required_tools
            and not self.conditional_required_tools
            and not self.target_concept_ids
            and not self.target_type_ids
            and not self.workflow_concept_ids
            and not self.target_contracts
            and not any(
                isinstance(getattr(self, field_name), str)
                and getattr(self, field_name).strip()
                for field_name in TURN_EXPECTED_OUTCOME_CONTRACT_FIELDS
            )
        )

    def to_dict(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        for field_name in TURN_EXPECTED_OUTCOME_CONTRACT_FIELDS:
            field_value = getattr(self, field_name)
            if isinstance(field_value, str) and field_value.strip():
                payload[field_name] = field_value.strip()
        return payload

    def to_state_payload(self) -> dict[str, Any]:
        field_payload = self.to_dict()
        payload = {
            "schema_version": TURN_EXPECTED_OUTCOME_CONTRACT_SCHEMA_VERSION,
            "fields": field_payload,
            "field_count": len(field_payload),
            "sources": list(self.sources),
        }
        if self.required_tools:
            payload["required_tools"] = list(self.required_tools)
        if self.conditional_required_tools:
            payload["conditional_required_tools"] = list(
                self.conditional_required_tools
            )
        if self.target_concept_ids:
            payload["target_concept_ids"] = list(self.target_concept_ids)
        if self.target_type_ids:
            payload["target_type_ids"] = list(self.target_type_ids)
        if self.workflow_concept_ids:
            payload["workflow_concept_ids"] = list(self.workflow_concept_ids)
        if self.target_contracts:
            payload["target_contracts"] = [
                contract.to_state_payload() for contract in self.target_contracts
            ]
        return payload


def build_turn_expected_outcome_boundary_payload(
    contract: TurnExpectedOutcomeContract | Mapping[str, Any] | None,
    *,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract_object = (
        contract
        if isinstance(contract, TurnExpectedOutcomeContract)
        else TurnExpectedOutcomeContract.from_mapping(contract)
    )
    profile_payload = _copy_string_key_mapping(profile) or {}
    payload: dict[str, Any] = {}
    if profile_payload:
        payload["turn_expected_outcome_profile"] = dict(profile_payload)

    contract_payload = contract_object.to_dict()
    if (
        not contract_payload
        and not contract_object.required_tools
        and not contract_object.conditional_required_tools
        and not contract_object.target_concept_ids
        and not contract_object.target_type_ids
        and not contract_object.workflow_concept_ids
        and not contract_object.target_contracts
    ):
        return payload

    if contract_object.required_tools:
        payload[TURN_EXPECTED_OUTCOME_REQUIRED_TOOLS_CONTEXT_KEY] = list(
            contract_object.required_tools
        )
    if contract_object.conditional_required_tools:
        payload[TURN_EXPECTED_OUTCOME_CONDITIONAL_REQUIRED_TOOLS_CONTEXT_KEY] = list(
            contract_object.conditional_required_tools
        )
    if contract_object.target_concept_ids:
        payload[TURN_EXPECTED_OUTCOME_TARGET_CONCEPT_IDS_CONTEXT_KEY] = list(
            contract_object.target_concept_ids
        )
    if contract_object.target_type_ids:
        payload[TURN_EXPECTED_OUTCOME_TARGET_TYPE_IDS_CONTEXT_KEY] = list(
            contract_object.target_type_ids
        )
    if contract_object.workflow_concept_ids:
        payload[TURN_EXPECTED_OUTCOME_WORKFLOW_CONCEPT_IDS_CONTEXT_KEY] = list(
            contract_object.workflow_concept_ids
        )
    if contract_object.target_contracts:
        payload[TURN_EXPECTED_OUTCOME_TARGET_CONTRACTS_CONTEXT_KEY] = [
            contract.to_state_payload() for contract in contract_object.target_contracts
        ]

    if not profile_payload:
        profile_from_contract: dict[str, Any] = dict(contract_payload)
        if contract_object.required_tools:
            profile_from_contract["required_tools"] = list(
                contract_object.required_tools
            )
        if contract_object.conditional_required_tools:
            profile_from_contract["conditional_required_tools"] = list(
                contract_object.conditional_required_tools
            )
        if contract_object.target_concept_ids:
            profile_from_contract["target_concept_ids"] = list(
                contract_object.target_concept_ids
            )
        if contract_object.target_type_ids:
            profile_from_contract["target_type_ids"] = list(
                contract_object.target_type_ids
            )
        if contract_object.workflow_concept_ids:
            profile_from_contract["workflow_concept_ids"] = list(
                contract_object.workflow_concept_ids
            )
        if contract_object.target_contracts:
            profile_from_contract["target_contracts"] = [
                contract.to_state_payload()
                for contract in contract_object.target_contracts
            ]
        payload["turn_expected_outcome_profile"] = profile_from_contract
    if contract_payload:
        payload["turn_expected_outcome_contract"] = dict(contract_payload)
    payload["turn_expected_outcome_contract_state"] = contract_object.to_state_payload()
    for (
        contract_field,
        context_key,
    ) in TURN_EXPECTED_OUTCOME_CONTEXT_FIELD_MAPPING.items():
        field_value = contract_payload.get(contract_field)
        if isinstance(field_value, str) and field_value.strip():
            payload[context_key] = field_value.strip()
    return payload
