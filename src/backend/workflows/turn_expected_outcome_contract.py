"""Shared turn expected-outcome contract support surfaces.

This module provides one canonical serialisable boundary payload for the
turn-level expected-outcome contract plus a typed Python helper used by the
orchestrator, durable turn-runtime support, and turn-execution reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

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
TURN_EXPECTED_OUTCOME_REQUIRED_TOOL_ALIASES: dict[str, str] = {
    "get_predicate_extent": "get_predicate_incidence",
    "get_predicates_for_class": "get_predicate_incidence",
}
TURN_EXPECTED_OUTCOME_TARGET_CONCEPT_FIELDS: tuple[str, ...] = (
    "target_concept_id",
    "target_concept_ids",
    "target_concepts",
    "focal_concept_id",
    "focal_concept_ids",
    "focal_concepts",
    "required_target_concept_ids",
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
    cleaned = value.strip()
    if not cleaned:
        return ""
    return TURN_EXPECTED_OUTCOME_REQUIRED_TOOL_ALIASES.get(
        cleaned.lower(),
        cleaned,
    )


def _dedupe_required_tools(values: Any) -> tuple[str, ...]:
    raw_values = _dedupe_strings(values)
    seen: set[str] = set()
    ordered: list[str] = []
    for raw_value in raw_values:
        tool_id = _normalise_required_tool_id(raw_value)
        if not tool_id:
            continue
        lowered = tool_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
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


@dataclass(frozen=True)
class TurnExpectedOutcomeContract:
    summary: str | None = None
    grounding_requirement: str | None = None
    precision_policy: str | None = None
    selector_guidance: str | None = None
    answering_guidance: str | None = None
    reasoning: str | None = None
    required_tools: tuple[str, ...] = ()
    target_concept_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Any,
        *,
        source: str | None = None,
    ) -> TurnExpectedOutcomeContract:
        payload = _copy_string_key_mapping(value) or {}
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
        required_tools = _dedupe_required_tools(
            payload.get("required_tools")
            if "required_tools" in payload
            else field_payload.get("required_tools")
        )
        target_concept_ids = _extract_target_concept_ids(
            payload=payload,
            field_payload=field_payload,
        )
        return cls(
            summary=field_values["summary"],
            grounding_requirement=field_values["grounding_requirement"],
            precision_policy=field_values["precision_policy"],
            selector_guidance=field_values["selector_guidance"],
            answering_guidance=field_values["answering_guidance"],
            reasoning=field_values["reasoning"],
            required_tools=required_tools,
            target_concept_ids=target_concept_ids,
            sources=_dedupe_sources(list(source_values)),
        )

    @classmethod
    def merge_preferred(
        cls,
        *contracts: TurnExpectedOutcomeContract | Mapping[str, Any] | None,
    ) -> TurnExpectedOutcomeContract:
        merged_fields: dict[str, str] = {}
        merged_sources: list[str] = []
        merged_required_tools: tuple[str, ...] = ()
        merged_target_concept_ids: list[str] = []
        for raw_contract in contracts:
            contract = (
                raw_contract
                if isinstance(raw_contract, cls)
                else cls.from_mapping(raw_contract)
            )
            merged_sources.extend(contract.sources)
            if not merged_required_tools and contract.required_tools:
                merged_required_tools = contract.required_tools
            merged_target_concept_ids.extend(contract.target_concept_ids)
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
            target_concept_ids=_dedupe_strings(merged_target_concept_ids),
            sources=_dedupe_sources(merged_sources),
        )

    def is_empty(self) -> bool:
        return (
            not self.required_tools
            and not self.target_concept_ids
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
        if self.target_concept_ids:
            payload["target_concept_ids"] = list(self.target_concept_ids)
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
        and not contract_object.target_concept_ids
    ):
        return payload

    if not profile_payload:
        profile_from_contract: dict[str, Any] = dict(contract_payload)
        if contract_object.required_tools:
            profile_from_contract["required_tools"] = list(
                contract_object.required_tools
            )
        if contract_object.target_concept_ids:
            profile_from_contract["target_concept_ids"] = list(
                contract_object.target_concept_ids
            )
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
