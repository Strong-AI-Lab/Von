"""Turn-scoped affordances for represented workflow discovery and execution.

This service adapts the existing actor-filtered workflow capability index and
verified durable submission surface to the thin ordinary-turn capability
interface.  It does not select a workflow or interpret request semantics.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.backend.security.access_control import override_current_actor

WORKFLOW_TURN_CAPABILITY_SCHEMA_VERSION = "workflow_turn_capability.v1"
WORKFLOW_TURN_DISCOVERY_SCHEMA_VERSION = "workflow_turn_capability_discovery.v1"
WORKFLOW_TURN_RECEIPT_SCHEMA_VERSION = "workflow_turn_effect_receipt.v1"
CAPABILITY_PLAN_PROFILE_SCHEMA_VERSION = "capability_plan_profile.v1"
CAPABILITY_EFFECT_PROFILE_SCHEMA_VERSION = "capability_effect_profile.v1"
CAPABILITY_COST_PROFILE_SCHEMA_VERSION = "capability_cost_profile.v1"
WORKFLOW_OBSERVATION_MAX_SECONDS = 90.0

_SERVER_PROVIDED_WORKFLOW_INPUT_KEYS = frozenset(
    {
        "actor_concept_id",
        "actor_user_concept_id",
        "augmented_context",
        "conversation_situation",
        "namespace",
        "org_concept_id",
        "organisation_concept_id",
        "prompt",
        "workflow_id",
        "user_id",
        "org_id",
        "user_concept_id",
        "user_namespace",
        "user_prompt",
        "workflow_launch_inputs",
    }
)
_MODEL_WORKFLOW_CONTROL_ARGUMENTS = frozenset(
    {
        "await_terminal",
        "include_step_result_envelopes",
        "include_trace",
        "inputs",
        "max_retries",
        "poll_interval_seconds",
        "timeout_seconds",
    }
)
_TERMINAL_SUCCESS_STATUSES = frozenset({"completed", "succeeded", "success"})
_TERMINAL_FAILURE_STATUSES = frozenset(
    {"cancelled", "canceled", "failed", "rejected", "terminated"}
)

_WORKFLOW_SEMANTIC_OUTCOMES = frozenset(
    {
        "blocked",
        "clarification_required",
        "completed",
        "failed",
        "follow_up_required",
        "not_completed",
        "partial",
    }
)

_WORKFLOW_NON_EFFECT_OUTCOMES = frozenset(
    {
        "blocked",
        "clarification_required",
        "failed",
        "follow_up_required",
        "not_completed",
    }
)


def _normalise_non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"1", "true", "yes", "on"}:
            return True
        if normalised in {"0", "false", "no", "off", ""}:
            return False
    return default


def _bounded_mapping_sequence(
    value: Any,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    return [dict(item) for item in list(value)[:limit] if isinstance(item, Mapping)]


def _bounded_text_tuple(value: Any, *, limit: int = 40) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalise_non_empty_text(item)
        if text is None or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def _coerce_optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _workflow_plan_metadata(
    raw_match: Mapping[str, Any],
    *,
    registered_capability_categories: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Project declared workflow shape without inventing semantic policy."""

    routing_index_metadata = raw_match.get("routing_index_metadata")
    index_metadata = (
        dict(routing_index_metadata)
        if isinstance(routing_index_metadata, Mapping)
        else {}
    )
    routing_profile_value = raw_match.get("routing_profile")
    routing_profile = (
        dict(routing_profile_value)
        if isinstance(routing_profile_value, Mapping)
        else {}
    )
    visible_categories = {
        str(name).strip(): str(category).strip().lower()
        for name, category in (registered_capability_categories or {}).items()
        if str(name).strip() and str(category).strip()
    }
    declared_components = _bounded_text_tuple(
        index_metadata.get("component_tools")
        or index_metadata.get("required_tools"),
    )
    visible_components = tuple(
        name for name in declared_components if name in visible_categories
    )
    compact_executability = index_metadata.get("compact_executability")
    declared_step_count = (
        _coerce_optional_non_negative_int(compact_executability.get("step_count"))
        if isinstance(compact_executability, Mapping)
        else None
    )
    direct_equivalents = tuple(
        name
        for name in _bounded_text_tuple(
            routing_profile.get("direct_equivalent_capability_names"),
            limit=12,
        )
        if name in visible_categories
    )

    component_categories = [
        visible_categories[name]
        for name in visible_components
        if name in visible_categories
    ]
    unresolved_component_count = max(
        0,
        len(declared_components) - len(visible_components),
    )
    if any(category == "write" for category in component_categories):
        semantic_effect: bool | None = True
        semantic_effect_source = "declared_registered_write_component"
    elif (
        declared_components
        and unresolved_component_count == 0
        and component_categories
        and all(category == "read" for category in component_categories)
    ):
        semantic_effect = False
        semantic_effect_source = "declared_registered_read_components"
    else:
        semantic_effect = None
        semantic_effect_source = "insufficient_declared_component_evidence"

    return {
        "component_capability_names": visible_components,
        "declared_component_count": len(declared_components),
        "unresolved_component_count": unresolved_component_count,
        "declared_step_count": declared_step_count,
        "direct_equivalent_capability_names": direct_equivalents,
        "semantic_effect": semantic_effect,
        "semantic_effect_source": semantic_effect_source,
    }


