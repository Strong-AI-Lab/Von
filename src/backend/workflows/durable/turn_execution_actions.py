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


def _build_turn_discovery_query_text(data: Mapping[str, Any]) -> str:
    parts = [_normalise_turn_prompt(data)]
    for key in (
        "turn_expected_outcome_summary",
        "turn_expected_grounding_requirement",
        "turn_selector_guidance",
        "turn_answering_guidance",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    required_tools = data.get("turn_expected_required_tools")
    if isinstance(required_tools, Sequence) and not isinstance(
        required_tools, (str, bytes, bytearray)
    ):
        parts.extend(str(item) for item in required_tools if item)
    return "\n".join(part for part in parts if part)


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
        reused = dict(discovery)
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
            "selector_prompt_available",
            "selector_prompt_id",
            "selector_prompt_text",
            "selector_call_prompt_text",
            "selector_requested_prompt_ids",
            "selector_prompt_provenance",
            "selector_prompt_failure_reason",
            "selector_prompt_failure_detail",
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

        request_data = {
            str(key): value
            for key, value in request.data.items()
            if isinstance(key, str)
        }
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
                        continuation_context
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
                request.data.get("turn_expected_outcome_contract_state")
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
            }
        )
        return WorkflowActionResult(outputs=outputs)

    return _handle


def _build_turn_execution_prepare_recovery_retry_handler() -> Any:
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
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

        outputs: dict[str, Any] = {
            "selected_workflow_id": target_workflow_id,
            "workflow_continuation_launch_inputs": dict(launch_inputs),
            "turn_recovery_attempted": True,
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
