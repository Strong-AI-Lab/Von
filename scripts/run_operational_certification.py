#!/usr/bin/env python3
"""Run or evaluate the Vontology-authoritative operational certification suite.

Live execution supports generic authenticated single-/multi-turn ``/von/generate``
and durable ``workflow_execute`` submission/resume transports. Scenario prompts,
workflows, inputs,
evaluators, checks, minefields, budgets, and gates are represented suite data.
Missing authority or adapter capability is a failing typed outcome.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import copy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse
import uuid

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.live_test_server_defaults import (
    get_default_agent_test_base_url,
)  # noqa: E402
from scripts.run_authenticated_browser_workflow_replay import (  # noqa: E402
    ReplayCase,
    apply_target_session_context,
    collect_run_environment,
    create_replay_chat_session,
    establish_browser_test_session,
    extract_observed_workflow_ids,
    extract_progress_facts,
    extract_selected_workflow_ids,
    extract_selector_diagnostics,
    extract_visible_answer,
    get_auth_status,
    poll_replay_task,
    require_agent_test_server,
    submit_background_generate,
)
from scripts.run_live_multi_turn_followup_replay import (  # noqa: E402
    extract_visible_answer_from_turn_record,
    fetch_turn_record,
)
from src.backend.services.benchmark_suite_vontology_service import (  # noqa: E402
    BenchmarkSuiteAuthorityMissingError,
    OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
    load_operational_certification_contract,
)
from src.backend.services.agent_test_replay_mode_service import (  # noqa: E402
    AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM,
)
from src.backend.services.experiment_run_service import (  # noqa: E402
    compute_experiment_verdict,
    create_experiment_spec,
    get_experiment_run_state,
    record_experiment_observation,
    start_experiment_run,
)
from src.backend.services.operational_certification_contract_service import (  # noqa: E402
    CertificationContractValidationError,
    aggregate_five_trial_campaign,
    evaluate_scenario_trial,
    json_serialisable_projection,
    stable_payload_digest,
)
from src.backend.services.operational_certification_runner_service import (  # noqa: E402
    run_operational_certification_campaign,
    validate_campaign_experiment_observation,
)
from src.backend.services.operational_certification_attestation_service import (  # noqa: E402
    verify_operational_certification_runner_attestation,
)
from src.backend.services.operational_certification_vontology_service import (  # noqa: E402
    OPERATIONAL_CERTIFICATION_CAMPAIGN_EVIDENCE_CONCEPT_ID,
    load_represented_operational_campaign_evidence,
)


AUTHENTICATED_GENERATE_ADAPTER_ID = "#V#authenticated_von_generate_operational_adapter"
AUTHENTICATED_MULTI_TURN_ADAPTER_ID = (
    "#V#authenticated_von_multi_turn_operational_adapter"
)
DURABLE_WORKFLOW_ADAPTER_ID = "#V#durable_workflow_execute_operational_adapter"
_REQUIRED_REPRESENTED_SELECTOR_TELEMETRY_FIELDS = (
    "prompt_present",
    "candidate_list_present",
    "response_present",
    "candidate_entries_present",
    "context_lineage_present",
    "selector_prompt_event_present",
    "selector_response_event_present",
    "model_name_present",
)
_REPRESENTED_SELECTOR_SOURCES = frozenset(
    {
        "selector",
        "selector_llm_verdict",
        "workflow_selector",
    }
)
_REPRESENTED_SELECTOR_RESOLUTIONS = frozenset(
    {
        "candidate_label_exact_match",
        "raw_response_contains_candidate_id",
    }
)


def _canonical_selector_source(value: Any) -> str:
    source = _text(value).lower()
    if source in _REPRESENTED_SELECTOR_SOURCES:
        return "workflow_selector"
    return source


MIGRATION_FIXTURE_PATH = (
    PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "operational_certification_benchmark_seed_bundle.json"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return list(value)
    return []


def _walk_mappings(value: Any) -> list[Mapping[str, Any]]:
    mappings: list[Mapping[str, Any]] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            mappings.append(current)
            stack.extend(current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            stack.extend(current)
    return mappings


def _represented_selector_evidence(
    turn_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the evidence that AgentTest exercised the represented selector.

    Operational certification is intended to exercise the user-equivalent
    selector path. Merely requesting that path is insufficient: the persisted
    turn record must prove that the represented prompt, candidate rendering,
    model response, and inherited context lineage were all present, and that
    the persisted final selection still has represented authority rather than
    a later Python fallback or recovery override.
    """

    routing = _mapping(turn_record.get("workflow_routing_diagnostics"))
    selector = _mapping(routing.get("selector"))
    completeness = _mapping(selector.get("telemetry_completeness"))
    workflow_selection = _mapping(turn_record.get("workflow_selection"))
    selector_selection_metadata = _mapping(selector.get("selection_metadata"))
    selector_selected_workflow_id = _text(
        selector_selection_metadata.get("selected_workflow_id")
    )
    selector_prompt_id = _text(selector.get("prompt_id"))
    selector_prompt_provenance = _mapping(selector.get("prompt_provenance"))
    resolved_selector_prompt_id = _text(
        selector_prompt_provenance.get("resolved_prompt_id")
    )
    selector_requested_prompt_ids = {
        _text(item)
        for item in _sequence(selector.get("requested_prompt_ids"))
        if _text(item)
    }
    provenance_requested_prompt_ids = {
        _text(item)
        for item in _sequence(selector_prompt_provenance.get("requested_prompt_ids"))
        if _text(item)
    }
    workflow_selection_id = _text(workflow_selection.get("selected_workflow_id"))
    routing_selection_id = _text(routing.get("selected_workflow_id"))
    workflow_selection_source = _text(workflow_selection.get("selector_source")).lower()
    routing_selector_source = _text(routing.get("selector_source")).lower()
    selector_source = workflow_selection_source or routing_selector_source
    canonical_workflow_selection_source = _canonical_selector_source(
        workflow_selection_source
    )
    canonical_routing_selector_source = _canonical_selector_source(
        routing_selector_source
    )
    workflow_selection_verdict = _text(
        workflow_selection.get("selector_verdict")
    ).lower()
    routing_selector_verdict = _text(routing.get("selector_verdict")).lower()
    selector_verdict = workflow_selection_verdict or routing_selector_verdict
    selection_resolution = _text(selector.get("selection_resolution")).lower()
    decision_attribution = _mapping(turn_record.get("decision_attribution"))
    selection_attribution = next(
        (
            _mapping(item)
            for item in _sequence(decision_attribution.get("decisions"))
            if isinstance(item, Mapping)
            and _text(item.get("decision_kind")).lower() == "selection"
        ),
        {},
    )
    selection_authority = _text(selection_attribution.get("authority")).lower()
    selection_attribution_evidence = _mapping(selection_attribution.get("evidence"))
    attribution_concept_ids = {
        _text(item)
        for item in _sequence(selection_attribution.get("concept_ids"))
        if _text(item)
    }
    attribution_evidence_workflow_id = _text(
        selection_attribution_evidence.get("selected_workflow_id")
    )
    attribution_workflow_ids = {
        workflow_id
        for workflow_id in (
            *attribution_concept_ids,
            attribution_evidence_workflow_id,
        )
        if workflow_id
    }
    final_workflow_ids = {
        workflow_id
        for workflow_id in (workflow_selection_id, routing_selection_id)
        if workflow_id
    }
    selector_source_surfaces_bound = bool(
        workflow_selection_source
        and routing_selector_source
        and canonical_workflow_selection_source == canonical_routing_selector_source
    )
    selector_verdict_surfaces_bound = bool(
        workflow_selection_verdict
        and routing_selector_verdict
        and workflow_selection_verdict == routing_selector_verdict
    )
    selector_source_represented = bool(
        selector_source_surfaces_bound
        and canonical_workflow_selection_source == "workflow_selector"
    )
    selection_resolution_represented = (
        selection_resolution in _REPRESENTED_SELECTOR_RESOLUTIONS
    )
    field_status = {
        "prompt_present": completeness.get("prompt_present") is True,
        "candidate_list_present": completeness.get("candidate_list_present") is True,
        "response_present": completeness.get("response_present") is True,
        "candidate_entries_present": completeness.get("candidate_entries_present")
        is True,
        "context_lineage_present": completeness.get("context_lineage_present") is True,
        "selector_prompt_event_present": int(
            completeness.get("selector_prompt_entry_count") or 0
        )
        >= 1,
        "selector_response_event_present": int(
            completeness.get("selector_response_entry_count") or 0
        )
        >= 1,
        "model_name_present": bool(_text(selector.get("model_name"))),
        "selector_prompt_id_present": bool(selector_prompt_id),
        "selector_prompt_provenance_bound": bool(
            selector_prompt_id and resolved_selector_prompt_id == selector_prompt_id
        ),
        "selector_prompt_requested_id_bound": bool(
            selector_prompt_id
            and selector_prompt_id in selector_requested_prompt_ids
            and selector_prompt_id in provenance_requested_prompt_ids
        ),
        "selector_source_represented": selector_source_represented,
        "selector_source_surfaces_bound": selector_source_surfaces_bound,
        "selector_verdict_present": bool(
            workflow_selection_verdict and routing_selector_verdict
        ),
        "selector_verdict_surfaces_bound": selector_verdict_surfaces_bound,
        "selection_resolution_represented": selection_resolution_represented,
        "selection_attribution_represented": selection_authority == "represented",
        "selector_selected_workflow_id_present": bool(selector_selected_workflow_id),
        "workflow_selection_id_present": bool(workflow_selection_id),
        "routing_selection_id_present": bool(routing_selection_id),
        "final_selection_identity_bound": bool(
            selector_selected_workflow_id
            and workflow_selection_id
            and routing_selection_id
            and final_workflow_ids == {selector_selected_workflow_id}
        ),
        "selection_attribution_identity_bound": bool(
            selector_selected_workflow_id
            and attribution_workflow_ids == {selector_selected_workflow_id}
        ),
        "selection_attribution_concept_identity_bound": bool(
            selector_selected_workflow_id
            and attribution_concept_ids == {selector_selected_workflow_id}
        ),
        "selection_attribution_evidence_identity_bound": bool(
            selector_selected_workflow_id
            and attribution_evidence_workflow_id == selector_selected_workflow_id
        ),
    }
    missing_fields = [field for field, present in field_status.items() if not present]
    return {
        "schema_version": "operational_selector_path_evidence.v1",
        "requested_replay_mode": AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM,
        "selector_prompt_id": selector_prompt_id or None,
        "resolved_selector_prompt_id": resolved_selector_prompt_id or None,
        "selector_source": selector_source or None,
        "workflow_selection_source": workflow_selection_source or None,
        "routing_selector_source": routing_selector_source or None,
        "selector_verdict": selector_verdict or None,
        "selection_resolution": selection_resolution or None,
        "selection_authority": selection_authority or None,
        "selector_selected_workflow_id": selector_selected_workflow_id or None,
        "final_workflow_ids": sorted(final_workflow_ids),
        "selection_attribution_workflow_ids": sorted(attribution_workflow_ids),
        "field_status": field_status,
        "missing_fields": missing_fields,
        "complete": not missing_fields,
    }


def _find_schema_payload(value: Any, schema_version: str) -> dict[str, Any] | None:
    for mapping in _walk_mappings(value):
        if mapping.get("schema_version") == schema_version:
            return dict(mapping)
    return None


_ABSENCE_PROBE_RESOLUTION_LINEAGE_SCHEMA_VERSION = (
    "operational_absence_probe_resolution_lineage.v1"
)