def _turn_capability_name(*, turn_id: str, workflow_id: str) -> str:
    digest = hashlib.sha256(f"{turn_id}\0{workflow_id}".encode("utf-8")).hexdigest()[
        :20
    ]
    return f"represented_workflow_{digest}"


def _mapping_source_input_key(source_expression: Any) -> str | None:
    expression = _normalise_non_empty_text(source_expression)
    if expression is None or not expression.startswith("inputs."):
        return None
    input_path = expression.removeprefix("inputs.").strip()
    if not input_path:
        return None
    return input_path.split(".", 1)[0].strip() or None


def _workflow_model_input_schema(
    contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    mappings = _bounded_mapping_sequence(
        contract.get("input_mappings") if isinstance(contract, Mapping) else None,
        limit=40,
    )
    represented_mappings: list[dict[str, Any]] = []
    model_properties: dict[str, Any] = {}
    required_model_inputs: list[str] = []
    explicit_required_inputs = {
        str(item).strip()
        for item in (
            contract.get("required_inputs", [])
            if isinstance(contract, Mapping)
            else []
        )
        if isinstance(item, str) and item.strip()
    }

    for mapping in mappings:
        target_key = _normalise_non_empty_text(
            mapping.get("target_context_key")
            or mapping.get("workflow_context_key")
            or mapping.get("context_key")
            or mapping.get("target_key")
        )
        source_expression = _normalise_non_empty_text(
            mapping.get("source_expression")
            or mapping.get("source")
            or mapping.get("source_context_key")
            or mapping.get("source_path")
        )
        if target_key is None or source_expression is None:
            continue
        source_input_key = _mapping_source_input_key(source_expression)
        required = bool(mapping.get("required")) or target_key in explicit_required_inputs
        represented_mapping = {
            "target_context_key": target_key,
            "source_expression": source_expression,
            "extractor": (
                _normalise_non_empty_text(mapping.get("extractor")) or "identity"
            ),
            "required": required,
        }
        description = _normalise_non_empty_text(mapping.get("description"))
        if description is not None:
            represented_mapping["description"] = description[:500]
        represented_mappings.append(represented_mapping)

        if (
            source_input_key is None
            or source_input_key in _SERVER_PROVIDED_WORKFLOW_INPUT_KEYS
        ):
            continue
        property_schema: dict[str, Any] = {}
        if description is not None:
            property_schema["description"] = description[:500]
        model_properties.setdefault(source_input_key, property_schema)
        if required and source_input_key not in required_model_inputs:
            required_model_inputs.append(source_input_key)

    inputs_schema: dict[str, Any] = {
        "type": "object",
        "properties": model_properties,
        "additionalProperties": True,
        "description": (
            "Workflow launch inputs not already supplied by the server from the "
            "turn prompt, authenticated actor, context, or explicit request inputs."
        ),
    }
    if required_model_inputs:
        inputs_schema["required"] = sorted(required_model_inputs)

    return {
        "type": "object",
        "properties": {
            "inputs": inputs_schema,
            "await_terminal": {
                "type": "boolean",
                "description": (
                    "Wait for a terminal workflow state before returning. "
                    "Defaults to true."
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 0.1,
                "maximum": WORKFLOW_OBSERVATION_MAX_SECONDS,
                "description": (
                    "One bounded observation interval of at most 90 seconds. "
                    "Expiry returns partial/current state; the model may choose "
                    "another wait."
                ),
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 0.0,
            },
            "include_trace": {"type": "boolean"},
            "include_step_result_envelopes": {"type": "boolean"},
            "max_retries": {"type": "integer", "minimum": 0, "maximum": 50},
        },
        "additionalProperties": False,
        "x-von-represented-launch-input-mappings": represented_mappings,
        "x-von-server-provided-inputs": sorted(
            _SERVER_PROVIDED_WORKFLOW_INPUT_KEYS
        ),
    }


@dataclass(frozen=True)
class WorkflowTurnCapability:
    """One actor-visible workflow exposed as a turn-bound capability."""

    name: str
    workflow_id: str
    display_name: str
    description: str
    relevance_score: float
    input_schema: Mapping[str, Any]
    launch_input_contract_source: str | None = None
    discovery_metadata: Mapping[str, Any] = field(default_factory=dict)
    outcome_explanation_prompt_support: Mapping[str, Any] = field(
        default_factory=dict
    )
    component_capability_names: tuple[str, ...] = ()
    declared_component_count: int = 0
    unresolved_component_count: int = 0
    declared_step_count: int | None = None
    direct_equivalent_capability_names: tuple[str, ...] = ()
    semantic_effect: bool | None = None
    semantic_effect_source: str = "insufficient_declared_component_evidence"

    def _outcome_explanation_prompt_keys(self) -> list[str]:
        prompts = self.outcome_explanation_prompt_support.get("prompts")
        if not isinstance(prompts, Mapping):
            return []
        return sorted(str(key) for key in prompts)

    def to_catalogue_entry(self) -> dict[str, Any]:
        effect_profile = {
            "schema_version": CAPABILITY_EFFECT_PROFILE_SCHEMA_VERSION,
            "semantic_effect": self.semantic_effect,
            "semantic_effect_source": self.semantic_effect_source,
            "operational_state_effect": True,
            "operational_state_effect_reason": (
                "workflow invocation creates or advances a durable instance"
            ),
        }
        cost_profile: dict[str, Any] = {
            "schema_version": CAPABILITY_COST_PROFILE_SCHEMA_VERSION,
            "basis": "declared_plan_structure",
            "durable_runtime": True,
            "declared_component_count": int(self.declared_component_count),
            "visible_component_count": len(self.component_capability_names),
            "unresolved_component_count": int(self.unresolved_component_count),
        }
        if self.declared_step_count is not None:
            cost_profile["declared_step_count"] = int(self.declared_step_count)
        plan_profile = {
            "schema_version": CAPABILITY_PLAN_PROFILE_SCHEMA_VERSION,
            "shape": "represented_workflow",
            "component_capability_names": list(self.component_capability_names),
            "direct_equivalent_capability_names": list(
                self.direct_equivalent_capability_names
            ),
            "effect_profile": effect_profile,
            "cost_profile": cost_profile,
        }
        return {
            "schema_version": WORKFLOW_TURN_CAPABILITY_SCHEMA_VERSION,
            "name": self.name,
            "kind": "represented_workflow",
            "workflow_id": self.workflow_id,
            "display_name": self.display_name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "query_match": True,
            "semantic_effect": self.semantic_effect,
            "effect_profile": effect_profile,
            "plan_profile": plan_profile,
            "server_bound_arguments": [
                "workflow_id",
                "user_id",
                "org_id",
                "namespace",
            ],
            "terminal_readback": True,
            "relevance_score": round(float(self.relevance_score), 4),
            "launch_input_contract_source": self.launch_input_contract_source,
            "outcome_explanation_prompt_keys": (
                self._outcome_explanation_prompt_keys()
            ),
            "discovery_metadata": dict(self.discovery_metadata),
        }


def discover_turn_workflow_capabilities(
    query: str,
    *,
    namespace: str | None,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    turn_id: str,
    max_results: int = 5,
    timeout_seconds: float = 5.0,
    registered_capability_categories: Mapping[str, str] | None = None,
) -> tuple[list[WorkflowTurnCapability], dict[str, Any]]:
    """Return bounded actor-accessible workflow affordances for a model query."""

    query_text = _normalise_non_empty_text(query)
    if query_text is None:
        return [], {
            "schema_version": WORKFLOW_TURN_DISCOVERY_SCHEMA_VERSION,
            "status": "not_requested",
            "match_count": 0,
        }
    if not _normalise_non_empty_text(user_concept_id) or not _normalise_non_empty_text(
        namespace
    ):
        return [], {
            "schema_version": WORKFLOW_TURN_DISCOVERY_SCHEMA_VERSION,
            "status": "authenticated_actor_required",
            "match_count": 0,
        }

    from src.backend.services.workflow_discovery_service import (
        discover_workflows_for_turn,
    )
    from src.backend.workflows.outcome_explanation_prompt_support import (
        resolve_workflow_outcome_explanation_prompt_support,
    )
    from src.backend.workflows.vontology_loader import (
        resolve_workflow_launch_input_contract,
    )

    bounded_max_results = max(1, min(10, int(max_results)))
    bounded_timeout_seconds = max(0.1, min(10.0, float(timeout_seconds)))
    with override_current_actor(user_concept_id, organisation_concept_id):
        payload = discover_workflows_for_turn(
            query_text,
            namespace=namespace,
            max_results=bounded_max_results,
            timeout_seconds=bounded_timeout_seconds,
            allow_non_executable=False,
        )

        matches = (
            payload.get("matches")
            if isinstance(payload, Mapping)
            and isinstance(payload.get("matches"), list)
            else []
        )
        capabilities: list[WorkflowTurnCapability] = []
        seen_workflow_ids: set[str] = set()
        contract_errors: list[dict[str, Any]] = []
        for raw_match in matches[:bounded_max_results]:
            if not isinstance(raw_match, Mapping):
                continue
            workflow_id = _normalise_non_empty_text(raw_match.get("concept_id"))
            if workflow_id is None or workflow_id in seen_workflow_ids:
                continue
            if raw_match.get("is_executable") is not True:
                continue
            if raw_match.get("routing_eligible") is not True:
                continue
            seen_workflow_ids.add(workflow_id)
            try:
                launch_contract, launch_contract_source = (
                    resolve_workflow_launch_input_contract(workflow_id)
                )
            except Exception as exc:  # noqa: BLE001
                launch_contract = None
                launch_contract_source = None
                contract_errors.append(
                    {
                        "workflow_id": workflow_id,
                        "stage": "launch_input_contract_resolution",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
            try:
                outcome_explanation_prompt_support = (
                    resolve_workflow_outcome_explanation_prompt_support(workflow_id)
                    or {}
                )
            except Exception as exc:  # noqa: BLE001 - optional represented guidance
                outcome_explanation_prompt_support = {}
                contract_errors.append(
                    {
                        "workflow_id": workflow_id,
                        "stage": "outcome_explanation_prompt_resolution",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
            routing_profile = raw_match.get("routing_profile")
            routing_index_metadata = raw_match.get("routing_index_metadata")
            plan_metadata = _workflow_plan_metadata(
                raw_match,
                registered_capability_categories=(registered_capability_categories),
            )
            capabilities.append(
                WorkflowTurnCapability(
                    name=_turn_capability_name(
                        turn_id=turn_id,
                        workflow_id=workflow_id,
                    ),
                    workflow_id=workflow_id,
                    display_name=(
                        _normalise_non_empty_text(raw_match.get("name"))
                        or workflow_id
                    ),
                    description=(
                        _normalise_non_empty_text(raw_match.get("description"))
                        or f"Execute represented workflow {workflow_id}."
                    ),
                    relevance_score=_coerce_relevance_score(
                        raw_match.get("relevance_score")
                    ),
                    input_schema=_workflow_model_input_schema(launch_contract),
                    launch_input_contract_source=(
                        _normalise_non_empty_text(launch_contract_source)
                    ),
                    outcome_explanation_prompt_support=(
                        outcome_explanation_prompt_support
                    ),
                    discovery_metadata={
                        "match_source": raw_match.get("match_source"),
                        "executability_reason": raw_match.get(
                            "executability_reason"
                        ),
                        "routing_readiness_status": raw_match.get(
                            "routing_readiness_status"
                        ),
                        "routing_profile": (
                            dict(routing_profile)
                            if isinstance(routing_profile, Mapping)
                            else None
                        ),
                        "routing_index_metadata": (
                            {
                                key: routing_index_metadata.get(key)
                                for key in (
                                    "authority_source",
                                    "compact_executability",
                                    "component_tools_source",
                                    "description_source",
                                    "publication_lifecycle",
                                    "required_tools_source",
                                    "routing_profile_source",
                                    "workflow_action_ids_source",
                                )
                                if key in routing_index_metadata
                            }
                            if isinstance(routing_index_metadata, Mapping)
                            else None
                        ),
                    },
                    component_capability_names=tuple(
                        plan_metadata["component_capability_names"]
                    ),
                    declared_component_count=int(
                        plan_metadata["declared_component_count"]
                    ),
                    unresolved_component_count=int(
                        plan_metadata["unresolved_component_count"]
                    ),
                    declared_step_count=plan_metadata["declared_step_count"],
                    direct_equivalent_capability_names=tuple(
                        plan_metadata["direct_equivalent_capability_names"]
                    ),
                    semantic_effect=plan_metadata["semantic_effect"],
                    semantic_effect_source=str(plan_metadata["semantic_effect_source"]),
                )
            )

    diagnostic = {
        "schema_version": WORKFLOW_TURN_DISCOVERY_SCHEMA_VERSION,
        "status": "completed",
        "query": query_text,
        "match_count": len(capabilities),
        "candidate_count": (
            payload.get("candidate_count") if isinstance(payload, Mapping) else None
        ),
        "search_time_ms": (
            payload.get("search_time_ms") if isinstance(payload, Mapping) else None
        ),
        "budget_exhausted": bool(
            payload.get("budget_exhausted")
            if isinstance(payload, Mapping)
            else False
        ),
        "elapsed_time_enforcement": (
            payload.get("elapsed_time_enforcement")
            if isinstance(payload, Mapping)
            else "advisory"
        ),
        "advisory_budget_exceeded": bool(
            payload.get("advisory_budget_exceeded")
            if isinstance(payload, Mapping)
            else False
        ),
        "hard_timeout_exceeded": bool(
            payload.get("hard_timeout_exceeded")
            if isinstance(payload, Mapping)
            else False
        ),
        "match_absence_reason": (
            payload.get("match_absence_reason")
            if isinstance(payload, Mapping)
            else None
        ),
        "errors": (
            [*list(payload.get("errors") or [])[:5], *contract_errors[:5]]
            if isinstance(payload, Mapping)
            else contract_errors[:5]
        ),
    }
    return capabilities, diagnostic


def _bounded_context_projection(
    context: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    remaining_chars = 24_000
    for item in list(context or ())[-20:]:
        if not isinstance(item, Mapping) or remaining_chars <= 0:
            continue
        role = _normalise_non_empty_text(item.get("role")) or "context"
        content = item.get("content")
        if isinstance(content, str):
            bounded_content = content[: min(4_000, remaining_chars)]
            projected.append({"role": role, "content": bounded_content})
            remaining_chars -= len(bounded_content)
    return projected


def _coerce_relevance_score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def build_workflow_execution_arguments(
    capability: WorkflowTurnCapability,
    model_arguments: Mapping[str, Any],
    *,
    prompt: str,
    context: Sequence[Mapping[str, Any]] | None,
    conversation_situation: str | None,
    request_workflow_launch_inputs: Mapping[str, Any] | None,
    user_concept_id: str,
    organisation_concept_id: str | None,
    namespace: str,
    maximum_wait_seconds: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind workflow identity/actor and build verified-submission arguments."""

    unknown_arguments = sorted(
        str(key)
        for key in model_arguments
        if str(key) not in _MODEL_WORKFLOW_CONTROL_ARGUMENTS
    )
    raw_model_inputs = model_arguments.get("inputs")
    if raw_model_inputs is None:
        model_inputs: dict[str, Any] = {}
    elif isinstance(raw_model_inputs, Mapping):
        model_inputs = {
            str(key): value
            for key, value in raw_model_inputs.items()
            if isinstance(key, str)
            and key.strip()
            and key not in _SERVER_PROVIDED_WORKFLOW_INPUT_KEYS
            and not key.startswith("__")
        }
    else:
        raise ValueError("workflow capability inputs must be an object")

    trusted_request_inputs = (
        {
            str(key): value
            for key, value in request_workflow_launch_inputs.items()
            if isinstance(key, str)
            and key.strip()
            and key not in _SERVER_PROVIDED_WORKFLOW_INPUT_KEYS
            and not key.startswith("__")
        }
        if isinstance(request_workflow_launch_inputs, Mapping)
        else {}
    )
    launch_inputs = dict(model_inputs)
    for key, value in trusted_request_inputs.items():
        if isinstance(key, str) and key.strip() and not key.startswith("__"):
            launch_inputs[key] = value
    if trusted_request_inputs:
        launch_inputs["workflow_launch_inputs"] = dict(trusted_request_inputs)
    launch_inputs.update(
        {
            "actor_concept_id": user_concept_id,
            "actor_user_concept_id": user_concept_id,
            "user_concept_id": user_concept_id,
            "namespace": namespace,
            "user_namespace": namespace,
        }
    )
    if organisation_concept_id is not None:
        launch_inputs.update(
            {
                "org_concept_id": organisation_concept_id,
                "organisation_concept_id": organisation_concept_id,
            }
        )
    clean_prompt = str(prompt or "").strip()
    if clean_prompt:
        launch_inputs["prompt"] = clean_prompt
        launch_inputs["user_prompt"] = clean_prompt
    context_projection = _bounded_context_projection(context)
    if context_projection:
        launch_inputs["augmented_context"] = context_projection
    clean_situation = str(conversation_situation or "").strip()
    if clean_situation:
        launch_inputs["conversation_situation"] = clean_situation[:24_000]

    try:
        requested_wait = float(model_arguments.get("timeout_seconds", 60.0))
    except (TypeError, ValueError):
        requested_wait = 60.0
    if not math.isfinite(requested_wait):
        raise ValueError("timeout_seconds must be finite")
    effective_wait = max(
        0.1,
        min(
            requested_wait if requested_wait > 0.0 else 0.1,
            WORKFLOW_OBSERVATION_MAX_SECONDS,
        ),
    )
    if maximum_wait_seconds is not None:
        try:
            bounded_maximum_wait = float(maximum_wait_seconds)
        except (TypeError, ValueError):
            bounded_maximum_wait = WORKFLOW_OBSERVATION_MAX_SECONDS
        if not math.isfinite(bounded_maximum_wait):
            bounded_maximum_wait = WORKFLOW_OBSERVATION_MAX_SECONDS
        effective_wait = min(
            effective_wait,
            max(0.1, bounded_maximum_wait),
        )
    try:
        poll_interval = float(model_arguments.get("poll_interval_seconds", 0.5))
    except (TypeError, ValueError):
        poll_interval = 0.5
    if not math.isfinite(poll_interval):
        raise ValueError("poll_interval_seconds must be finite")
    try:
        max_retries = int(model_arguments.get("max_retries", 3))
    except (TypeError, ValueError):
        max_retries = 3

    effective_arguments = {
        "workflow_id": capability.workflow_id,
        "user_id": user_concept_id,
        "namespace": namespace,
        "inputs": launch_inputs,
        "max_retries": max(0, min(max_retries, 50)),
        "await_terminal": _coerce_bool(
            model_arguments.get("await_terminal"),
            default=True,
        ),
        "timeout_seconds": effective_wait,
        "poll_interval_seconds": max(0.0, poll_interval),
        "include_step_result_envelopes": _coerce_bool(
            model_arguments.get("include_step_result_envelopes"),
            default=False,
        ),
        "include_trace": _coerce_bool(
            model_arguments.get("include_trace"),
            default=False,
        ),
    }
    if organisation_concept_id is not None:
        effective_arguments["org_id"] = organisation_concept_id
    binding_diagnostics = {
        "schema_version": "workflow_turn_capability_binding.v1",
        "capability_name": capability.name,
        "workflow_id": capability.workflow_id,
        "server_bound_arguments": [
            "workflow_id",
            "user_id",
            "org_id",
            "namespace",
        ],
        "ignored_model_arguments": unknown_arguments,
        "trusted_request_input_keys": sorted(trusted_request_inputs),
        "model_input_keys": sorted(model_inputs),
        "effective_wait_seconds": effective_wait,
    }
    return effective_arguments, binding_diagnostics


def normalise_workflow_effect_receipt(
    payload: Any,
    *,
    capability: WorkflowTurnCapability,
) -> Any:
    """Project durable workflow launch/terminal state into effect semantics."""

    if not isinstance(payload, Mapping):
        return payload
    receipt = dict(payload)
    instance_id = _normalise_non_empty_text(receipt.get("instance_id"))
    final_status = _normalise_non_empty_text(receipt.get("final_status"))
    workflow_instance = receipt.get("workflow_instance")
    if final_status is None and isinstance(workflow_instance, Mapping):
        final_status = _normalise_non_empty_text(workflow_instance.get("status"))
    status_key = (final_status or "").lower()
    timed_out = bool(receipt.get("timed_out"))
    created_new = receipt.get("created_new")
    incoming_mutation_outcome = (
        _normalise_non_empty_text(receipt.get("mutation_outcome")) or ""
    ).lower()
    incoming_error_code = (
        _normalise_non_empty_text(receipt.get("error_code")) or ""
    ).lower()
    explicit_not_started = bool(
        status_key == "not_started"
        or incoming_mutation_outcome == "not_started"
    )
    explicit_indeterminate = bool(
        incoming_mutation_outcome == "unknown"
        or incoming_error_code == "tool_timeout_outcome_unknown"
    )
    changed: bool | None = (
        created_new if instance_id is not None and isinstance(created_new, bool) else None
    )

    if instance_id is None and explicit_not_started:
        effect_status = "not_started"
        changed = False
    elif instance_id is None and explicit_indeterminate:
        effect_status = "indeterminate"
        changed = None
    elif receipt.get("success") is False and instance_id is None:
        effect_status = "not_started"
        changed = False
    elif status_key in _TERMINAL_SUCCESS_STATUSES and not timed_out:
        effect_status = "succeeded"
    elif status_key in _TERMINAL_FAILURE_STATUSES:
        effect_status = "failed"
    elif (
        timed_out
        or status_key in {"pending", "queued", "running", "paused"}
        or not status_key
    ):
        effect_status = "partial" if instance_id is not None else "failed"
    else:
        effect_status = "partial" if instance_id is not None else "failed"

    workflow_execution = receipt.get("workflow_execution")
    workflow_outputs = (
        workflow_execution.get("outputs")
        if isinstance(workflow_execution, Mapping)
        and isinstance(workflow_execution.get("outputs"), Mapping)
        else None
    )
    if workflow_outputs is None and isinstance(workflow_instance, Mapping):
        candidate_outputs = workflow_instance.get("outputs")
        workflow_outputs = (
            candidate_outputs if isinstance(candidate_outputs, Mapping) else None
        )
    explicit_semantic_outcome = (
        _normalise_non_empty_text(workflow_outputs.get("semantic_outcome"))
        if isinstance(workflow_outputs, Mapping)
        else None
    )
    if explicit_semantic_outcome:
        explicit_semantic_outcome = explicit_semantic_outcome.lower().replace(" ", "_")
    clarification_required = bool(
        effect_status == "succeeded"
        and isinstance(workflow_outputs, Mapping)
        and (
            workflow_outputs.get("requires_user_affirmation") is True
            or workflow_outputs.get("needs_user_affirmation") is True
            or explicit_semantic_outcome == "clarification_required"
        )
    )
    if effect_status != "succeeded":
        semantic_outcome = "not_completed"
    elif clarification_required:
        semantic_outcome = "clarification_required"
    elif explicit_semantic_outcome in _WORKFLOW_SEMANTIC_OUTCOMES:
        semantic_outcome = explicit_semantic_outcome
    elif (
        isinstance(workflow_outputs, Mapping)
        and workflow_outputs.get("follow_up_required") is True
    ):
        semantic_outcome = "follow_up_required"
    else:
        semantic_outcome = "completed"
    semantic_effect = (
        False
        if effect_status == "succeeded"
        and semantic_outcome in _WORKFLOW_NON_EFFECT_OUTCOMES
        else capability.semantic_effect
    )

    receipt.update(
        {
            "workflow_turn_receipt_schema_version": (
                WORKFLOW_TURN_RECEIPT_SCHEMA_VERSION
            ),
            "capability_kind": "represented_workflow",
            "capability_name": capability.name,
            "workflow_id": capability.workflow_id,
            "effect_status": effect_status,
            "changed": changed,
            "change_kind": "workflow_instance_operational_state",
            "operational_changed": changed,
            "semantic_effect": semantic_effect,
            "semantic_outcome": semantic_outcome,
            "operational_state_effect": True,
            "mutation_outcome": (
                "completed"
                if effect_status == "succeeded"
                else (
                    "partial"
                    if instance_id is not None
                    else (
                        "unknown"
                        if effect_status == "indeterminate"
                        else "not_started"
                    )
                )
            ),
            "outcome_finality": "terminal_for_turn",
        }
    )
    explanation_outcome_key = (
        semantic_outcome
        if effect_status == "succeeded" and semantic_outcome != "completed"
        else effect_status
    )
    from src.backend.workflows.outcome_explanation_prompt_support import (
        select_workflow_outcome_explanation_prompt,
    )

    outcome_explanation_support = select_workflow_outcome_explanation_prompt(
        capability.outcome_explanation_prompt_support,
        outcome_key=explanation_outcome_key,
    )
    if outcome_explanation_support is not None:
        receipt["outcome_explanation_support"] = outcome_explanation_support
    if effect_status != "succeeded":
        recovery_affordances = list(receipt.get("recovery_affordances") or [])
        if instance_id is not None:
            workflow_execution = receipt.get("workflow_execution")
            prior_poll_interval = (
                workflow_execution.get("poll_interval_seconds")
                if isinstance(workflow_execution, Mapping)
                else None
            )
            wait_arguments: dict[str, Any] = {"instance_id": instance_id}
            if effect_status == "partial":
                # The durable handle is already established. Inspecting once is
                # the least burdensome recovery; another long wait remains an
                # adaptive choice only when the user's outcome requires it.
                wait_arguments["await_terminal"] = False
                if isinstance(prior_poll_interval, (int, float)) and not isinstance(
                    prior_poll_interval,
                    bool,
                ) and math.isfinite(float(prior_poll_interval)):
                    wait_arguments["poll_interval_seconds"] = max(
                        0.0,
                        float(prior_poll_interval),
                    )
            recovery_affordances.append(
                {
                    "action_type": (
                        "inspect_pending_workflow_instance"
                        if effect_status == "partial"
                        else "inspect_workflow_instance"
                    ),
                    "capability": "workflow_get_instance",
                    "arguments": wait_arguments,
                    "semantic_effect": (
                        (
                            "inspect the already-submitted instance without "
                            "spending another observation interval waiting"
                            if effect_status == "partial"
                            else "inspect the terminal workflow instance"
                        )
                        + "; do not cancel, retry, or create another instance"
                    ),
                }
            )
        else:
            recovery_affordances.append(
                {
                    "action_type": (
                        "inspect_operation_state_before_retry"
                        if effect_status == "indeterminate"
                        else "inspect_launch_failure_before_retry"
                    ),
                    "workflow_id": capability.workflow_id,
                }
            )
        receipt["recovery_affordances"] = recovery_affordances
    return receipt


__all__ = [
    "WorkflowTurnCapability",
    "build_workflow_execution_arguments",
    "discover_turn_workflow_capabilities",
    "normalise_workflow_effect_receipt",
]