def _resolution_payload_from_action_outputs(
    action_outputs: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Find the structured resolver/read result carried by one action output."""

    candidates: list[Mapping[str, Any]] = []
    for field_name in ("result", "mcp_result", "payload", "data"):
        value = action_outputs.get(field_name)
        if isinstance(value, Mapping):
            candidates.append(value)
    candidates.append(action_outputs)
    for candidate in candidates:
        if _text(candidate.get("status")):
            return dict(candidate)
    return None


def _resolution_target_from_action_inputs(action_inputs: Mapping[str, Any]) -> str:
    for field_name in (
        "name",
        "target_name",
        "target_marker",
        "isolation_id",
        "concept_id",
    ):
        value = _text(action_inputs.get(field_name))
        if value:
            return value
    return ""


def _canonical_absence_probe_resolution_lineage(
    *,
    tool_name: Any,
    target_name: Any,
    result_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Project the exact resolver facts that a represented probe must preserve."""

    tool = _text(tool_name)
    target = _text(target_name)
    status = _text(result_payload.get("status")).lower()
    if not tool or not target or not status:
        return None
    raw_candidates = result_payload.get("candidates")
    candidates = _sequence(raw_candidates) if raw_candidates is not None else []
    return {
        "schema_version": _ABSENCE_PROBE_RESOLUTION_LINEAGE_SCHEMA_VERSION,
        "tool": tool,
        "target_name": target,
        "status": status,
        "resolved_concept_id": _text(result_payload.get("resolved_concept_id")) or None,
        "candidates": json_serialisable_projection(candidates),
    }


def _resolution_lineage_from_action(
    *,
    action_inputs: Mapping[str, Any],
    action_outputs: Mapping[str, Any],
) -> dict[str, Any] | None:
    result_payload = _resolution_payload_from_action_outputs(action_outputs)
    if result_payload is None:
        return None
    return _canonical_absence_probe_resolution_lineage(
        tool_name=(
            action_outputs.get("mcp_resolved_tool")
            or action_outputs.get("mcp_tool")
            or action_inputs.get("tool_name")
            or action_inputs.get("tool")
        ),
        target_name=_resolution_target_from_action_inputs(action_inputs),
        result_payload=result_payload,
    )


def _resolution_lineage_from_probe_result(
    probe_result: Mapping[str, Any],
) -> dict[str, Any] | None:
    for raw_evidence in _sequence(probe_result.get("evidence")):
        if not isinstance(raw_evidence, Mapping):
            continue
        if raw_evidence.get("schema_version") != (
            _ABSENCE_PROBE_RESOLUTION_LINEAGE_SCHEMA_VERSION
        ):
            continue
        lineage = _canonical_absence_probe_resolution_lineage(
            tool_name=raw_evidence.get("tool"),
            target_name=raw_evidence.get("target_name"),
            result_payload=raw_evidence,
        )
        if lineage is not None:
            return lineage
    return None


def _json_pointer_value(value: Any, pointer: Any) -> Any:
    """Resolve a small RFC 6901 pointer without adding policy semantics."""

    pointer_text = _text(pointer)
    if pointer_text == "":
        return value
    if not pointer_text.startswith("/"):
        return None
    current = value
    for raw_token in pointer_text[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return None
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            try:
                index = int(token)
            except ValueError:
                return None
            if index < 0 or index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _observed_tool_names(value: Any) -> list[str]:
    """Return executed tool names from invocation/receipt contexts only."""

    names: list[str] = []
    invocation_container_keys = {
        "invocations",
        "observed_invocations",
        "tool_invocations",
        "tool_observations",
        "execution_receipts",
    }

    def _walk(current: Any, *, invocation_context: bool = False) -> None:
        if isinstance(current, Mapping):
            name = _text(current.get("tool") or current.get("method"))
            has_execution_marker = any(
                key in current
                for key in (
                    "status",
                    "success",
                    "outcome",
                    "started_at",
                    "completed_at",
                    "call_id",
                )
            )
            if (
                name
                and (invocation_context or has_execution_marker)
                and name not in names
            ):
                names.append(name)
            for key, item in current.items():
                _walk(
                    item,
                    invocation_context=(
                        invocation_context or str(key) in invocation_container_keys
                    ),
                )
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            for item in current:
                _walk(item, invocation_context=invocation_context)

    _walk(value)
    return names


def _write_json(path: str, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_artifact_stem(value: Any) -> str:
    """Return one bounded path component for default report artefacts."""

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", _text(value)).strip("._-")
    return stem[:160] or "operational-certification"


def _refresh_campaign_digest(campaign: dict[str, Any]) -> None:
    campaign.pop("report_sha256", None)
    campaign["report_sha256"] = stable_payload_digest(campaign)


def _apply_hard_campaign_gate(
    campaign: dict[str, Any],
    *,
    gate_id: str,
    passed: bool,
    blocker_code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    gate_results = [
        dict(row)
        for row in (campaign.get("certification_gate_results") or [])
        if isinstance(row, Mapping) and row.get("gate_id") != gate_id
    ]
    gate_result = {
        "gate_id": gate_id,
        "kind": "hard_interface",
        "passed": bool(passed),
        "check": json_serialisable_projection(details or {}),
    }
    gate_result["result_sha256"] = stable_payload_digest(gate_result)
    gate_results.append(gate_result)
    campaign["certification_gate_results"] = gate_results
    blockers = [
        dict(row)
        for row in (campaign.get("blockers") or [])
        if isinstance(row, Mapping) and row.get("code") != blocker_code
    ]
    if not passed:
        blockers.append(
            {
                "schema_version": "operational_certification_blocker.v1",
                "code": blocker_code,
                "scope": "campaign.release_eligibility",
                "message": message,
                "recoverable": True,
                "blocking": True,
                "details": json_serialisable_projection(details or {}),
            }
        )
    campaign["blockers"] = blockers
    campaign["failed_certification_gate_ids"] = [
        str(row.get("gate_id") or "")
        for row in gate_results
        if row.get("passed") is not True
    ]
    campaign["certified"] = bool(
        gate_results
        and all(row.get("passed") is True for row in gate_results)
        and not any(row.get("blocking") is True for row in blockers)
    )
    _refresh_campaign_digest(campaign)


def _typed_failure_execution(
    *,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    contract: Any | None = None,
    mode: str = "live",
) -> dict[str, Any]:
    contract_projection = contract.to_projection() if contract is not None else {}
    campaign = {
        "schema_version": "operational_certification_campaign_result.v1",
        "suite_id": contract_projection.get("suite_id"),
        "suite_concept_id": contract_projection.get("suite_concept_id"),
        "case_set": contract_projection.get("case_set"),
        "contract_sha256": contract_projection.get("contract_sha256"),
        "trial_count": 0,
        "pass_windows": [1, 3, 5],
        "topological_scenario_ids": contract_projection.get(
            "topological_scenario_ids",
            [],
        ),
        "scenarios": {},
        "families": {},
        "pass_rates": {"pass^1": 0.0, "pass^3": 0.0, "pass^5": 0.0},
        "certification_gate_results": [],
        "failed_certification_gate_ids": ["certification_preflight_complete"],
        "blockers": [
            {
                "schema_version": "operational_certification_blocker.v1",
                "code": code,
                "scope": "campaign.preflight",
                "message": message,
                "recoverable": True,
                "blocking": True,
                "details": json_serialisable_projection(details or {}),
            }
        ],
        "certified": False,
    }
    _refresh_campaign_digest(campaign)
    execution = {
        "schema_version": "operational_certification_execution.v1",
        "mode": mode,
        "contract": contract_projection,
        "trial_observations": [],
        "trial_results": [],
        "campaign_result": campaign,
        "experiment_persistence": [],
        "release_eligibility": {
            "eligible": False,
            "reason_code": code,
        },
    }
    execution["execution_sha256"] = stable_payload_digest(execution)
    return execution


def _parse_runtime_binding_items(items: Sequence[str] | None) -> dict[str, Any]:
    bindings: dict[str, Any] = {}
    for raw_item in items or ():
        item = str(raw_item or "").strip()
        key, separator, raw_value = item.partition("=")
        key = key.strip()
        if (
            separator != "="
            or not key
            or key in {"trial_index", "isolation_id"}
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
        ):
            raise ValueError(f"operational_runtime_binding_invalid:{item}")
        if key in bindings:
            raise ValueError(f"operational_runtime_binding_duplicate:{key}")
        raw_value = raw_value.strip()
        if not raw_value:
            raise ValueError(f"operational_runtime_binding_value_missing:{key}")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        bindings[key] = value
    return bindings


def _runtime_binding_requirements(contract: Any) -> tuple[str, ...]:
    policy = _mapping(getattr(contract, "policy", {}))
    binding_contract = _mapping(policy.get("runtime_binding_contract"))
    return tuple(
        dict.fromkeys(
            _text(item)
            for item in _sequence(binding_contract.get("required_bindings"))
            if _text(item)
        )
    )


def _unresolved_runtime_placeholders(value: Any) -> tuple[str, ...]:
    placeholders: set[str] = set()

    def _visit(item: Any) -> None:
        if isinstance(item, str):
            placeholders.update(re.findall(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}", item))
            return
        if isinstance(item, Mapping):
            for nested in item.values():
                _visit(nested)
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                _visit(nested)

    _visit(value)
    return tuple(sorted(placeholders))


def _substitute_trial_values(
    value: Any,
    *,
    trial_index: int,
    isolation_id: str,
    runtime_bindings: Mapping[str, Any] | None = None,
) -> Any:
    bindings = {
        "trial_index": trial_index,
        "isolation_id": isolation_id,
        **dict(runtime_bindings or {}),
    }
    if isinstance(value, str):
        exact_match = re.fullmatch(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}", value)
        if exact_match and exact_match.group(1) in bindings:
            return copy.deepcopy(bindings[exact_match.group(1)])

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in bindings:
                return match.group(0)
            replacement = bindings[key]
            if isinstance(replacement, str):
                return replacement
            if replacement is None or isinstance(replacement, (bool, int, float)):
                return json.dumps(replacement, ensure_ascii=True)
            raise ValueError(
                f"operational_runtime_binding_embedded_value_invalid:{key}"
            )

        return re.sub(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}", _replace, value)
    if isinstance(value, Mapping):
        return {
            str(key): _substitute_trial_values(
                item,
                trial_index=trial_index,
                isolation_id=isolation_id,
                runtime_bindings=runtime_bindings,
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _substitute_trial_values(
                item,
                trial_index=trial_index,
                isolation_id=isolation_id,
                runtime_bindings=runtime_bindings,
            )
            for item in value
        ]
    return value


def _canonical_tool_catalogue_digest() -> str:
    from src.backend.integrations.internal_mcp.tool_contract_registry import (
        get_canonical_tool_registry,
    )

    registry = get_canonical_tool_registry()
    projection = [
        {
            "name": contract.name,
            "family": contract.family,
            "category": contract.category,
            "input_schema": contract.input_schema,
            "output_schema": contract.output_schema,
        }
        for _name, contract in sorted(registry.items())
    ]
    return stable_payload_digest(projection)


def _execute_represented_workflow_synchronously(
    *,
    workflow_id: str,
    inputs: Mapping[str, Any],
    namespace: str,
    user_id: str,
    org_id: str,
    source_event_type: str,
    source_event_id: str,
    event_idempotency_key: str,
    execution_metadata: Mapping[str, Any] | None = None,
    timeout_seconds: float = 600.0,
    require_policy_identity: bool = True,
    requested_model: str | None = None,
) -> dict[str, Any]:
    """Run one live-Vontology workflow without relying on a durable worker.

    AgentTest deliberately does not start background durable workers.  The
    certification evaluator is nevertheless an ordinary represented workflow,
    so the canonical ``WorkflowExecutor`` can execute it synchronously while
    the workflow and prompt remain authoritative in Vontology.  This helper is
    transport only: it resolves the represented definition, supplies the exact
    authenticated scope, and persists a workflow trace.  It does not interpret
    or amend the workflow's result.
    """

    from src.backend.languagemodels.llm_interface import (
        get_active_model_name,
        get_llm_client,
        resolve_provider_from_model_concept,
    )
    from src.backend.security.access_control import override_current_actor
    from src.backend.workflows.action_registry import WorkflowEnvironment
    from src.backend.workflows.durable.registry_factory import (
        _get_or_build_durable_mcp_gateway,
        build_vontology_workflow_registry_snapshot,
        get_shared_durable_action_registry,
        resolve_workflow_definition_from_authority,
    )
    from src.backend.workflows.durable.durable_executor import (
        _resolve_instance_runtime_model_context,
    )
    from src.backend.workflows.engine import WorkflowExecutor
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.trace_store import (
        get_workflow_execution_trace,
        insert_workflow_execution_trace,
    )

    workflow_id_text = _text(workflow_id)
    namespace_text = _text(namespace)
    user_id_text = _text(user_id)
    org_id_text = _text(org_id)
    exact_scope = {
        "namespace": namespace_text or None,
        "user_id": user_id_text or None,
        "org_id": org_id_text or None,
        "source_event_type": _text(source_event_type) or None,
        "source_event_id": _text(source_event_id) or None,
        "event_idempotency_key": _text(event_idempotency_key) or None,
    }
    response: dict[str, Any] = {
        "schema_version": "synchronous_represented_workflow_execution.v1",
        "success": False,
        "transport": "synchronous_workflow_executor",
        "workflow_id": workflow_id_text or None,
        "exact_scope": exact_scope,
        "exact_scope_sha256": stable_payload_digest(exact_scope),
        "policy_identity_required": require_policy_identity,
    }
    missing_scope_fields = [
        key
        for key, value in (
            ("workflow_id", workflow_id_text),
            ("namespace", namespace_text),
            ("user_id", user_id_text),
            ("org_id", org_id_text),
            ("source_event_type", _text(source_event_type)),
            ("source_event_id", _text(source_event_id)),
            ("event_idempotency_key", _text(event_idempotency_key)),
        )
        if not value
    ]
    if missing_scope_fields:
        response["error_code"] = "synchronous_workflow_scope_incomplete"
        response["missing_scope_fields"] = missing_scope_fields
        return response

    projected_inputs = json_serialisable_projection(inputs)
    projected_metadata = json_serialisable_projection(execution_metadata or {})
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        response["error_code"] = "synchronous_workflow_timeout_invalid"
        return response
    effective_timeout_seconds = max(1.0, min(float(timeout_seconds), 600.0))
    response["workflow_inputs_sha256"] = stable_payload_digest(projected_inputs)
    response["timeout_seconds"] = effective_timeout_seconds
    trace = WorkflowExecutionTrace(
        workflow_id=workflow_id_text,
        execution_id=str(uuid.uuid4()),
        user_namespace=namespace_text,
        org_id=org_id_text,
        metadata={
            "schema_version": "operational_certification_workflow_trace.v1",
            "execution_transport": "synchronous_workflow_executor",
            "exact_scope": exact_scope,
            "exact_scope_sha256": response["exact_scope_sha256"],
            "workflow_inputs_sha256": response["workflow_inputs_sha256"],
            "timeout_seconds": effective_timeout_seconds,
            "execution_metadata": projected_metadata,
        },
    )
    result: Any | None = None
    resolution: Any | None = None
    error_code: str | None = None
    error_detail: str | None = None

    try:
        # Use a dedicated live-Vontology registry rather than the AgentTest
        # repo-seed registry.  Certification is release-ineligible unless the
        # evaluator definition actually came from the live authority surface.
        authority_registry = build_vontology_workflow_registry_snapshot(
            workflow_ids=[workflow_id_text]
        )
        resolution = resolve_workflow_definition_from_authority(
            workflow_id_text,
            registry=authority_registry,
            use_current_shared_registry=False,
            register_authoritative_fallback=True,
            actor_user_id=user_id_text,
            actor_org_id=org_id_text,
        )
        response["authority_resolution"] = resolution.to_dict()
        definition = resolution.definition
        registration_source = _text(resolution.registration_source).lower()
        response["workflow_authority_source"] = registration_source or None
        if definition is None:
            error_code = "represented_workflow_definition_unavailable"
            error_detail = _text(resolution.error_code) or None
        elif registration_source != "vontology":
            error_code = "represented_workflow_not_live_vontology_authority"
            error_detail = registration_source or "unknown"
        else:
            definition_identity = (
                dict(resolution.definition_identity)
                if isinstance(resolution.definition_identity, Mapping)
                else {}
            )
            response["workflow_definition_identity"] = definition_identity
            if definition_identity:
                response["workflow_definition_identity_sha256"] = stable_payload_digest(
                    definition_identity
                )
            trace.metadata["workflow_authority_source"] = registration_source
            trace.metadata["workflow_definition_identity"] = definition_identity

            workflow_data = dict(projected_inputs)
            # Authenticated scope is a hard transport invariant.  Caller inputs
            # cannot substitute a different actor or namespace.
            workflow_data["namespace"] = namespace_text
            workflow_data["user_namespace"] = namespace_text
            workflow_data["user_id"] = user_id_text
            workflow_data["org_id"] = org_id_text
            workflow_data["user_concept_id"] = user_id_text
            workflow_data["org_concept_id"] = org_id_text
            workflow_data["source_event_type"] = _text(source_event_type)
            workflow_data["source_event_id"] = _text(source_event_id)
            workflow_data["event_idempotency_key"] = _text(event_idempotency_key)
            workflow_data["conversation_turn_llm_timeout_override_sec"] = (
                effective_timeout_seconds
            )

            with override_current_actor(user_id_text, org_id_text):
                (
                    resolved_requested_model,
                    requested_client_type,
                    requested_model_parameters,
                ) = _resolve_instance_runtime_model_context(
                    context={"requested_model": _text(requested_model)},
                    inputs=workflow_data,
                )
                effective_model = resolved_requested_model or get_active_model_name(
                    user_concept_id=user_id_text,
                    org_concept_id=org_id_text,
                )
                if resolved_requested_model:
                    workflow_data["requested_model"] = resolved_requested_model
                    trace.metadata["requested_model"] = resolved_requested_model
                    trace.metadata["requested_model_override_applied"] = True
                if requested_client_type:
                    workflow_data["requested_client_type"] = requested_client_type
                    trace.metadata["requested_client_type"] = requested_client_type
                if requested_model_parameters:
                    workflow_data["requested_model_parameters"] = dict(
                        requested_model_parameters
                    )
                    trace.metadata["requested_model_parameters"] = dict(
                        requested_model_parameters
                    )
                llm_client = get_llm_client(
                    client_type=requested_client_type,
                    user_concept_id=user_id_text,
                    org_concept_id=org_id_text,
                )
                environment = WorkflowEnvironment(
                    llm_client=llm_client,
                    gateway=_get_or_build_durable_mcp_gateway(),
                    model=effective_model,
                    model_parameters=requested_model_parameters or None,
                    user_namespace=namespace_text,
                    user_concept_id=user_id_text,
                    org_concept_id=org_id_text,
                )
                if isinstance(effective_model, str) and effective_model.strip():
                    model_text = effective_model.strip()
                    trace.metadata["default_model"] = model_text
                    provider = resolve_provider_from_model_concept(model_text)
                    if provider:
                        trace.metadata["default_provider"] = provider
                result = WorkflowExecutor(
                    registry=get_shared_durable_action_registry(),
                    max_transitions=50,
                ).run(
                    definition,
                    environment=environment,
                    data=workflow_data,
                    trace=trace,
                )

            llm_step_envelope = _mapping(result.data.get("llm_step_envelope"))
            selected_prompt_id = _text(
                llm_step_envelope.get("selected_prompt_id")
                or llm_step_envelope.get("base_prompt_id")
            )
            selected_model = _text(llm_step_envelope.get("selected_model"))
            if (
                result.completed is True
                and not result.error
                and require_policy_identity
            ):
                if not selected_prompt_id or not selected_model:
                    error_code = "represented_evaluator_policy_identity_missing"
                else:
                    from src.backend.services.prompt_template_service import (
                        PromptTemplateService,
                    )

                    with override_current_actor(user_id_text, org_id_text):
                        (
                            resolved_prompt_id,
                            prompt_text,
                        ) = PromptTemplateService().resolve_prompt_text(
                            [selected_prompt_id],
                            max_chars=200_000,
                        )
                    if resolved_prompt_id != selected_prompt_id or not prompt_text:
                        error_code = (
                            "represented_evaluator_prompt_authority_unavailable"
                        )
                    else:
                        evaluator_policy_identity = {
                            "schema_version": "represented_evaluator_policy_identity.v1",
                            "prompt_concept_id": selected_prompt_id,
                            "prompt_content_sha256": stable_payload_digest(prompt_text),
                            "selected_model": selected_model,
                            "selected_provider": (
                                resolve_provider_from_model_concept(selected_model)
                            ),
                        }
                        evaluator_policy_identity["identity_sha256"] = (
                            stable_payload_digest(evaluator_policy_identity)
                        )
                        response["evaluator_policy_identity"] = (
                            evaluator_policy_identity
                        )
                        trace.metadata["evaluator_policy_identity"] = (
                            evaluator_policy_identity
                        )
            response["workflow_completed"] = bool(result.completed)
            response["final_state"] = _text(result.final_state) or None
            response["workflow_error"] = _text(result.error) or None
            response["workflow_output"] = json_serialisable_projection(result.data)
            response["workflow_result_envelope"] = json_serialisable_projection(
                result.result_envelope or {}
            )
            response["workflow_output_sha256"] = stable_payload_digest(
                response["workflow_output"]
            )
            workflow_action_evidence: list[dict[str, Any]] = []
            for raw_action in trace.actions:
                if not isinstance(raw_action, Mapping):
                    continue
                action_inputs = _mapping(raw_action.get("inputs"))
                action_outputs = _mapping(raw_action.get("outputs"))
                state_probe_result = _find_schema_payload(
                    action_outputs,
                    "represented_operational_state_probe_result.v1",
                )
                resolution_lineage = _resolution_lineage_from_action(
                    action_inputs=action_inputs,
                    action_outputs=action_outputs,
                )
                evidence_row = {
                    "action_id": _text(raw_action.get("action_id")) or None,
                    "status": _text(raw_action.get("status")) or None,
                    "call_id": _text(raw_action.get("call_id")) or None,
                    "inputs_sha256": stable_payload_digest(action_inputs),
                    "outputs_sha256": stable_payload_digest(action_outputs),
                    "state_probe_result_sha256": (
                        stable_payload_digest(state_probe_result)
                        if state_probe_result
                        else None
                    ),
                    "resolution_lineage_sha256": (
                        stable_payload_digest(resolution_lineage)
                        if resolution_lineage
                        else None
                    ),
                }
                workflow_action_evidence.append(evidence_row)
            response["workflow_action_evidence"] = workflow_action_evidence
            if result.completed is not True or result.error:
                error_code = "represented_workflow_execution_failed"
                error_detail = _text(result.error) or _text(result.final_state)
    except Exception as exc:
        error_code = "synchronous_represented_workflow_execution_exception"
        error_detail = f"{type(exc).__name__}:{exc}"
        if trace.status == "running":
            trace.finish_failed(error_code)

    if trace.status == "running":
        trace.finish_failed(error_code or "synchronous_workflow_terminal_state_missing")
    trace_payload_sha256: str | None = None
    try:
        trace_storage_document = _bounded_safe_evidence(trace.to_storage_document())
        if not isinstance(trace_storage_document, dict):
            raise TypeError("workflow_trace_safe_projection_invalid")
        trace_payload_sha256 = stable_payload_digest(trace_storage_document)
        trace_storage_document["certification_trace_payload_sha256"] = (
            trace_payload_sha256
        )
        persisted_trace_id = insert_workflow_execution_trace(trace_storage_document)
    except Exception as exc:
        persisted_trace_id = None
        response["trace_persistence_error"] = f"{type(exc).__name__}:{exc}"
        if error_code is None:
            error_code = "workflow_trace_persistence_exception"
    trace_readback_verified = False
    canonical_trace_readback_sha256: str | None = None
    if persisted_trace_id == trace.execution_id:
        canonical_trace = get_workflow_execution_trace(trace.execution_id)
        if isinstance(canonical_trace, Mapping):
            canonical_trace_projection = dict(canonical_trace)
            observed_trace_digest = canonical_trace_projection.pop(
                "certification_trace_payload_sha256",
                None,
            )

            def _normalise_trace_value(value: Any) -> Any:
                if isinstance(value, datetime):
                    return value.isoformat()
                if isinstance(value, Mapping):
                    return {
                        str(key): _normalise_trace_value(item)
                        for key, item in value.items()
                    }
                if isinstance(value, Sequence) and not isinstance(
                    value, (str, bytes, bytearray)
                ):
                    return [_normalise_trace_value(item) for item in value]
                return value

            canonical_trace_projection = _normalise_trace_value(
                canonical_trace_projection
            )
            canonical_trace_readback_sha256 = stable_payload_digest(
                canonical_trace_projection
            )
            trace_readback_verified = bool(
                trace_payload_sha256 is not None
                and observed_trace_digest == trace_payload_sha256
                and canonical_trace_readback_sha256 == trace_payload_sha256
                and canonical_trace_projection.get("execution_id") == trace.execution_id
                and canonical_trace_projection.get("workflow_id") == workflow_id_text
            )
    trace_persisted = bool(
        persisted_trace_id == trace.execution_id and trace_readback_verified
    )
    response["trace_persisted"] = trace_persisted
    response["trace_readback_verified"] = trace_readback_verified
    response["trace_document_sha256"] = (
        trace_payload_sha256 if trace_readback_verified else None
    )
    response["canonical_trace_readback_sha256"] = canonical_trace_readback_sha256
    response["execution_trace_id"] = persisted_trace_id if trace_persisted else None
    response["attempted_execution_trace_id"] = trace.execution_id
    if not trace_persisted and error_code is None:
        error_code = (
            "workflow_trace_readback_failed"
            if persisted_trace_id == trace.execution_id
            else "workflow_trace_persistence_failed"
        )

    response["error_code"] = error_code
    if error_detail:
        response["error_detail"] = error_detail
    response["success"] = bool(
        error_code is None
        and trace_persisted
        and result is not None
        and result.completed is True
        and not result.error
    )
    response["execution_sha256"] = stable_payload_digest(
        {key: value for key, value in response.items() if key != "execution_sha256"}
    )
    return response


def _represented_evaluator_result_from_execution(
    execution: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Extract only the evaluator workflow's validated terminal output."""

    if (
        execution.get("success") is not True
        or execution.get("trace_persisted") is not True
        or _text(execution.get("workflow_authority_source")).lower() != "vontology"
        or not _mapping(execution.get("evaluator_policy_identity"))
        or not _text(execution.get("trace_document_sha256"))
    ):
        return None
    workflow_output = _mapping(execution.get("workflow_output"))
    candidate = workflow_output.get("represented_operational_evaluator_result")
    if not isinstance(candidate, Mapping) or candidate.get("schema_version") != (
        "represented_operational_evaluator_result.v1"
    ):
        return None
    result = dict(candidate)
    result["execution_evidence"] = {
        "schema_version": "represented_evaluator_execution_evidence.v1",
        "transport": execution.get("transport"),
        "execution_trace_id": execution.get("execution_trace_id"),
        "workflow_definition_identity_sha256": execution.get(
            "workflow_definition_identity_sha256"
        ),
        "workflow_inputs_sha256": execution.get("workflow_inputs_sha256"),
        "workflow_output_sha256": execution.get("workflow_output_sha256"),
        "trace_document_sha256": execution.get("trace_document_sha256"),
        "evaluator_policy_identity": _mapping(
            execution.get("evaluator_policy_identity")
        ),
        "exact_scope_sha256": execution.get("exact_scope_sha256")
        or stable_payload_digest(_mapping(execution.get("exact_scope"))),
    }
    return result


def _bounded_safe_evidence(value: Any, *, key_name: str = "") -> Any:
    lowered = key_name.lower()
    sensitive_markers = (
        "password",
        "secret",
        "token",
        "authorization",
        "cookie",
        "api_key",
        "credential",
    )
    private_content_keys = {
        "body",
        "body_text",
        "body_html",
        "context_messages",
        "final_response",
        "message_body",
        "messages",
        "prompt",
        "prompt_text",
        "raw",
        "raw_response",
        "rendered_prompt",
        "response_text",
        "rationale",
        "reason",
        "snippet",
        "visible_answer",
        "visible_answers",
    }
    safe_string_keys = {
        "action_id",
        "adapter_id",
        "base_url",
        "classification",
        "code",
        "current_status",
        "decision",
        "effect_type",
        "error_code",
        "error_type",
        "event_idempotency_key",
        "field",
        "final_state",
        "final_status",
        "gate_id",
        "git_branch",
        "git_commit",
        "kind",
        "matcher_id",
        "method_name",
        "mode",
        "model",
        "namespace",
        "observation_type",
        "operator",
        "outcome",
        "path",
        "provider",
        "ref",
        "reason_code",
        "sanitized_uri",
        "schema_version",
        "scope",
        "source",
        "source_event_type",
        "status",
        "terminal_state",
        "tool_name",
        "transport",
        "user_namespace",
        "verdict",
        "version",
        "workflow_authority_source",
    }
    if any(marker in lowered for marker in sensitive_markers):
        return {"redacted": True, "value_present": value not in (None, "", [], {})}
    if lowered in private_content_keys and value not in (None, "", [], {}):
        redaction: dict[str, Any] = {
            "redacted": True,
            "sha256": stable_payload_digest(value),
        }
        if isinstance(value, (str, Sequence, Mapping)):
            redaction["length"] = len(value)
        return redaction
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_safe_evidence(item, key_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        return [
            _bounded_safe_evidence(item, key_name=key_name) for item in items[:200]
        ] + ([{"truncated_item_count": len(items) - 200}] if len(items) > 200 else [])
    if isinstance(value, str):
        safe_identifier_key = bool(
            lowered in safe_string_keys
            or lowered.endswith("_id")
            or lowered.endswith("_ids")
            or lowered.endswith("_sha256")
            or lowered.endswith("_digest")
            or lowered.endswith("_code")
            or lowered.endswith("_status")
        )
        if safe_identifier_key and len(value) <= 2000:
            return value
        return {
            "redacted": True,
            "length": len(value),
            "sha256": stable_payload_digest(value),
        }
    return value


def _artifact_safe_execution_projection(
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a bounded local-report projection without pilot/private content."""

    source_execution_sha256 = _text(execution.get("execution_sha256")) or (
        stable_payload_digest(execution)
    )
    projected = _bounded_safe_evidence(execution)
    if not isinstance(projected, Mapping):
        raise TypeError("operational_certification_artifact_projection_invalid")
    artifact = dict(projected)
    artifact["artifact_projection"] = {
        "schema_version": "operational_certification_artifact_projection.v1",
        "private_content_redacted": True,
        "source_execution_sha256": source_execution_sha256,
    }
    artifact["artifact_sha256"] = stable_payload_digest(artifact)
    return artifact


def _collect_runtime_authority_alignment(
    *,
    session: requests.Session,
    base_url: str,
    environment: Mapping[str, Any],
    allow_non_agent_test_server: bool = False,
    diagnostics_timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    """Prove that HTTP trials and local authority writes share one runtime.

    The live adapter sends user turns to an HTTP server while certification
    authority, evaluator execution, and experiment persistence run in this
    process.  A campaign is meaningful only when those two halves are the same
    code revision and the same safely identified Mongo authority location.
    """

    result: dict[str, Any] = {
        "schema_version": "operational_certification_runtime_alignment.v1",
        "verified": False,
    }
    try:
        parsed = urlparse(_text(base_url))
        hostname = (parsed.hostname or "").lower()
        loopback_target = hostname in {"127.0.0.1", "localhost", "::1"}
        response = session.get(
            f"{_text(base_url).rstrip('/')}/diag",
            timeout=diagnostics_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("server_diagnostics_mapping_required")

        from src.backend.db.mongo_client import (
            get_configured_database_name,
            get_effective_mongo_uri,
            is_using_fallback_uri,
        )
        from src.backend.db.mongo_uri_redaction import (
            build_safe_mongo_connection_location,
        )

        server_mongo = _mapping(payload.get("mongo"))
        server_location = _mapping(server_mongo.get("effective_mongo_location"))
        local_location = build_safe_mongo_connection_location(
            get_effective_mongo_uri(),
            using_fallback=is_using_fallback_uri(),
        )
        server_location_sha256 = (
            stable_payload_digest(server_location) if server_location else None
        )
        local_location_sha256 = (
            stable_payload_digest(local_location) if local_location else None
        )
        server_database_name_sha256 = _text(
            server_mongo.get("effective_database_name_sha256")
        )
        local_database_name_sha256 = hashlib.sha256(
            get_configured_database_name().encode("utf-8")
        ).hexdigest()
        version_details = _mapping(payload.get("version_details"))
        server_git_commit = _text(
            version_details.get("git_commit") or environment.get("server_git_commit")
        )
        local_git_commit = _text(environment.get("local_repo_git_head"))
        server_git_dirty = version_details.get("git_dirty")
        checks = {
            "loopback_http_target": loopback_target,
            "approved_server_mode": (
                environment.get("server_agent_test_instance") is True
                or allow_non_agent_test_server
            ),
            "exact_git_commit": bool(
                server_git_commit
                and local_git_commit
                and server_git_commit == local_git_commit
            ),
            "clean_server_build": server_git_dirty is False,
            "clean_local_build": environment.get("local_repo_git_dirty") is False,
            "exact_mongo_authority_location": bool(
                server_location_sha256
                and local_location_sha256
                and server_location_sha256 == local_location_sha256
            ),
            "exact_mongo_database": bool(
                server_database_name_sha256
                and server_database_name_sha256 == local_database_name_sha256
            ),
        }
        result.update(
            {
                "verified": all(checks.values()),
                "checks": checks,
                "server_git_commit": server_git_commit or None,
                "local_git_commit": local_git_commit or None,
                "server_git_dirty": server_git_dirty,
                "server_mongo_location": server_location or None,
                "local_mongo_location": local_location or None,
                "server_mongo_location_sha256": server_location_sha256,
                "local_mongo_location_sha256": local_location_sha256,
                "server_database_name_sha256": (server_database_name_sha256 or None),
                "local_database_name_sha256": local_database_name_sha256,
            }
        )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    result["alignment_sha256"] = stable_payload_digest(result)
    return result


def _validate_canonical_certification_observations(
    *,
    observations: Sequence[Any],
    trial_results: Sequence[Any],
    campaign: Mapping[str, Any],
    experiment_run_id: str,
    campaign_execution_id: str,
    namespace: str,
    user_id: str,
    org_id: str,
    execution_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact canonical trial/campaign rows after experiment readback."""

    rows = [_mapping(row) for row in observations if isinstance(row, Mapping)]
    trial_rows = [
        row
        for row in rows
        if row.get("observation_type") == "operational_certification_trial"
    ]
    campaign_rows = [
        row
        for row in rows
        if row.get("observation_type") == "operational_certification_campaign"
    ]
    expected_trials: dict[tuple[str, int], dict[str, Any]] = {}
    for raw_result in trial_results:
        result = _mapping(raw_result)
        scenario_id = _text(result.get("scenario_id"))
        trial_index = result.get("trial_index")
        if (
            scenario_id
            and isinstance(trial_index, int)
            and not isinstance(trial_index, bool)
        ):
            expected_trials[(scenario_id, trial_index)] = result

    observed_trials: dict[tuple[str, int], dict[str, Any]] = {}
    trial_binding_errors: list[dict[str, Any]] = []
    for row in trial_rows:
        outcome = _mapping(row.get("observed_outcome"))
        scenario_id = _text(outcome.get("scenario_id"))
        trial_index = outcome.get("trial_index")
        key = (
            scenario_id,
            (
                trial_index
                if isinstance(trial_index, int) and not isinstance(trial_index, bool)
                else -1
            ),
        )
        if key in observed_trials:
            trial_binding_errors.append(
                {"code": "duplicate_trial_observation", "key": list(key)}
            )
            continue
        observed_trials[key] = row
        expected = expected_trials.get(key)
        stored_result = _mapping(
            _mapping(row.get("evidence")).get("operational_certification_trial_result")
        )
        if (
            row.get("schema_version")
            != "strict_certification_experiment_observation.v1"
            or expected is None
            or outcome.get("result_sha256") != expected.get("result_sha256")
            or outcome.get("passed") is not expected.get("passed")
            or stored_result.get("result_sha256") != expected.get("result_sha256")
            or stored_result.get("scenario_id") != scenario_id
            or stored_result.get("trial_index") != trial_index
        ):
            trial_binding_errors.append(
                {"code": "trial_observation_binding_mismatch", "key": list(key)}
            )

    campaign_validation_errors: list[dict[str, Any]] = []
    attestation_verified = False
    attestation_exact = False
    if len(campaign_rows) == 1:
        campaign_row = campaign_rows[0]
        campaign_validation_errors.extend(
            validate_campaign_experiment_observation(campaign_row)
        )
        stored_campaign = _mapping(
            _mapping(campaign_row.get("evidence")).get(
                "operational_certification_campaign_result"
            )
        )
        if (
            stored_campaign.get("report_sha256") != campaign.get("report_sha256")
            or stored_campaign.get("contract_sha256") != campaign.get("contract_sha256")
            or stored_campaign.get("certified") is not campaign.get("certified")
        ):
            campaign_validation_errors.append(
                {"code": "canonical_campaign_report_binding_mismatch"}
            )
        canonical_provenance = _mapping(campaign_row.get("execution_provenance"))
        canonical_attestation = _mapping(
            canonical_provenance.get("trusted_runner_attestation")
        )
        expected_attestation = _mapping(
            execution_provenance.get("trusted_runner_attestation")
        )
        attestation_exact = bool(
            canonical_attestation
            and canonical_attestation == expected_attestation
            and canonical_provenance.get("trusted_runner_attestation_status")
            == "signed"
        )
        if attestation_exact:
            try:
                verify_operational_certification_runner_attestation(
                    canonical_attestation,
                    expected={
                        "experiment_run_id": experiment_run_id,
                        "campaign_execution_id": campaign_execution_id,
                        "namespace": namespace,
                        "user_id": user_id,
                        "org_id": org_id,
                        "report_sha256": campaign.get("report_sha256"),
                        "contract_sha256": campaign.get("contract_sha256"),
                    },
                )
            except Exception as exc:
                campaign_validation_errors.append(
                    {
                        "code": "canonical_campaign_attestation_invalid",
                        "error_type": type(exc).__name__,
                    }
                )
            else:
                attestation_verified = True
        else:
            campaign_validation_errors.append(
                {"code": "canonical_campaign_attestation_mismatch"}
            )
    else:
        campaign_validation_errors.append(
            {
                "code": "canonical_campaign_observation_count_invalid",
                "observed_count": len(campaign_rows),
            }
        )

    checks = {
        "row_count_exact": len(rows) == len(expected_trials) + 1,
        "trial_key_set_exact": set(observed_trials) == set(expected_trials),
        "trial_bindings_exact": not trial_binding_errors,
        "campaign_observation_exact": not campaign_validation_errors,
        "trusted_runner_attestation_exact": attestation_exact,
        "trusted_runner_attestation_verified": attestation_verified,
    }
    return {
        "success": all(checks.values()),
        "checks": checks,
        "trial_binding_errors": trial_binding_errors,
        "campaign_validation_errors": campaign_validation_errors,
        "observed_trial_keys": [list(key) for key in sorted(observed_trials)],
        "expected_trial_keys": [list(key) for key in sorted(expected_trials)],
    }


def _turn_record_evidence_projection(turn_record: Mapping[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "schema_version",
        "request_id",
        "session_id",
        "namespace",
        "created_at_utc",
        "workflow_selection",
        "selected_workflow_handoff",
        "completion_gate",
        "terminal_outcome_receipt",
        "required_effects",
        "tool_observation_ledger",
        "critic",
        "final_response",
        "turn_execution_record_locator",
        "execution_trace_id",
        "workflow_instance_ids",
    }
    return _bounded_safe_evidence(
        {key: value for key, value in turn_record.items() if key in allowed_keys}
    )


def _task_evidence_projection(task_evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Keep evaluator task evidence bounded to terminal status and locators.

    The background-task status can embed the complete result and LLM debug
    record. Passing that recursively into the represented evaluator duplicates
    the separately projected turn record and can exceed local model context.
    """

    last_status = _mapping(task_evidence.get("last_task_status"))
    allowed_status_keys = {
        "status",
        "current_status",
        "final_status",
        "state",
        "phase",
        "task_id",
        "request_id",
        "error_code",
        "timed_out",
        "completed",
        "terminal",
    }
    last_status_projection = {
        key: value for key, value in last_status.items() if key in allowed_status_keys
    }
    return _bounded_safe_evidence(
        {
            "last_task_status": last_status_projection,
            "last_task_status_sha256": (
                stable_payload_digest(last_status) if last_status else None
            ),
            "timed_out": task_evidence.get("timed_out"),
            "task_status_count": len(_sequence(task_evidence.get("task_statuses"))),
            "progress_snapshot_count": len(
                _sequence(task_evidence.get("progress_snapshots"))
            ),
        }
    )


def _render_markdown(execution: Mapping[str, Any]) -> str:
    campaign = _mapping(execution.get("campaign_result"))
    lines = [
        "# Operational certification campaign",
        "",
        f"- Suite: `{campaign.get('suite_concept_id') or campaign.get('suite_id')}`",
        f"- Certified: **{'yes' if campaign.get('certified') is True else 'no'}**",
        f"- Evidence mode: `{execution.get('mode') or 'live'}`",
        f"- Release eligible: **{'yes' if _mapping(execution.get('release_eligibility')).get('eligible') is True else 'no'}**",
        f"- Contract: `{campaign.get('contract_sha256') or ''}`",
        f"- Report: `{campaign.get('report_sha256') or ''}`",
        "",
        "| Scenario | Family | pass^1 | pass^3 | pass^5 | Passed trials |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    scenarios = _mapping(campaign.get("scenarios"))
    for scenario_id in campaign.get("topological_scenario_ids") or scenarios:
        row = _mapping(scenarios.get(scenario_id))
        windows = _mapping(row.get("pass_windows"))
        lines.append(
            "| "
            + " | ".join(
                [
                    str(scenario_id),
                    _text(row.get("family_id")),
                    "yes" if windows.get("pass^1") is True else "no",
                    "yes" if windows.get("pass^3") is True else "no",
                    "yes" if windows.get("pass^5") is True else "no",
                    str(row.get("successful_trial_count") or 0),
                ]
            )
            + " |"
        )
    failed_gates = campaign.get("failed_certification_gate_ids") or []
    blockers = campaign.get("blockers") or []
    evidence = _mapping(campaign.get("represented_campaign_evidence"))
    operational_metrics = _mapping(campaign.get("operational_metrics"))
    latency = _mapping(operational_metrics.get("latency_ms"))
    lines.extend(
        [
            "",
            f"Failed gates: {', '.join(map(str, failed_gates)) or 'none'}",
            f"Campaign blockers: {len(blockers)}",
            f"Typed non-success outcomes: {evidence.get('typed_non_success_outcome_rate')}",
            f"Causal-stage evidence: {evidence.get('causal_stage_evidence_rate')}",
            f"Recoverable-fault recovery: {evidence.get('recoverable_fault_recovery_rate')}",
            f"Latency p50/p95 ms: {latency.get('p50')} / {latency.get('p95')}",
            f"Timeouts: {latency.get('timeout_count')}",
            f"Experiment persistence complete: {campaign.get('experiment_persistence_complete')}",
        ]
    )
    return "\n".join(lines) + "\n"


def _offline_execution(contract: Any, path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_observations = (
        payload.get("trial_observations") if isinstance(payload, Mapping) else payload
    )
    observations = [
        dict(item) for item in _sequence(raw_observations) if isinstance(item, Mapping)
    ]
    results = [
        evaluate_scenario_trial(
            contract,
            scenario_id=_text(observation.get("scenario_id")),
            observation=observation,
        )
        for observation in observations
    ]
    represented_campaign_evidence = (
        _mapping(payload.get("represented_campaign_evidence"))
        if isinstance(payload, Mapping)
        else {}
    )
    campaign = aggregate_five_trial_campaign(
        contract,
        results,
        represented_campaign_evidence=represented_campaign_evidence,
    )
    _apply_hard_campaign_gate(
        campaign,
        gate_id="live_release_evidence_eligible",
        passed=False,
        blocker_code="offline_evidence_not_release_eligible",
        message=(
            "Offline evidence evaluation is diagnostic only and cannot certify "
            "the live trusted-SAIL path."
        ),
        details={"mode": "offline_evidence_evaluation"},
    )
    execution = {
        "schema_version": "operational_certification_execution.v1",
        "mode": "offline_evidence_evaluation",
        "contract": contract.to_projection(),
        "trial_observations": observations,
        "trial_results": results,
        "campaign_result": campaign,
        "experiment_persistence": [],
        "release_eligibility": {
            "eligible": False,
            "reason_code": "offline_evidence_not_release_eligible",
        },
    }
    execution["execution_sha256"] = stable_payload_digest(execution)
    return execution


def _aggregate_authenticated_multi_turn_results(
    turn_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project sequential authenticated turns into one scenario observation.

    Turn semantics remain represented in the scenario's ordered ``turns``
    inputs.  This helper only preserves each turn's evidence and computes
    mechanical totals/unions for the existing certification evaluator.
    """

    results = [dict(item) for item in turn_results if isinstance(item, Mapping)]
    if not results:
        raise ValueError("represented_multi_turn_results_missing")

    def _ordered_strings(key: str) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for result in results:
            values = result.get(key)
            for value in _sequence(values):
                text = _text(value)
                if text and text not in seen:
                    seen.add(text)
                    output.append(text)
        return output

    def _ordered_path_strings(key: str) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for result in results:
            path_analysis = _mapping(result.get("path_analysis"))
            for value in _sequence(path_analysis.get(key)):
                text = _text(value)
                if text and text not in seen:
                    seen.add(text)
                    output.append(text)
        return output

    def _sum_numeric_metric(key: str) -> tuple[int | float | None, bool]:
        values = [
            _mapping(result.get("operational_metrics")).get(key) for result in results
        ]
        numeric = [
            value
            for value in values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        complete = len(numeric) == len(results)
        return (sum(numeric) if complete else None), complete

    last = results[-1]
    false_success_claims = [
        {**dict(item), "turn_index": turn_index}
        for turn_index, result in enumerate(results, start=1)
        for item in _sequence(result.get("false_success_claims"))
        if isinstance(item, Mapping)
    ]
    path_turns = [
        {
            "turn_index": turn_index,
            "terminal_state": result.get("terminal_state"),
            "visible_answer": result.get("visible_answer"),
            "path_analysis": _mapping(result.get("path_analysis")),
            "turn_execution_request_ids": _sequence(
                result.get("turn_execution_request_ids")
            ),
        }
        for turn_index, result in enumerate(results, start=1)
    ]
    request_ids = [
        _text(request_id)
        for result in results
        for request_id in _sequence(result.get("turn_execution_request_ids"))
        if _text(request_id)
    ]
    task_ids = [
        _text(_mapping(result.get("submission")).get("task_id")) for result in results
    ]
    for field_name, identifiers in (
        ("request_id", request_ids),
        ("task_id", task_ids),
    ):
        if len(identifiers) != len(results) or len(set(identifiers)) != len(results):
            raise ValueError(f"multi_turn_evidence_identifier_reused:{field_name}")
    duration_ms, duration_complete = _sum_numeric_metric("duration_ms")
    model_cost_units, model_cost_complete = _sum_numeric_metric("model_cost_units")
    tool_cost_units, tool_cost_complete = _sum_numeric_metric("tool_cost_units")
    clarification_count, clarification_complete = _sum_numeric_metric(
        "clarification_count"
    )
    correction_count, correction_complete = _sum_numeric_metric("correction_count")
    return {
        "terminal_state": last.get("terminal_state"),
        "visible_answer": last.get("visible_answer"),
        "visible_answers": [result.get("visible_answer") for result in results],
        "conversation_turn_count": len(results),
        "path_analysis": {
            "turns": path_turns,
            "selected_workflow_ids": _ordered_path_strings("selected_workflow_ids"),
            "observed_workflow_ids": _ordered_path_strings("observed_workflow_ids"),
            "observed_tool_names": _ordered_path_strings("observed_tool_names"),
            "progress_fact_count": sum(
                int(
                    _mapping(result.get("path_analysis")).get("progress_fact_count")
                    or 0
                )
                for result in results
            ),
        },
        "forbidden_mutations": [
            item
            for result in results
            for item in _sequence(result.get("forbidden_mutations"))
        ],
        "namespace_violations": _ordered_strings("namespace_violations"),
        "false_success_claims": false_success_claims,
        "execution_budget_units": sum(
            int(result.get("execution_budget_units") or 0) for result in results
        ),
        "operational_metrics": {
            "duration_ms": duration_ms,
            "duration_ms_complete": duration_complete,
            "timeout": any(
                _mapping(result.get("operational_metrics")).get("timeout") is True
                for result in results
            ),
            "model_cost_units": model_cost_units,
            "model_cost_units_complete": model_cost_complete,
            "tool_cost_units": tool_cost_units,
            "tool_cost_units_complete": tool_cost_complete,
            "clarification_count": clarification_count,
            "clarification_count_complete": clarification_complete,
            "correction_count": correction_count,
            "correction_count_complete": correction_complete,
            "false_success_count": len(false_success_claims),
            "namespace_violation_count": len(_ordered_strings("namespace_violations")),
        },
        "turn_execution_request_ids": request_ids,
        "submission": {
            "turns": [_mapping(result.get("submission")) for result in results]
        },
        "task_evidence": {
            "turns": [_mapping(result.get("task_evidence")) for result in results]
        },
        "turn_execution_records": [
            _mapping(result.get("turn_execution_record")) for result in results
        ],
        "turn_execution_record": _mapping(last.get("turn_execution_record")),
        "final_state_snapshots": [
            _mapping(result.get("final_state_snapshot")) for result in results
        ],
        "final_state_snapshot": _mapping(last.get("final_state_snapshot")),
    }


def _live_execution(args: argparse.Namespace, contract: Any) -> dict[str, Any]:
    campaign_execution_id = f"operational-certification-{uuid.uuid4()}"
    requested_runtime_bindings = _parse_runtime_binding_items(
        getattr(args, "runtime_binding", None)
    )
    session = requests.Session()
    environment = collect_run_environment(session=session, base_url=args.base_url)
    require_agent_test_server(
        environment=environment,
        base_url=args.base_url,
        allow_non_agent_test_server=args.allow_non_agent_test_server,
    )
    runtime_alignment = _collect_runtime_authority_alignment(
        session=session,
        base_url=args.base_url,
        environment=environment,
        allow_non_agent_test_server=args.allow_non_agent_test_server,
    )
    if runtime_alignment.get("verified") is not True:
        execution = _typed_failure_execution(
            code="certification_runtime_authority_alignment_unverified",
            message=(
                "The HTTP trial server and local certification authority "
                "process could not be proven to share one clean code revision "
                "and Mongo authority location."
            ),
            details={"runtime_alignment": runtime_alignment},
            contract=contract,
        )
        execution["campaign_execution_id"] = campaign_execution_id
        execution["environment"] = environment
        execution["runtime_authority_alignment"] = runtime_alignment
        execution["execution_sha256"] = stable_payload_digest(
            {
                key: value
                for key, value in execution.items()
                if key != "execution_sha256"
            }
        )
        return execution
    auth_status_before = get_auth_status(session=session, base_url=args.base_url)
    auth_login: dict[str, Any] = {}
    if auth_status_before.get("authenticated") is not True:
        auth_login = establish_browser_test_session(
            session=session,
            base_url=args.base_url,
            window_session_id=f"certification-{uuid.uuid4()}",
            timeout_seconds=30.0,
        )
    auth_status = get_auth_status(session=session, base_url=args.base_url)
    target_context = apply_target_session_context(
        session=session,
        base_url=args.base_url,
        user_concept_id=args.user_concept_id,
        organisation_concept_id=args.organisation_concept_id,
    )
    session_context = _mapping(target_context.get("session_context"))
    effective_user_id = _text(session_context.get("user_id"))
    effective_org_id = _text(session_context.get("organisation_id"))
    effective_namespace = _text(session_context.get("namespace"))
    target_mismatches: list[dict[str, str | None]] = []
    for field, requested, observed in (
        ("user_concept_id", args.user_concept_id, effective_user_id),
        ("organisation_concept_id", args.organisation_concept_id, effective_org_id),
        ("namespace", args.namespace, effective_namespace),
    ):
        if _text(requested) and _text(requested) != _text(observed):
            target_mismatches.append(
                {
                    "field": field,
                    "requested": _text(requested) or None,
                    "observed": _text(observed) or None,
                }
            )
    session_ready = bool(
        auth_status.get("authenticated") is True
        and target_context.get("target_session_context_ready") is True
        and session_context.get("authenticated") is True
        and effective_user_id
        and effective_org_id
        and effective_namespace
        and not target_mismatches
    )
    if not session_ready:
        execution = _typed_failure_execution(
            code="target_session_context_not_verified",
            message=(
                "The authenticated session could not be aligned to the requested "
                "pilot user, organisation, and namespace."
            ),
            details={
                "authenticated": auth_status.get("authenticated") is True,
                "target_session_context_ready": target_context.get(
                    "target_session_context_ready"
                )
                is True,
                "target_mismatches": target_mismatches,
                "effective_user_id": effective_user_id or None,
                "effective_org_id": effective_org_id or None,
                "effective_namespace": effective_namespace or None,
            },
            contract=contract,
        )
        execution.update(
            {
                "campaign_execution_id": campaign_execution_id,
                "environment": environment,
                "auth_status_before": auth_status_before,
                "auth_status": auth_status,
                "auth_login": auth_login,
                "target_session_context": target_context,
            }
        )
        execution["execution_sha256"] = stable_payload_digest(
            {
                key: value
                for key, value in execution.items()
                if key != "execution_sha256"
            }
        )
        return execution

    runtime_bindings = {
        **requested_runtime_bindings,
        "namespace": effective_namespace,
        "user_id": effective_user_id,
        "org_id": effective_org_id,
    }
    required_runtime_bindings = _runtime_binding_requirements(contract)
    missing_runtime_bindings = [
        key
        for key in required_runtime_bindings
        if key not in runtime_bindings or runtime_bindings[key] in (None, "")
    ]
    if missing_runtime_bindings:
        execution = _typed_failure_execution(
            code="operational_certification_runtime_bindings_missing",
            message=(
                "The represented suite requires explicit runtime bindings that "
                "were not supplied."
            ),
            details={
                "required_bindings": list(required_runtime_bindings),
                "supplied_binding_keys": sorted(runtime_bindings),
                "missing_bindings": missing_runtime_bindings,
            },
            contract=contract,
        )
        execution.update(
            {
                "campaign_execution_id": campaign_execution_id,
                "environment": environment,
                "auth_status_before": auth_status_before,
                "auth_status": auth_status,
                "auth_login": auth_login,
                "target_session_context": target_context,
                "runtime_authority_alignment": runtime_alignment,
            }
        )
        execution["execution_sha256"] = stable_payload_digest(
            {
                key: value
                for key, value in execution.items()
                if key != "execution_sha256"
            }
        )
        return execution

    if not args.migration_fixture:
        from src.backend.security.access_control import override_current_actor

        try:
            with override_current_actor(effective_user_id, effective_org_id):
                actor_contract = load_operational_certification_contract(
                    suite_concept_id=contract.suite_concept_id,
                    case_set=contract.case_set,
                )
        except Exception as exc:
            execution = _typed_failure_execution(
                code="certification_suite_actor_authority_unavailable",
                message=(
                    "The represented certification suite is not readable under "
                    "the authenticated pilot actor scope."
                ),
                details={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "effective_user_id": effective_user_id,
                    "effective_org_id": effective_org_id,
                    "effective_namespace": effective_namespace,
                },
                contract=contract,
            )
            execution["campaign_execution_id"] = campaign_execution_id
            execution["environment"] = environment
            execution["target_session_context"] = target_context
            execution["execution_sha256"] = stable_payload_digest(
                {
                    key: value
                    for key, value in execution.items()
                    if key != "execution_sha256"
                }
            )
            return execution
        if actor_contract.contract_sha256 != contract.contract_sha256:
            return _typed_failure_execution(
                code="certification_suite_actor_authority_mismatch",
                message=(
                    "Process-level and authenticated-actor suite resolution "
                    "produced different certification contracts."
                ),
                details={
                    "process_contract_sha256": contract.contract_sha256,
                    "actor_contract_sha256": actor_contract.contract_sha256,
                },
                contract=contract,
            )
        contract = actor_contract

    declared_workflow_ids = sorted(
        {
            workflow_id
            for scenario in contract.scenarios
            for workflow_id in (
                _text(_mapping(scenario.execution.get("inputs")).get("workflow_id")),
                *(
                    _text(spec.get("workflow_id") or spec.get("evaluator_id"))
                    for spec in scenario.evaluator_specs
                ),
            )
            if workflow_id
        }
    )
    execution_provenance: dict[str, Any] = {
        "schema_version": "operational_certification_execution_provenance.v1",
        "campaign_execution_id": campaign_execution_id,
        "server_environment": _bounded_safe_evidence(environment),
        "effective_user_id": effective_user_id,
        "effective_org_id": effective_org_id,
        "effective_namespace": effective_namespace,
        "authenticated": True,
        "requested_model": args.model or None,
        "suite_concept_id": contract.suite_concept_id,
        "suite_source": contract.source,
        "source_definition_sha256": contract.source_definition_sha256,
        "contract_sha256": contract.contract_sha256,
        "policy_sha256": stable_payload_digest(contract.policy),
        "declared_workflow_and_evaluator_ids": declared_workflow_ids,
        "tool_catalogue_sha256": _canonical_tool_catalogue_digest(),
        "runtime_authority_alignment_sha256": runtime_alignment.get("alignment_sha256"),
        "represented_evaluator_transport": "synchronous_workflow_executor",
        "represented_evaluator_authority_required": "vontology",
        "represented_evaluator_trace_persistence_required": True,
        "supported_operational_adapter_ids": [
            AUTHENTICATED_GENERATE_ADAPTER_ID,
            AUTHENTICATED_MULTI_TURN_ADAPTER_ID,
            DURABLE_WORKFLOW_ADAPTER_ID,
        ],
    }

    experiment_spec_id = (
        f"#V#operational_certification_experiment_spec_{contract.contract_sha256[:20]}"
    )
    experiment_run_id = _text(args.experiment_run_id)
    if experiment_run_id:
        return _typed_failure_execution(
            code="experiment_run_reuse_not_supported",
            message=(
                "Certification runs are single-owner records. Reusing an "
                "existing run is disabled until the experiment service exposes "
                "an atomic campaign claim primitive."
            ),
            details={"experiment_run_id": experiment_run_id},
            contract=contract,
        )
    else:
        spec_result = create_experiment_spec(
            name=f"Operational certification {contract.case_set}",
            experiment_spec_id=experiment_spec_id,
            namespace=effective_namespace,
            user_id=effective_user_id,
            org_id=effective_org_id,
            description=(
                "Five-trial operational certification bound to the represented "
                "Vontology suite and strict evaluator evidence."
            ),
            target_workflow_ids=declared_workflow_ids,
            experiment_suite_id=contract.suite_concept_id,
            expected_outcomes=[
                {
                    "scenario_id": scenario.scenario_id,
                    "acceptable_goal_states": list(scenario.acceptable_goal_states),
                }
                for scenario in contract.scenarios
            ],
            allowed_side_effects=[
                effect
                for scenario in contract.scenarios
                for effect in scenario.permitted_effects
            ],
            forbidden_side_effects=[
                {"scenario_id": scenario.scenario_id, "minefield": minefield}
                for scenario in contract.scenarios
                for minefield in scenario.minefields
            ],
            verdict_rules=contract.policy,
            metadata=execution_provenance,
        )
        if spec_result.get("success") is not True:
            return _typed_failure_execution(
                code="certification_experiment_spec_persistence_failed",
                message="The certification experiment specification could not be persisted.",
                details={"result": _bounded_safe_evidence(spec_result)},
                contract=contract,
            )
        run_result = start_experiment_run(
            experiment_spec_id=experiment_spec_id,
            namespace=effective_namespace,
            user_id=effective_user_id,
            org_id=effective_org_id,
            target_workflow_ids=declared_workflow_ids,
            metadata=execution_provenance,
        )
        if run_result.get("success") is not True:
            return _typed_failure_execution(
                code="certification_experiment_run_persistence_failed",
                message="The certification experiment run could not be started.",
                details={"result": _bounded_safe_evidence(run_result)},
                contract=contract,
            )
        experiment_run_id = _text(run_result.get("run_id"))
    execution_provenance["experiment_run_id"] = experiment_run_id
    from src.backend.security.access_control import override_current_actor

    with override_current_actor(effective_user_id, effective_org_id):
        represented_campaign_evidence = load_represented_operational_campaign_evidence(
            concept_id=args.campaign_evidence_concept_id,
            expected_namespace=effective_namespace,
            expected_user_id=effective_user_id,
            expected_org_id=effective_org_id,
        )
    execution_provenance["campaign_evidence_concept_id"] = (
        args.campaign_evidence_concept_id
    )
    execution_provenance["campaign_evidence_sha256"] = (
        represented_campaign_evidence.get("evidence_sha256")
        if isinstance(represented_campaign_evidence, Mapping)
        else None
    )
    used_conversation_session_ids: set[str] = set()

    def _unique_state_binding_verified(
        inputs: Mapping[str, Any],
        reset_policy: Mapping[str, Any],
    ) -> tuple[bool, list[str]]:
        binding_paths = [
            _text(item)
            for item in _sequence(reset_policy.get("isolation_binding_paths"))
            if _text(item)
        ]
        verified = bool(binding_paths) and all(
            isinstance(_json_pointer_value(inputs, path), str)
            and "{{isolation_id}}" in _json_pointer_value(inputs, path)
            for path in binding_paths
        )
        return verified, binding_paths

    def _run_authoritative_absence_probe(
        *,
        scenario: Any,
        trial_index: int,
        isolation_id: str,
        reset_policy: Mapping[str, Any],
    ) -> dict[str, Any]:
        probe_spec = _mapping(reset_policy.get("authoritative_absence_probe"))
        workflow_id = _text(probe_spec.get("workflow_id"))
        raw_probe_inputs = _mapping(probe_spec.get("inputs"))
        required_action_ids = [
            _text(item)
            for item in _sequence(probe_spec.get("required_action_ids"))
            if _text(item)
        ]
        probe_declares_isolation = "{{isolation_id}}" in json.dumps(
            raw_probe_inputs,
            ensure_ascii=True,
            sort_keys=True,
        )
        if (
            not workflow_id
            or not probe_declares_isolation
            or not required_action_ids
            or "llm.action" in required_action_ids
        ):
            return {
                "verified": False,
                "reason": "authoritative_absence_probe_required",
                "workflow_id": workflow_id or None,
                "probe_declares_isolation": probe_declares_isolation,
                "required_action_ids": required_action_ids,
            }
        probe_inputs = _mapping(
            _substitute_trial_values(
                raw_probe_inputs,
                trial_index=trial_index,
                isolation_id=isolation_id,
                runtime_bindings=runtime_bindings,
            )
        )
        probe_inputs["isolation_id"] = isolation_id
        payload = _execute_represented_workflow_synchronously(
            workflow_id=workflow_id,
            inputs=probe_inputs,
            namespace=effective_namespace,
            user_id=effective_user_id,
            org_id=effective_org_id,
            source_event_type="operational_certification_absence_probe",
            source_event_id=(
                f"{campaign_execution_id}:{scenario.scenario_id}:{trial_index}:absence"
            ),
            event_idempotency_key=(
                "operational-certification-absence-probe:"
                f"{stable_payload_digest({'workflow_id': workflow_id, 'inputs': probe_inputs, 'namespace': effective_namespace})}"
            ),
            execution_metadata={
                "campaign_execution_id": campaign_execution_id,
                "contract_sha256": contract.contract_sha256,
                "scenario_id": scenario.scenario_id,
                "trial_index": trial_index,
                "isolation_id_sha256": stable_payload_digest(isolation_id),
            },
            timeout_seconds=args.timeout_seconds,
            require_policy_identity=False,
            requested_model=args.model or None,
        )
        probe_result = _mapping(
            _mapping(payload.get("workflow_output")).get(
                "represented_operational_state_probe_result"
            )
        )
        evidence = _sequence(probe_result.get("evidence"))
        probe_result_sha256 = (
            stable_payload_digest(probe_result) if probe_result else None
        )
        probe_resolution_lineage = _resolution_lineage_from_probe_result(probe_result)
        probe_resolution_lineage_sha256 = (
            stable_payload_digest(probe_resolution_lineage)
            if probe_resolution_lineage
            else None
        )
        action_evidence = [
            _mapping(item)
            for item in _sequence(payload.get("workflow_action_evidence"))
            if isinstance(item, Mapping)
        ]
        successful_action_ids = {
            _text(item.get("action_id"))
            for item in action_evidence
            if _text(item.get("status")).lower() == "success"
        }
        matching_receipt_actions = [
            item
            for item in action_evidence
            if _text(item.get("action_id")) in required_action_ids
            and _text(item.get("status")).lower() == "success"
            and probe_result_sha256
            and item.get("state_probe_result_sha256") == probe_result_sha256
        ]
        matching_resolution_actions = [
            item
            for item in action_evidence
            if _text(item.get("action_id")) in required_action_ids
            and _text(item.get("status")).lower() == "success"
            and probe_resolution_lineage_sha256
            and item.get("resolution_lineage_sha256") == probe_resolution_lineage_sha256
        ]
        checks = {
            "execution_succeeded": payload.get("success") is True,
            "trace_persisted": payload.get("trace_persisted") is True,
            "live_vontology_authority": (
                _text(payload.get("workflow_authority_source")).lower() == "vontology"
            ),
            "result_schema_exact": (
                probe_result.get("schema_version")
                == "represented_operational_state_probe_result.v1"
            ),
            "isolation_id_exact": (
                _text(probe_result.get("isolation_id")) == isolation_id
            ),
            "namespace_exact": (
                _text(probe_result.get("namespace")) == effective_namespace
            ),
            "authoritative_absence_observed": (
                probe_result.get("target_absent") is True
            ),
            "evidence_present": bool(evidence),
            "resolver_result_lineage_present": bool(probe_resolution_lineage),
            "resolver_result_lineage_not_found": (
                _text(_mapping(probe_resolution_lineage).get("status")).lower()
                == "not_found"
            ),
            "resolver_target_binds_isolation": isolation_id
            in _text(_mapping(probe_resolution_lineage).get("target_name")),
            "required_actions_executed": all(
                action_id in successful_action_ids for action_id in required_action_ids
            ),
            "canonical_tool_output_exact": bool(matching_receipt_actions),
            "resolver_output_causal_lineage_exact": bool(matching_resolution_actions),
        }
        return {
            "verified": all(checks.values()),
            "reason": (
                None
                if all(checks.values())
                else "authoritative_absence_probe_unverified"
            ),
            "workflow_id": workflow_id,
            "required_action_ids": required_action_ids,
            "checks": checks,
            "execution_trace_id": payload.get("execution_trace_id"),
            "workflow_definition_identity_sha256": payload.get(
                "workflow_definition_identity_sha256"
            ),
            "workflow_output_sha256": payload.get("workflow_output_sha256"),
            "result_sha256": probe_result_sha256,
            "matching_action_evidence": matching_receipt_actions,
            "resolution_lineage_sha256": probe_resolution_lineage_sha256,
            "matching_resolution_action_evidence": matching_resolution_actions,
        }

    def reset_scenario(scenario: Any, trial_index: int) -> Mapping[str, Any]:
        adapter_id = _text(scenario.execution.get("adapter_id"))
        inputs = _mapping(scenario.execution.get("inputs"))
        reset_policy = _mapping(scenario.reset_policy)
        isolation_mode = _text(reset_policy.get("mode"))
        isolation_id = f"{scenario.scenario_id}-{trial_index}-{uuid.uuid4()}"
        if adapter_id in {
            AUTHENTICATED_GENERATE_ADAPTER_ID,
            AUTHENTICATED_MULTI_TURN_ADAPTER_ID,
        }:
            read_only_scenario = bool(
                scenario.metadata.get("read_only") is True
                or not scenario.permitted_effects
            )
            unique_state_declared, isolation_binding_paths = (
                _unique_state_binding_verified(inputs, reset_policy)
            )
            isolation_strategy_supported = bool(
                (
                    isolation_mode in {"read_only", "new_chat_session"}
                    and read_only_scenario
                )
                or (isolation_mode == "unique_state" and unique_state_declared)
            )
            if not isolation_strategy_supported:
                return {
                    "success": False,
                    "reason": (
                        "unique_state_template_required"
                        if isolation_mode == "unique_state"
                        else "represented_reset_policy_not_executable"
                    ),
                    "isolation_mode": isolation_mode or None,
                    "unique_state_declared": unique_state_declared,
                    "isolation_binding_paths": isolation_binding_paths,
                }
            absence_probe = (
                _run_authoritative_absence_probe(
                    scenario=scenario,
                    trial_index=trial_index,
                    isolation_id=isolation_id,
                    reset_policy=reset_policy,
                )
                if isolation_mode == "unique_state"
                else {"verified": True, "reason": None}
            )
            if absence_probe.get("verified") is not True:
                return {
                    "success": False,
                    "reason": absence_probe.get("reason"),
                    "isolation_mode": isolation_mode,
                    "state_isolation_verified": False,
                    "isolation_id": isolation_id,
                    "unique_state_declared": unique_state_declared,
                    "isolation_binding_paths": isolation_binding_paths,
                    "authoritative_absence_probe": absence_probe,
                }
            chat = create_replay_chat_session(
                session=session,
                base_url=args.base_url,
                case_id=f"{scenario.scenario_id}-trial-{trial_index}",
            )
            conversation_session_id = _text(chat.get("session_id"))
            conversation_session_unique = bool(
                conversation_session_id
                and conversation_session_id not in used_conversation_session_ids
            )
            if conversation_session_unique:
                used_conversation_session_ids.add(conversation_session_id)
            return {
                "success": bool(
                    conversation_session_unique and isolation_strategy_supported
                ),
                "isolation_mode": isolation_mode,
                "state_isolation_verified": bool(
                    conversation_session_unique and isolation_strategy_supported
                ),
                "isolation_id": isolation_id,
                "conversation_session_id": conversation_session_id or None,
                "conversation_session_unique": conversation_session_unique,
                "unique_state_declared": unique_state_declared,
                "isolation_binding_paths": isolation_binding_paths,
                "authoritative_absence_probe": absence_probe,
                "reason": (
                    None
                    if conversation_session_unique
                    else "conversation_session_missing_or_reused"
                ),
                "pre_state_snapshot": {
                    "schema_version": "operational_state_snapshot.v1",
                    "mode": (
                        "read_only_no_mutable_state"
                        if isolation_mode in {"read_only", "new_chat_session"}
                        else "cryptographically_unique_state"
                    ),
                    "isolation_id": isolation_id,
                    "verified": True,
                    "isolation_strategy_verified": isolation_strategy_supported,
                    "authoritative_absence_readback_verified": (
                        absence_probe.get("verified") is True
                        if isolation_mode == "unique_state"
                        else True
                    ),
                },
            }
        if adapter_id == DURABLE_WORKFLOW_ADAPTER_ID:
            read_only_scenario = bool(
                scenario.metadata.get("read_only") is True
                or not scenario.permitted_effects
            )
            unique_state_declared, isolation_binding_paths = (
                _unique_state_binding_verified(inputs, reset_policy)
            )
            supported = (isolation_mode == "read_only" and read_only_scenario) or (
                isolation_mode == "unique_state" and unique_state_declared
            )
            absence_probe = (
                _run_authoritative_absence_probe(
                    scenario=scenario,
                    trial_index=trial_index,
                    isolation_id=isolation_id,
                    reset_policy=reset_policy,
                )
                if supported and isolation_mode == "unique_state"
                else {"verified": isolation_mode == "read_only", "reason": None}
            )
            isolation_verified = supported and absence_probe.get("verified") is True
            return {
                "success": isolation_verified,
                "isolation_mode": isolation_mode or None,
                "state_isolation_verified": isolation_verified,
                "unique_state_declared": unique_state_declared,
                "isolation_binding_paths": isolation_binding_paths,
                "isolation_id": isolation_id,
                "correlation_id": f"cert-{isolation_id}",
                "reason": (
                    None
                    if isolation_verified
                    else (
                        absence_probe.get("reason")
                        if supported
                        else "durable_reset_requires_read_only_or_unique_state_template"
                    )
                ),
                "authoritative_absence_probe": absence_probe,
                "pre_state_snapshot": {
                    "schema_version": "operational_state_snapshot.v1",
                    "mode": isolation_mode or None,
                    "isolation_id": isolation_id,
                    "verified": supported and isolation_mode == "read_only",
                    "isolation_strategy_verified": supported,
                    "authoritative_absence_readback_verified": (
                        absence_probe.get("verified") is True
                        if isolation_mode == "unique_state"
                        else True
                    ),
                },
            }
        return {
            "success": False,
            "reason": "unsupported_scenario_adapter",
            "adapter_id": adapter_id or None,
        }

    def execute_scenario(
        scenario: Any,
        trial_index: int,
        reset_evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        adapter_id = _text(scenario.execution.get("adapter_id"))
        isolation_id = _text(reset_evidence.get("isolation_id"))
        inputs = _mapping(
            _substitute_trial_values(
                scenario.execution.get("inputs") or {},
                trial_index=trial_index,
                isolation_id=isolation_id,
                runtime_bindings=runtime_bindings,
            )
        )
        unresolved_bindings = _unresolved_runtime_placeholders(inputs)
        if unresolved_bindings:
            raise ValueError(
                "operational_certification_runtime_bindings_unresolved:"
                + ",".join(unresolved_bindings)
            )
        started = time.perf_counter()
        if adapter_id == AUTHENTICATED_MULTI_TURN_ADAPTER_ID:
            raw_turns = _sequence(inputs.get("turns"))
            if (
                not raw_turns
                or len(raw_turns) > 16
                or not all(isinstance(turn, Mapping) for turn in raw_turns)
            ):
                raise ValueError("represented_multi_turn_inputs_missing")
            shared_inputs = {
                key: value for key, value in inputs.items() if key != "turns"
            }
            turn_results: list[Mapping[str, Any]] = []
            for turn_index, raw_turn in enumerate(raw_turns, start=1):
                turn_inputs = {**shared_inputs, **dict(raw_turn)}
                if not _text(turn_inputs.get("prompt")):
                    raise ValueError(
                        f"represented_multi_turn_prompt_missing:{turn_index}"
                    )
                single_turn_scenario = replace(
                    scenario,
                    execution={
                        "adapter_id": AUTHENTICATED_GENERATE_ADAPTER_ID,
                        "inputs": turn_inputs,
                    },
                )
                turn_results.append(
                    execute_scenario(
                        single_turn_scenario,
                        trial_index,
                        reset_evidence,
                    )
                )
            return _aggregate_authenticated_multi_turn_results(turn_results)
        if adapter_id == AUTHENTICATED_GENERATE_ADAPTER_ID:
            prompt = _text(inputs.get("prompt"))
            if not prompt:
                raise ValueError("represented_scenario_prompt_missing")
            case = ReplayCase(
                case_id=f"{scenario.scenario_id}-trial-{trial_index}",
                prompt=prompt,
                expected_workflow_id=_text(inputs.get("expected_workflow_id")) or None,
                expected_progress_fact_ids=(),
                expected_contract_ids=(),
                requires_gmail=bool(inputs.get("requires_gmail")),
                workflow_inputs=_mapping(inputs.get("workflow_inputs")) or None,
            )
            client_request_id = f"certification-{uuid.uuid4()}"
            submission = submit_background_generate(
                session=session,
                base_url=args.base_url,
                case=case,
                client_request_id=client_request_id,
                conversation_session_id=_text(
                    reset_evidence.get("conversation_session_id")
                )
                or None,
                gmail_profile=_text(inputs.get("gmail_profile")) or None,
                model=args.model or None,
                presenter_mode=False,
                thinking_card_mode="on",
                agent_test_selector_replay_mode=(
                    AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM
                ),
            )
            task_id = _text(submission.get("task_id"))
            request_id = _text(submission.get("request_id")) or task_id
            if not task_id or not request_id:
                raise RuntimeError("background_generate_identifiers_missing")
            task_evidence = poll_replay_task(
                session=session,
                base_url=args.base_url,
                task_id=task_id,
                request_id=request_id,
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
                cancel_on_timeout=True,
            )
            task_result = _mapping(task_evidence.get("task_result"))
            turn_record = fetch_turn_record(
                session=session,
                base_url=args.base_url,
                chat_session_id=_text(reset_evidence.get("conversation_session_id")),
                request_id=request_id,
                namespace=effective_namespace,
                task_result=task_result,
            )
            expected_session_id = _text(reset_evidence.get("conversation_session_id"))
            observed_request_id = _text(turn_record.get("request_id"))
            observed_session_id = _text(turn_record.get("session_id"))
            observed_namespace = _text(turn_record.get("namespace"))
            binding_mismatches = [
                field
                for field, expected, observed in (
                    ("request_id", request_id, observed_request_id),
                    ("session_id", expected_session_id, observed_session_id),
                    ("namespace", effective_namespace, observed_namespace),
                )
                if not observed or observed != expected
            ]
            if binding_mismatches:
                raise RuntimeError(
                    "turn_execution_record_binding_mismatch:"
                    + ",".join(binding_mismatches)
                )
            selector_path_evidence = _represented_selector_evidence(turn_record)
            if selector_path_evidence.get("complete") is not True:
                missing_selector_evidence = ",".join(
                    str(item)
                    for item in _sequence(selector_path_evidence.get("missing_fields"))
                )
                raise RuntimeError(
                    "represented_selector_evidence_incomplete:"
                    + (missing_selector_evidence or "unknown")
                )
            evidence_sources = [task_result, task_evidence, turn_record]
            observed_tools = _observed_tool_names(evidence_sources)
            forbidden_names = {
                _text(item).lower()
                for item in _sequence(inputs.get("forbidden_tool_names"))
                if _text(item)
            }
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            completion_gate = _mapping(turn_record.get("completion_gate"))
            terminal_receipt = _mapping(turn_record.get("terminal_outcome_receipt"))
            if not completion_gate:
                raise RuntimeError("turn_completion_gate_missing")
            if not terminal_receipt:
                raise RuntimeError("turn_terminal_outcome_receipt_missing")
            false_success_claims: list[dict[str, Any]] = []
            if (
                _mapping(turn_record.get("final_response")).get(
                    "completion_claim_detected"
                )
                is True
                and completion_gate.get("safe_to_claim_completion") is not True
            ):
                false_success_claims.append(
                    {
                        "request_id": request_id,
                        "reason": "completion_claim_without_verified_gate",
                    }
                )
            observed_namespaces = {
                _text(mapping.get("namespace"))
                for mapping in _walk_mappings(turn_record)
                if _text(mapping.get("namespace"))
            }
            namespace_violations = [
                namespace
                for namespace in sorted(observed_namespaces)
                if namespace != effective_namespace
            ]
            record_visible_answer, record_visible_answer_source = (
                extract_visible_answer_from_turn_record(turn_record)
            )
            visible_answer = record_visible_answer or extract_visible_answer(
                task_result
            )
            return {
                "terminal_state": _text(
                    completion_gate.get("decision") or terminal_receipt.get("outcome")
                )
                or "inconclusive",
                "visible_answer": visible_answer,
                "visible_answer_source": (
                    f"turn_execution_record.{record_visible_answer_source}"
                    if record_visible_answer_source
                    else "background_task_result"
                ),
                "path_analysis": {
                    "selected_workflow_ids": extract_selected_workflow_ids(
                        *evidence_sources
                    ),
                    "observed_workflow_ids": extract_observed_workflow_ids(
                        *evidence_sources
                    ),
                    "selector_diagnostics": extract_selector_diagnostics(
                        *evidence_sources
                    ),
                    "selector_path_evidence": selector_path_evidence,
                    "observed_tool_names": observed_tools,
                    "progress_fact_count": len(
                        extract_progress_facts(*evidence_sources)
                    ),
                },
                "forbidden_mutations": [
                    name for name in observed_tools if name.lower() in forbidden_names
                ],
                "namespace_violations": namespace_violations,
                "false_success_claims": false_success_claims,
                "execution_budget_units": len(
                    _sequence(task_evidence.get("task_statuses"))
                )
                + len(_sequence(task_evidence.get("progress_snapshots"))),
                "operational_metrics": {
                    "duration_ms": elapsed_ms,
                    "timeout": bool(task_evidence.get("timed_out")),
                    "model_cost_units": task_evidence.get("model_cost_units"),
                    "tool_cost_units": task_evidence.get("tool_cost_units"),
                    "clarification_count": int(
                        completion_gate.get("requires_follow_up") is True
                    ),
                    "correction_count": 0,
                    "false_success_count": len(false_success_claims),
                    "namespace_violation_count": len(namespace_violations),
                },
                "turn_execution_request_ids": [request_id],
                "submission": _bounded_safe_evidence(
                    {
                        "task_id": task_id,
                        "request_id": request_id,
                        "client_request_id": client_request_id,
                        "agent_test_selector_replay_mode": (
                            AGENT_TEST_SELECTOR_REPLAY_MODE_REPRESENTED_LLM
                        ),
                    }
                ),
                "task_evidence": _task_evidence_projection(task_evidence),
                "turn_execution_record": _turn_record_evidence_projection(turn_record),
                "final_state_snapshot": {
                    "schema_version": "operational_state_snapshot.v1",
                    "isolation_id": isolation_id,
                    "terminal_outcome_receipt_sha256": (
                        stable_payload_digest(terminal_receipt)
                        if terminal_receipt
                        else None
                    ),
                    "committed_effects": terminal_receipt.get(
                        "committed_effects",
                        [],
                    ),
                },
            }
        if adapter_id == DURABLE_WORKFLOW_ADAPTER_ID:
            from src.backend.integrations.internal_mcp.catalogue import (
                _workflow_execute,
            )

            workflow_id = _text(inputs.get("workflow_id"))
            if not workflow_id:
                raise ValueError("represented_scenario_workflow_id_missing")
            if "submission_plan" not in inputs:
                submission_plan: list[Any] = [{"await_terminal": True}]
            else:
                raw_submission_plan = inputs.get("submission_plan")
                if not isinstance(raw_submission_plan, Sequence) or isinstance(
                    raw_submission_plan,
                    (str, bytes, bytearray),
                ):
                    raise ValueError("represented_durable_submission_plan_invalid")
                submission_plan = list(raw_submission_plan)
            if (
                not submission_plan
                or len(submission_plan) > 8
                or not all(isinstance(step, Mapping) for step in submission_plan)
            ):
                raise ValueError("represented_durable_submission_plan_invalid")
            correlation_id = _text(reset_evidence.get("correlation_id"))
            workflow_inputs = _mapping(inputs.get("workflow_inputs"))
            if _text(args.model):
                # ``--model`` is a campaign runtime control for every LLM-backed
                # adapter. Carry it unchanged into the durable instance so the
                # workflow runtime, rather than the runner, resolves provider
                # details and records the override in execution telemetry.
                workflow_inputs["requested_model"] = _text(args.model)
            payloads: list[dict[str, Any]] = []
            for step_index, raw_step in enumerate(submission_plan, start=1):
                step = dict(raw_step)
                await_terminal = step.get("await_terminal", True)
                if not isinstance(await_terminal, bool):
                    raise ValueError(
                        f"represented_durable_await_terminal_invalid:{step_index}"
                    )
                raw_timeout = step.get("timeout_seconds", args.timeout_seconds)
                if isinstance(raw_timeout, bool) or not isinstance(
                    raw_timeout, (int, float)
                ):
                    raise ValueError(
                        f"represented_durable_timeout_invalid:{step_index}"
                    )
                payloads.append(
                    _mapping(
                        _workflow_execute(
                            workflow_id=workflow_id,
                            inputs=workflow_inputs,
                            namespace=effective_namespace,
                            user_id=effective_user_id,
                            org_id=effective_org_id,
                            await_terminal=await_terminal,
                            include_step_result_envelopes=True,
                            include_trace=True,
                            timeout_seconds=float(raw_timeout),
                            source_event_type="operational_certification_trial",
                            source_event_id=correlation_id,
                            event_idempotency_key=correlation_id,
                        )
                    )
                )
            payload = payloads[-1]
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            observed_tools = _observed_tool_names(payloads)
            forbidden_names = {
                _text(item).lower()
                for item in _sequence(inputs.get("forbidden_tool_names"))
                if _text(item)
            }
            observed_namespaces = {
                _text(mapping.get("namespace"))
                for mapping in _walk_mappings(payloads)
                if _text(mapping.get("namespace"))
            }
            namespace_violations = [
                namespace
                for namespace in sorted(observed_namespaces)
                if namespace != effective_namespace
            ]
            workflow_instance_ids = [
                _text(step_payload.get("instance_id")) for step_payload in payloads
            ]
            durable_binding_mismatches: list[str] = []
            for step_index, step_payload in enumerate(payloads, start=1):
                instance = _mapping(step_payload.get("workflow_instance"))
                for field_name, expected, observed in (
                    (
                        "namespace",
                        effective_namespace,
                        _text(instance.get("namespace")),
                    ),
                    ("user_id", effective_user_id, _text(instance.get("user_id"))),
                    ("org_id", effective_org_id, _text(instance.get("org_id"))),
                ):
                    if not observed or observed != expected:
                        durable_binding_mismatches.append(
                            f"step_{step_index}.{field_name}"
                        )
            if durable_binding_mismatches:
                raise RuntimeError(
                    "durable_workflow_instance_binding_mismatch:"
                    + ",".join(durable_binding_mismatches)
                )
            created_new_flags = [
                step_payload.get("created_new") for step_payload in payloads
            ]
            submission_statuses = [
                _text(step_payload.get("status")).lower() for step_payload in payloads
            ]
            idempotent_instance_reuse_observed = bool(
                len(payloads) > 1
                and all(workflow_instance_ids)
                and len(set(workflow_instance_ids)) == 1
                and created_new_flags[0] is True
                and all(flag is False for flag in created_new_flags[1:])
                and all(status == "reused" for status in submission_statuses[1:])
            )
            timed_out = any(
                mapping.get("timed_out") is True for mapping in _walk_mappings(payloads)
            )
            workflow_execution = _mapping(payload.get("workflow_execution"))
            terminal_state = _text(
                payload.get("final_status")
                or workflow_execution.get("final_status")
                or workflow_execution.get("current_status")
            )
            if not terminal_state:
                terminal_state = "inconclusive"
            return {
                "terminal_state": terminal_state,
                "path_analysis": {
                    "workflow_id": workflow_id,
                    "observed_tool_names": observed_tools,
                    "execution": _bounded_safe_evidence(payload),
                    "submissions": _bounded_safe_evidence(payloads),
                    "submission_count": len(payloads),
                    "workflow_instance_ids": workflow_instance_ids,
                    "submission_created_new_flags": created_new_flags,
                    "submission_statuses": submission_statuses,
                    "idempotent_instance_reuse_observed": (
                        idempotent_instance_reuse_observed
                    ),
                },
                "forbidden_mutations": [
                    name for name in observed_tools if name.lower() in forbidden_names
                ],
                "namespace_violations": namespace_violations,
                "false_success_claims": [],
                "execution_budget_units": len(_walk_mappings(payloads)),
                "operational_metrics": {
                    "duration_ms": elapsed_ms,
                    "timeout": timed_out,
                    "model_cost_units": None,
                    "tool_cost_units": None,
                    "clarification_count": 0,
                    "correction_count": 0,
                    "false_success_count": 0,
                    "namespace_violation_count": len(namespace_violations),
                },
                "workflow_execution": _bounded_safe_evidence(payload),
                "workflow_submissions": _bounded_safe_evidence(payloads),
                "final_state_snapshot": {
                    "schema_version": "operational_state_snapshot.v1",
                    "isolation_id": isolation_id,
                    "workflow_id": workflow_id,
                    "workflow_instance_ids": workflow_instance_ids,
                    "submission_count": len(payloads),
                    "terminal_payload_sha256": stable_payload_digest(payload),
                },
            }
        raise ValueError(f"unsupported_scenario_adapter:{adapter_id or 'missing'}")

    def execute_evaluator(
        scenario: Any,
        evaluator_spec: Mapping[str, Any],
        trial_index: int,
        observation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        workflow_id = _text(
            evaluator_spec.get("workflow_id") or evaluator_spec.get("evaluator_id")
        )
        observation_sha256 = stable_payload_digest(
            json_serialisable_projection(observation)
        )
        evaluator_execution_digest = stable_payload_digest(
            {
                "campaign_execution_id": campaign_execution_id,
                "contract_sha256": contract.contract_sha256,
                "scenario_id": scenario.scenario_id,
                "trial_index": trial_index,
                "workflow_id": workflow_id,
                "effective_namespace": effective_namespace,
                "effective_user_id": effective_user_id,
                "effective_org_id": effective_org_id,
                "observation_sha256": observation_sha256,
            }
        )
        payload = _execute_represented_workflow_synchronously(
            workflow_id=workflow_id,
            inputs={
                "scenario_contract": scenario.to_projection(),
                "trial_index": trial_index,
                "trial_observation": json_serialisable_projection(observation),
            },
            namespace=effective_namespace,
            user_id=effective_user_id,
            org_id=effective_org_id,
            source_event_type="operational_certification_evaluation",
            source_event_id=(
                f"{campaign_execution_id}:{scenario.scenario_id}:{trial_index}"
            ),
            event_idempotency_key=(
                f"operational-certification-evaluator:{evaluator_execution_digest}"
            ),
            execution_metadata={
                "campaign_execution_id": campaign_execution_id,
                "contract_sha256": contract.contract_sha256,
                "scenario_id": scenario.scenario_id,
                "trial_index": trial_index,
                "evaluator_id": _text(evaluator_spec.get("evaluator_id")),
                "observation_sha256": observation_sha256,
                "evaluator_execution_digest": evaluator_execution_digest,
            },
            timeout_seconds=args.timeout_seconds,
            requested_model=args.model or None,
        )
        represented_result = _represented_evaluator_result_from_execution(payload)
        if represented_result is None:
            raise RuntimeError(
                "represented_evaluator_synchronous_execution_failed:"
                f"{_text(payload.get('error_code')) or 'validated_output_missing'}:"
                f"trace={_text(payload.get('execution_trace_id')) or _text(payload.get('attempted_execution_trace_id')) or 'none'}"
            )
        return represented_result

    def _record_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
        return record_experiment_observation(
            run_id=experiment_run_id,
            observations=observation,
            turn_execution_request_ids=observation.get(
                "turn_execution_request_ids",
                (),
            ),
        )

    execution = run_operational_certification_campaign(
        contract,
        reset_scenario=reset_scenario,
        execute_scenario=execute_scenario,
        execute_represented_evaluator=execute_evaluator,
        record_experiment_observation=_record_observation,
        execution_provenance=execution_provenance,
        hard_campaign_gates=(
            {
                "gate_id": "live_release_evidence_eligible",
                "passed": contract.source == "vontology" and not args.migration_fixture,
                "blocker_code": "non_live_suite_authority_not_release_eligible",
                "message": (
                    "Only an unambiguous live Vontology suite may issue a release "
                    "certification verdict."
                ),
                "details": {"suite_source": contract.source},
            },
            {
                "gate_id": "runtime_authority_alignment_verified",
                "passed": runtime_alignment.get("verified") is True,
                "blocker_code": (
                    "certification_runtime_authority_alignment_unverified"
                ),
                "message": (
                    "HTTP trials and certification authority writes must use "
                    "one clean runtime and Mongo authority location."
                ),
                "details": runtime_alignment,
            },
        ),
        represented_campaign_evidence=represented_campaign_evidence,
    )
    campaign = _mapping(execution.get("campaign_result"))
    expected_experiment_observation_count = len(contract.scenarios) * 5 + 1
    try:
        verdict_result = compute_experiment_verdict(run_id=experiment_run_id)
        final_experiment_state = get_experiment_run_state(experiment_run_id)
        final_state = _mapping(final_experiment_state)
        final_verdict = _text(final_state.get("verdict")).lower()
        final_status = _text(final_state.get("status")).lower()
        final_observations = _sequence(final_state.get("observations"))
        canonical_observation_validation = (
            _validate_canonical_certification_observations(
                observations=final_observations,
                trial_results=_sequence(execution.get("trial_results")),
                campaign=campaign,
                experiment_run_id=experiment_run_id,
                campaign_execution_id=campaign_execution_id,
                namespace=effective_namespace,
                user_id=effective_user_id,
                org_id=effective_org_id,
                execution_provenance=_mapping(execution.get("execution_provenance")),
            )
        )
        final_metadata = _mapping(final_state.get("metadata"))
        finalisation_checks = {
            "verdict_receipt_success": verdict_result.get("success") is True,
            "canonical_readback_available": bool(final_state),
            "run_id_exact": _text(final_state.get("run_id")) == experiment_run_id,
            "experiment_spec_id_exact": (
                _text(final_state.get("experiment_spec_id")) == experiment_spec_id
            ),
            "namespace_exact": (
                _text(final_state.get("namespace")) == effective_namespace
            ),
            "user_id_exact": _text(final_state.get("user_id")) == effective_user_id,
            "org_id_exact": _text(final_state.get("org_id")) == effective_org_id,
            "terminal_status": final_status in {"completed", "failed"},
            "verdict_exact": (
                final_verdict
                and final_verdict == _text(verdict_result.get("verdict")).lower()
            ),
            "observation_count_exact": (
                len(final_observations) == expected_experiment_observation_count
            ),
            "run_metadata_contract_exact": (
                final_metadata.get("suite_concept_id") == contract.suite_concept_id
                and final_metadata.get("contract_sha256") == contract.contract_sha256
                and final_metadata.get("source_definition_sha256")
                == contract.source_definition_sha256
            ),
            "canonical_observations_exact": (
                canonical_observation_validation.get("success") is True
            ),
        }
        experiment_finalisation = {
            "schema_version": "operational_certification_experiment_finalisation.v1",
            "success": all(finalisation_checks.values()),
            "checks": finalisation_checks,
            "expected_observation_count": expected_experiment_observation_count,
            "observed_observation_count": len(final_observations),
            "status": final_status or None,
            "verdict": final_verdict or None,
            "verdict_result": _bounded_safe_evidence(verdict_result),
            "canonical_observation_validation": (canonical_observation_validation),
            "canonical_run_state_sha256": (
                stable_payload_digest(final_state) if final_state else None
            ),
        }
    except Exception as exc:
        experiment_finalisation = {
            "schema_version": "operational_certification_experiment_finalisation.v1",
            "success": False,
            "expected_observation_count": expected_experiment_observation_count,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    live_authority = contract.source == "vontology" and not args.migration_fixture
    execution["campaign_result"] = campaign
    execution["campaign_execution_id"] = campaign_execution_id
    execution["experiment_run_id"] = experiment_run_id
    execution["experiment_finalisation"] = experiment_finalisation
    execution["release_eligibility"] = {
        "eligible": bool(
            live_authority
            and campaign.get("certified") is True
            and campaign.get("experiment_persistence_complete") is True
            and experiment_finalisation.get("success") is True
            and experiment_finalisation.get("status") == "completed"
            and runtime_alignment.get("verified") is True
        ),
        "suite_source": contract.source,
        "authenticated": True,
        "effective_user_id": effective_user_id,
        "effective_org_id": effective_org_id,
        "effective_namespace": effective_namespace,
        "experiment_finalisation_success": experiment_finalisation.get("success")
        is True,
        "experiment_status": experiment_finalisation.get("status"),
        "experiment_verdict": experiment_finalisation.get("verdict"),
        "runtime_authority_alignment_verified": runtime_alignment.get("verified")
        is True,
    }
    execution["environment"] = environment
    execution["auth_status_before"] = auth_status_before
    execution["auth_status"] = auth_status
    execution["auth_login"] = auth_login
    execution["target_session_context"] = target_context
    execution["runtime_authority_alignment"] = runtime_alignment
    execution["execution_sha256"] = stable_payload_digest(
        {key: value for key, value in execution.items() if key != "execution_sha256"}
    )
    return execution


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the represented five-trial operational certification suite."
    )
    parser.add_argument(
        "--suite-concept-id",
        default=OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
    )
    parser.add_argument("--case-set", default=None)
    parser.add_argument(
        "--migration-fixture",
        action="store_true",
        help="Explicitly load the repository import fixture instead of live authority.",
    )
    parser.add_argument(
        "--observations-json",
        default="",
        help="Evaluate an existing trial-observation dossier instead of executing live.",
    )
    parser.add_argument("--base-url", default=get_default_agent_test_base_url())
    parser.add_argument("--namespace", default="")
    parser.add_argument("--user-concept-id", default="")
    parser.add_argument("--organisation-concept-id", default="")
    parser.add_argument(
        "--runtime-binding",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Bind a represented suite runtime placeholder. Repeat as needed; "
            "VALUE is parsed as JSON when valid, otherwise as a string. Actor "
            "scope bindings come from the verified authenticated session."
        ),
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.75)
    parser.add_argument("--allow-non-agent-test-server", action="store_true")
    parser.add_argument("--experiment-run-id", default="")
    parser.add_argument(
        "--campaign-evidence-concept-id",
        default=OPERATIONAL_CERTIFICATION_CAMPAIGN_EVIDENCE_CONCEPT_ID,
        help=(
            "Live Vontology concept carrying agreed pilot corpus/envelopes, "
            "learning receipts, and safe operating envelope evidence."
        ),
    )
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-markdown", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    contract = None
    try:
        contract = load_operational_certification_contract(
            suite_concept_id=args.suite_concept_id,
            case_set=args.case_set,
            fixture_path=MIGRATION_FIXTURE_PATH if args.migration_fixture else None,
        )
        execution = (
            _offline_execution(contract, Path(args.observations_json))
            if args.observations_json
            else _live_execution(args, contract)
        )
    except BenchmarkSuiteAuthorityMissingError as exc:
        execution = _typed_failure_execution(
            code="benchmark_suite_authority_unavailable",
            message="The represented operational certification suite is unavailable.",
            details=exc.diagnostics,
            contract=contract,
            mode=("offline_evidence_evaluation" if args.observations_json else "live"),
        )
    except CertificationContractValidationError as exc:
        execution = _typed_failure_execution(
            code="operational_certification_contract_invalid",
            message="The represented certification contract is invalid.",
            details=exc.to_dict(),
            contract=contract,
            mode=("offline_evidence_evaluation" if args.observations_json else "live"),
        )
    except Exception as exc:
        execution = _typed_failure_execution(
            code="operational_certification_execution_failed",
            message="Operational certification stopped at a typed execution blocker.",
            details={"error_type": type(exc).__name__, "error": str(exc)},
            contract=contract,
            mode=("offline_evidence_evaluation" if args.observations_json else "live"),
        )
    markdown = _render_markdown(execution)
    print(markdown, end="")
    output_stem = (
        _text(execution.get("experiment_run_id"))
        or _text(execution.get("campaign_execution_id"))
        or f"operational-certification-{stable_payload_digest(execution)[:12]}"
    )
    safe_output_stem = _safe_artifact_stem(output_stem)
    output_dir = PROJECT_ROOT / "artifacts" / "operational_certification"
    output_json = args.output_json or str(output_dir / f"{safe_output_stem}.json")
    output_markdown = args.output_markdown or str(output_dir / f"{safe_output_stem}.md")
    execution["report_artifacts"] = {
        "json_path": output_json,
        "markdown_path": output_markdown,
    }
    execution["execution_sha256"] = stable_payload_digest(
        {key: value for key, value in execution.items() if key != "execution_sha256"}
    )
    _write_json(output_json, _artifact_safe_execution_projection(execution))
    target = Path(output_markdown)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return (
        0
        if _mapping(execution.get("release_eligibility")).get("eligible") is True
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
