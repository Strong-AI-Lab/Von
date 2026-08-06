from __future__ import annotations

import json
import re
from typing import Any

import pytest

from src.backend.services import (
    context_bundle_benchmark_service,
    context_grounded_answering_benchmark_service,
    workflow_selector_benchmark_service,
)
from tests.backend.benchmark_suite_test_helpers import (
    represented_suite_case_set_loader,
)


def _terminal_receipt(
    *,
    outcome: str,
    causal_stage: str,
    cause_code: str | None,
    retryability: str,
    recovery_affordances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "terminal_outcome_receipt.v1",
        "profile_concept_id": "#V#terminal_outcome_receipt",
        "outcome": outcome,
        "cause_code": cause_code,
        "causal_stage": causal_stage,
        "summary": f"Represented outcome: {outcome}.",
        "evidence_refs": [{"source": "test", "ref": f"evidence-{outcome}"}],
        "committed_effects": [],
        "remaining_obligations": (
            [] if outcome == "verified_success" else [{"effect_id": "effect-1"}]
        ),
        "retryability": retryability,
        "recovery_affordances": recovery_affordances or [],
        "learning_candidate": None,
        "redaction_status": "safe_projection",
        "provenance": {"decision_source": "represented_llm"},
    }


@pytest.fixture(autouse=True)
def _use_represented_benchmark_suites(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (
        context_bundle_benchmark_service,
        context_grounded_answering_benchmark_service,
        workflow_selector_benchmark_service,
    ):
        monkeypatch.setattr(
            module,
            "load_benchmark_suite_case_set",
            represented_suite_case_set_loader,
        )


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def sort(self, field: str, direction: Any):
        reverse = False
        try:
            reverse = int(direction) < 0
        except (TypeError, ValueError):
            reverse = False
        self._docs.sort(key=lambda doc: str(doc.get(field) or ""), reverse=reverse)
        return self

    def skip(self, n: int):
        self._docs = self._docs[int(n) :]
        return self

    def limit(self, n: int):
        self._docs = self._docs[: int(n)]
        return self

    def __iter__(self):
        return iter(self._docs)


class _TurnExecutionCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False

        decision_filter = query.get("completion_gate.decision")
        decision = (doc.get("completion_gate") or {}).get("decision")
        if isinstance(decision_filter, str):
            if decision != decision_filter:
                return False
        elif isinstance(decision_filter, dict):
            options = decision_filter.get("$in")
            if isinstance(options, list) and decision not in options:
                return False

        workflow_filter = query.get("workflow_selection.selected_workflow_id")
        workflow_id = (doc.get("workflow_selection") or {}).get("selected_workflow_id")
        if isinstance(workflow_filter, str) and workflow_id != workflow_filter:
            return False

        requires_follow_up = query.get("completion_gate.requires_follow_up")
        if (
            isinstance(requires_follow_up, bool)
            and bool((doc.get("completion_gate") or {}).get("requires_follow_up"))
            != requires_follow_up
        ):
            return False

        prompt_filter = query.get("prompt.preview")
        if isinstance(prompt_filter, dict):
            pattern = prompt_filter.get("$regex")
            flags = prompt_filter.get("$options")
            preview = (doc.get("prompt") or {}).get("preview") or ""
            if not isinstance(pattern, str):
                return False
            regex_flags = re.IGNORECASE if flags == "i" else 0
            if not re.search(pattern, str(preview), regex_flags):
                return False

        created_at_filter = query.get("created_at_utc")
        if isinstance(created_at_filter, dict):
            created_at = doc.get("created_at_utc")
            if not isinstance(created_at, str):
                return False
            gte = created_at_filter.get("$gte")
            if isinstance(gte, str) and created_at < gte:
                return False
            lte = created_at_filter.get("$lte")
            if isinstance(lte, str) and created_at > lte:
                return False

        request_id = query.get("request_id")
        if isinstance(request_id, str) and doc.get("request_id") != request_id:
            return False

        session_id = query.get("session_id")
        return not (
            isinstance(session_id, str) and doc.get("session_id") != session_id
        )

    def find(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        docs = [doc for doc in self._docs if self._matches(doc, query)]
        return _Cursor(docs)

    def find_one(
        self, query: dict[str, Any], _projection: dict[str, Any] | None = None
    ):
        for doc in self.find(query, _projection):
            return doc
        return None

    def count_documents(self, query: dict[str, Any]) -> int:
        return len(list(self.find(query)))

    def aggregate(self, pipeline: list[dict[str, Any]]):
        match = {}
        for stage in pipeline:
            if "$match" in stage and isinstance(stage["$match"], dict):
                match = stage["$match"]
                break
        docs = [doc for doc in self._docs if self._matches(doc, match)]

        counts: dict[str, int] = {}
        for doc in docs:
            decision = (doc.get("completion_gate") or {}).get("decision") or "unknown"
            key = str(decision)
            counts[key] = counts.get(key, 0) + 1
        return [{"_id": key, "count": value} for key, value in counts.items()]


class _EpisodeCritiqueCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False

        request_filter = query.get("request_id")
        request_id = doc.get("request_id")
        if isinstance(request_filter, str) and request_id != request_filter:
            return False
        if isinstance(request_filter, dict):
            options = request_filter.get("$in")
            if isinstance(options, list) and request_id not in options:
                return False

        workflow_filter = query.get("workflow_id")
        if (
            isinstance(workflow_filter, str)
            and doc.get("workflow_id") != workflow_filter
        ):
            return False

        episode_filter = query.get("episode_id")
        if isinstance(episode_filter, str) and doc.get("episode_id") != episode_filter:
            return False

        verdict_filter = query.get("verdict")
        if isinstance(verdict_filter, str) and doc.get("verdict") != verdict_filter:
            return False
        if isinstance(verdict_filter, dict):
            options = verdict_filter.get("$in")
            if isinstance(options, list) and doc.get("verdict") not in options:
                return False

        created_at_filter = query.get("created_at_utc")
        if isinstance(created_at_filter, dict):
            created_at = doc.get("created_at_utc")
            if not isinstance(created_at, str):
                return False
            gte = created_at_filter.get("$gte")
            if isinstance(gte, str) and created_at < gte:
                return False
            lte = created_at_filter.get("$lte")
            if isinstance(lte, str) and created_at > lte:
                return False

        return True

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        docs = [doc for doc in self._docs if self._matches(doc, query)]
        if not isinstance(projection, dict):
            return _Cursor(docs)

        include_keys = {key for key, include in projection.items() if include}
        projected_docs: list[dict[str, Any]] = []
        for doc in docs:
            projected: dict[str, Any] = {}
            for key in include_keys:
                if key == "_id":
                    continue
                if key in doc:
                    projected[key] = doc[key]
            projected_docs.append(projected)
        return _Cursor(projected_docs)

    def count_documents(self, query: dict[str, Any]) -> int:
        return len(list(self.find(query)))


class _ChatHistoryCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False

        session_id_filter = query.get("session_id")
        session_id = doc.get("session_id")
        if isinstance(session_id_filter, str):
            if session_id != session_id_filter:
                return False
        elif isinstance(session_id_filter, dict):
            options = session_id_filter.get("$in")
            if isinstance(options, list) and session_id not in options:
                return False

        return True

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        docs = [doc for doc in self._docs if self._matches(doc, query)]
        if not isinstance(projection, dict):
            return _Cursor(docs)

        include_keys = {key for key, include in projection.items() if include}
        projected_docs: list[dict[str, Any]] = []
        for doc in docs:
            projected_doc: dict[str, Any] = {}
            for key in include_keys:
                if key in doc:
                    projected_doc[key] = doc[key]
            projected_docs.append(projected_doc)
        return _Cursor(projected_docs)


class _DiagnosticsChatHistoryCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def find_one(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        namespace = query.get("namespace")
        history_filter = query.get("history")
        elem_match = (
            history_filter.get("$elemMatch")
            if isinstance(history_filter, dict)
            else None
        )
        requested_role = (
            elem_match.get("role") if isinstance(elem_match, dict) else None
        )
        requested_request_id = (
            elem_match.get("llm_debug_data.request_id")
            if isinstance(elem_match, dict)
            else None
        )

        for doc in self._docs:
            if isinstance(namespace, str) and doc.get("namespace") != namespace:
                continue

            matched = False
            for entry in doc.get("history") or []:
                if not isinstance(entry, dict):
                    continue
                if (
                    isinstance(requested_role, str)
                    and entry.get("role") != requested_role
                ):
                    continue
                llm_debug = entry.get("llm_debug_data")
                debug_request_id = (
                    llm_debug.get("request_id") if isinstance(llm_debug, dict) else None
                )
                if (
                    isinstance(requested_request_id, str)
                    and debug_request_id != requested_request_id
                ):
                    continue
                matched = True
                break

            if not matched:
                continue

            if not isinstance(projection, dict):
                return doc

            include_keys = {
                key for key, include in projection.items() if include and key != "_id"
            }
            projected_doc: dict[str, Any] = {}
            for key in include_keys:
                if key in doc:
                    projected_doc[key] = doc[key]
            return projected_doc

        return None


class _DB:
    def __init__(self, collections: dict[str, Any]):
        self._collections = dict(collections)

    def __getitem__(self, key: str):
        return self._collections[key]


def _build_gateway():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _install_minimal_imposition_profile_loader(monkeypatch) -> None:
    profile = {
        "profile_concept_id": "#V#minimal_imposition_benchmark_profile_autopilot_v1",
        "profile_id": "autopilot_minimal_imposition_v1",
        "description": "Test minimal-imposition profile",
        "benchmark_surface_ids": [
            "turn_execution_build_benchmark",
            "turn_execution_build_dashboard",
        ],
        "composite_policy": {"policy_version": "minimal_imposition_cost.v1"},
        "dimensions": [
            {
                "dimension_id": "user_interruption_burden",
                "title": "User interruption burden",
                "formula_id": "turn_follow_up_rate_pct",
                "weight": 0.35,
                "evidence_kind": "direct",
                "ideal_max_pct": 5.0,
                "warning_max_pct": 15.0,
                "fail_max_pct": 30.0,
            },
            {
                "dimension_id": "unsafe_under_escalation",
                "title": "Unsafe under-escalation",
                "formula_id": "turn_false_success_rate_pct",
                "weight": 0.35,
                "evidence_kind": "direct",
                "ideal_max_pct": 0.0,
                "warning_max_pct": 1.0,
                "fail_max_pct": 5.0,
            },
            {
                "dimension_id": "workflow_process_disruption",
                "title": "Workflow/process disruption",
                "formula_id": "combined_workflow_disruption_rate_pct",
                "weight": 0.2,
                "evidence_kind": "direct",
                "ideal_max_pct": 5.0,
                "warning_max_pct": 12.0,
                "fail_max_pct": 25.0,
            },
            {
                "dimension_id": "epistemic_intrusion",
                "title": "Epistemic intrusion",
                "formula_id": "missing_epistemic_intrusion_telemetry",
                "weight": 0.1,
                "evidence_kind": "missing",
                "ideal_max_pct": 0.0,
                "warning_max_pct": 0.0,
                "fail_max_pct": 0.0,
            },
        ],
        "scenario_families": [
            {"scenario_id": "low_risk_additive_internal_write"},
            {"scenario_id": "destructive_action"},
        ],
    }
    monkeypatch.setattr(
        "src.backend.services.minimal_imposition_benchmark_service.load_minimal_imposition_benchmark_profile",
        lambda **_: (
            profile,
            {"loaded_profile_concept_id": profile["profile_concept_id"]},
        ),
    )


def _build_hesitancy_trace_docs() -> list[dict[str, Any]]:
    return [
        {
            "request_id": "req-hes-1",
            "session_id": "chat-hes-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-20T00:00:00Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
                "evidence_payload": {
                    "terminal_outcome": "retrying",
                    "repeat_iteration": True,
                },
            },
            "completion_gate_repeat_iteration": True,
            "completion_gate_loop_attempts": 1,
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "prompt": {"preview": "JVNAUTOSCI-1326 hesitancy trace turn 1"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="recoverable_failure",
                causal_stage="execution",
                cause_code="required_action_missing",
                retryability="now",
                recovery_affordances=[{"action_type": "retry"}],
            ),
        },
        {
            "request_id": "req-hes-2",
            "session_id": "chat-hes-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-20T00:00:15Z",
            "completion_gate": {
                "decision": "partial",
                "decision_reason": "Mutation execution observed but verification is inconclusive.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_2"],
                "evidence_payload": {
                    "terminal_outcome": "stall_latency_budget_exhausted",
                    "repeat_iteration": False,
                    "repeat_stop_reason": "stall_latency_budget_exhausted",
                    "escalation_signal": True,
                    "escalation_reason": "stall_latency_budget_exhausted",
                },
            },
            "completion_gate_repeat_iteration": False,
            "completion_gate_loop_attempts": 2,
            "completion_gate_loop_stop_reason": "stall_latency_budget_exhausted",
            "completion_gate_escalation_signal": True,
            "completion_gate_escalation_reason": "stall_latency_budget_exhausted",
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
            "prompt": {"preview": "JVNAUTOSCI-1326 hesitancy trace turn 2"},
            "critic": {"summary": {"inconclusive_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="verified_partial",
                causal_stage="verification",
                cause_code="verification_inconclusive",
                retryability="now",
                recovery_affordances=[{"action_type": "inspect"}],
            ),
        },
        {
            "request_id": "req-hes-3",
            "session_id": "chat-hes-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-20T00:00:35Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
                "evidence_payload": {"terminal_outcome": "completed"},
            },
            "completion_gate_repeat_iteration": False,
            "completion_gate_loop_attempts": 0,
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "required_effects": [],
            "prompt": {"preview": "JVNAUTOSCI-1326 hesitancy trace turn 3"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": True,
            },
        },
    ]


def _build_corrective_evidence_failure_docs(
    *,
    follow_up_tool: str,
) -> list[dict[str, Any]]:
    return [
        {
            "request_id": "req-corr-1",
            "session_id": "chat-corr-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-04-12T07:44:21Z",
            "completion_gate": {
                "decision": "partial",
                "decision_reason": "Workflow misrouted and stronger verification was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_corr_1"],
            },
            "required_effects": [{"effect_id": "effect_corr_1", "status": "satisfied"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "rag_default",
            },
            "workflow_routing_diagnostics": {
                "selector": {
                    "selected_model_candidate": {
                        "concept_id": "#V#specialised_vontology_search_workflow"
                    }
                },
                "dispatch": {
                    "dispatch_workflow_id": "#V#chat_assistant_workflow",
                    "dispatch_terminal_status": "follow_up_required",
                },
            },
            "execution": {
                "tool_invocations": [
                    {
                        "tool": follow_up_tool,
                        "status": "ok",
                    }
                ]
            },
            "prompt": {
                "preview": "Yuchen Su for example is an instance of #V#current_uo_asail_ph_d_student"
            },
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": False,
            },
            "execution_correctness": {
                "overall_outcome": "tool_or_workflow_misrouting",
                "failure_mode": "mutation_failed_or_blocked",
                "likely_failure_to_act": True,
                "metric_labels": {
                    "successful_completion": False,
                    "false_success": False,
                    "unresolved_follow_up_needed": True,
                    "tool_or_workflow_misrouting": True,
                    "abstain_escalate_no_safe_route": False,
                },
            },
        }
    ]


def _build_corrective_evidence_follow_up_docs() -> list[dict[str, Any]]:
    return [
        {
            "request_id": "req-corr-family-1",
            "session_id": "chat-corr-family-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-04-12T07:44:21Z",
            "completion_gate": {
                "decision": "partial",
                "decision_reason": "Workflow misrouted and stronger verification was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_corr_family_1"],
            },
            "required_effects": [
                {"effect_id": "effect_corr_family_1", "status": "satisfied"}
            ],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "rag_default",
            },
            "workflow_routing_diagnostics": {
                "selector": {
                    "selected_model_candidate": {
                        "concept_id": "#V#specialised_vontology_search_workflow"
                    }
                },
                "dispatch": {
                    "dispatch_workflow_id": "#V#chat_assistant_workflow",
                    "dispatch_terminal_status": "follow_up_required",
                },
            },
            "execution": {
                "tool_invocations": [
                    {
                        "tool": "concept_exists",
                        "status": "ok",
                    }
                ]
            },
            "prompt": {
                "preview": "Verify whether Su Yuchen is represented and whether the student relation needs correction."
            },
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": False,
            },
            "execution_correctness": {
                "overall_outcome": "tool_or_workflow_misrouting",
                "failure_mode": "mutation_failed_or_blocked",
                "likely_failure_to_act": True,
                "metric_labels": {
                    "successful_completion": False,
                    "false_success": False,
                    "unresolved_follow_up_needed": True,
                    "tool_or_workflow_misrouting": True,
                    "abstain_escalate_no_safe_route": False,
                },
            },
        },
        {
            "request_id": "req-corr-family-2",
            "session_id": "chat-corr-family-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-04-12T07:45:55Z",
            "completion_gate": {
                "decision": "partial",
                "decision_reason": "Dispatch still diverged, but the follow-up used stronger relation verification.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_corr_family_2"],
            },
            "required_effects": [
                {"effect_id": "effect_corr_family_2", "status": "satisfied"}
            ],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "rag_default",
            },
            "workflow_routing_diagnostics": {
                "selector": {
                    "selected_model_candidate": {
                        "concept_id": "#V#specialised_vontology_search_workflow"
                    }
                },
                "dispatch": {
                    "dispatch_workflow_id": "#V#chat_assistant_workflow",
                    "dispatch_terminal_status": "follow_up_required",
                },
            },
            "execution": {
                "tool_invocations": [
                    {
                        "tool": "fetch_concept_content",
                        "status": "ok",
                    }
                ]
            },
            "prompt": {
                "preview": "No, stronger evidence: Yuchen Su is an instance of the current UoA SAIL PhD student concept, so verify the relation."
            },
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": False,
            },
            "execution_correctness": {
                "overall_outcome": "tool_or_workflow_misrouting",
                "failure_mode": "mutation_failed_or_blocked",
                "likely_failure_to_act": True,
                "metric_labels": {
                    "successful_completion": False,
                    "false_success": False,
                    "unresolved_follow_up_needed": True,
                    "tool_or_workflow_misrouting": True,
                    "abstain_escalate_no_safe_route": False,
                },
            },
        },
    ]


def _build_dashboard_trace_docs() -> list[dict[str, Any]]:
    return [
        {
            "request_id": "req-dash-1",
            "session_id": "chat-dash-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-20T00:00:00Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "workflow_routing_diagnostics": {
                "schema_version": "workflow_routing_diagnostics.v1",
                "dispatch": {
                    "selected_execution_mode": "tool_pipeline",
                    "pre_dispatch": {
                        "step_count": 2,
                        "completed_step_count": 2,
                        "failed_step_count": 0,
                        "total_duration_ms": 18,
                        "slowest_step_id": "selector_candidate_preparation",
                        "slowest_step_label": "Prepare selector candidates",
                        "slowest_step_duration_ms": 11,
                        "steps": [
                            {
                                "step_id": "workflow_model_policy",
                                "step_label": "Load routing model policy",
                                "status": "completed",
                                "duration_ms": 7,
                            },
                            {
                                "step_id": "selector_candidate_preparation",
                                "step_label": "Prepare selector candidates",
                                "status": "completed",
                                "duration_ms": 11,
                            },
                        ],
                    },
                },
            },
            "prompt": {"preview": "JVNAUTOSCI-1429 pre-dispatch gap example"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-dash-2",
            "session_id": "chat-dash-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-21T00:10:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                "selector_verdict": "rag_selected",
            },
            "workflow_routing_diagnostics": {
                "schema_version": "workflow_routing_diagnostics.v1",
                "dispatch": {
                    "selected_execution_mode": "custom_workflow",
                    "dispatch_terminal_status": "failed",
                    "dispatch_terminal_failure_reason": "metadata_validation_failed",
                    "zero_tool_reason_code": (
                        "custom_workflow_failed_before_tool_invocation"
                    ),
                    "pre_dispatch": {
                        "step_count": 2,
                        "completed_step_count": 2,
                        "failed_step_count": 0,
                        "total_duration_ms": 33,
                        "slowest_step_id": "selector_candidate_preparation",
                        "slowest_step_label": "Prepare selector candidates",
                        "slowest_step_duration_ms": 20,
                        "steps": [
                            {
                                "step_id": "workflow_model_policy",
                                "step_label": "Load routing model policy",
                                "status": "completed",
                                "duration_ms": 13,
                            },
                            {
                                "step_id": "selector_candidate_preparation",
                                "step_label": "Prepare selector candidates",
                                "status": "completed",
                                "duration_ms": 20,
                            },
                        ],
                    },
                },
            },
            "prompt": {"preview": "Materialise the onboarding workflow"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="verified_success",
                causal_stage="not_applicable",
                cause_code=None,
                retryability="not_applicable",
            ),
        },
        {
            "request_id": "req-dash-3",
            "session_id": "chat-dash-3",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-21T00:20:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "workflow_routing_diagnostics": {
                "schema_version": "workflow_routing_diagnostics.v1",
                "dispatch": {
                    "selected_execution_mode": "tool_pipeline",
                    "dispatch_terminal_status": "completed",
                    "pre_dispatch": {
                        "step_count": 2,
                        "completed_step_count": 2,
                        "failed_step_count": 0,
                        "total_duration_ms": 12,
                        "slowest_step_id": "selector_candidate_preparation",
                        "slowest_step_label": "Prepare selector candidates",
                        "slowest_step_duration_ms": 7,
                        "steps": [
                            {
                                "step_id": "workflow_model_policy",
                                "step_label": "Load routing model policy",
                                "status": "completed",
                                "duration_ms": 5,
                            },
                            {
                                "step_id": "selector_candidate_preparation",
                                "step_label": "Prepare selector candidates",
                                "status": "completed",
                                "duration_ms": 7,
                            },
                        ],
                    },
                },
            },
            "prompt": {"preview": "Update the workflow metadata"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
        },
    ]


def test_rag_list_indexed_supports_turn_execution_records(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-1",
            "session_id": "chat-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "workflow_routing_diagnostics": {
                "schema_version": "workflow_routing_diagnostics.v1",
                "dispatch": {
                    "selected_execution_mode": "tool_pipeline",
                    "failure_codes": ["tool_dispatch_handoff_zero_execution"],
                },
            },
            "prompt": {"preview": "Those relations were not added."},
            "critic": {"summary": {"not_verified_count": 1}},
        },
        {
            "request_id": "req-2",
            "session_id": "chat-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:01:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the status?"},
            "critic": {"summary": {"not_verified_count": 0}},
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._rag_list_indexed(
        namespace="#V#user@org",
        collection="turn_execution_records",
        decision="escalation_required",
        limit=10,
        offset=0,
    )

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    assert result["provenance"]["item_kind"] == "turn_execution_record_list"
    assert result["provenance"]["source_system"] == "mongo.turn_execution_records"
    assert result["decision_counts"]["escalation_required"] == 1
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["request_id"] == "req-1"
    assert item["decision"] == "escalation_required"
    assert item["unresolved_effect_count"] == 1
    assert item["overall_outcome"] == "unresolved_follow_up_needed"
    assert item["failure_mode"] == "mutation_not_executed"
    assert (
        item["execution_correctness"]["metric_labels"]["unresolved_follow_up_needed"]
        is True
    )
    assert item["item_kind"] == "turn_execution_record"
    assert item["source_system"] == "mongo.turn_execution_records"
    assert item["workflow_routing_diagnostics"]["dispatch"]["failure_codes"] == [
        "tool_dispatch_handoff_zero_execution"
    ]


def test_rag_get_item_supports_turn_execution_records(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    doc = {
        "request_id": "req-9",
        "session_id": "chat-9",
        "namespace": "#V#user@org",
        "created_at_utc": "2026-02-19T00:59:00Z",
        "updated_at_utc": "2026-02-19T01:00:00Z",
        "completion_gate": {
            "decision": "partial",
            "decision_reason": "Mutation execution observed but verification is inconclusive.",
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
            "blocking_effect_ids": ["effect_2"],
        },
        "workflow_selection": {
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "tool_seeking",
        },
        "workflow_routing_diagnostics": {
            "schema_version": "workflow_routing_diagnostics.v1",
            "selector": {
                "prompt": {"text": "Select workflow", "char_count": 15},
                "response": {
                    "text": "#V#tool_calling_workflow",
                    "char_count": 24,
                },
            },
            "dispatch": {
                "selected_execution_mode": "tool_pipeline",
                "last_successful_boundary": "workflow_handoff",
            },
        },
        "prompt": {"preview": "Proceed with predicates"},
        "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
        "postcondition_checks": [
            {"check_id": "check_effect_2", "status": "inconclusive"}
        ],
        "critic": {"summary": {"inconclusive_count": 1}},
        "late_effect_observations": [
            {
                "schema_version": "late_effect_observation.v1",
                "observation_id": "late-observation-9",
                "effect_id": "effect_2",
                "execution_id": "mcp_late_9",
                "capability_name": "add_relationship",
                "outcome": "late_success",
                "effect_status": "succeeded",
                "changed": True,
                "observed_at_utc": "2026-02-19T01:00:02Z",
                "storage_transformed": True,
                "payload": {
                    "success": True,
                    "effect_status": "succeeded",
                    "changed": True,
                    "private_detail": "not part of the actor receipt projection",
                },
            }
        ],
        "effect_observation_journal": {
            "effect_2": {
                "identity": {
                    "schema_version": "effect_observation_journal.v1",
                    "effect_id": "effect_2",
                    "call_id": "call-effect-2",
                    "capability_name": "add_relationship",
                    "created_at_utc": "2026-02-19T01:00:00Z",
                },
                "dispatch_intent": {
                    "phase": "dispatch_intent",
                    "dispatch_state": "intent_recorded",
                    "recorded_at_utc": "2026-02-19T01:00:00Z",
                },
                "turn_terminal": {
                    "phase": "turn_terminal",
                    "effect_status": "indeterminate",
                    "changed": None,
                    "recorded_at_utc": "2026-02-19T01:00:01Z",
                    "transport": {
                        "execution_id": "mcp_late_9",
                        "outcome": "timed_out",
                    },
                    "receipt": {"mutation_outcome": "unknown"},
                },
                "late_terminal": {
                    "phase": "late_terminal",
                    "outcome": "late_success",
                    "effect_status": "succeeded",
                    "changed": True,
                    "recorded_at_utc": "2026-02-19T01:00:02Z",
                    "payload": {
                        "success": True,
                        "effect_status": "succeeded",
                        "changed": True,
                        "private_detail": "not projected",
                    },
                },
            },
            "effect_unresolved": {
                "identity": {
                    "schema_version": "effect_observation_journal.v1",
                    "effect_id": "effect_unresolved",
                },
                "turn_terminal": {
                    "phase": "turn_terminal",
                    "effect_status": "indeterminate",
                    "receipt": {"mutation_outcome": "unknown"},
                },
            },
            "effect_missing_late_payload": {
                "identity": {
                    "schema_version": "effect_observation_journal.v1",
                    "effect_id": "effect_missing_late_payload",
                },
                "late_terminal": {
                    "phase": "late_terminal",
                    "outcome": "late_success",
                    "effect_status": "succeeded",
                    "changed": True,
                },
            },
            "effect_missing_turn_receipt": {
                "identity": {
                    "schema_version": "effect_observation_journal.v1",
                    "effect_id": "effect_missing_turn_receipt",
                },
                "turn_terminal": {
                    "phase": "turn_terminal",
                    "effect_status": "succeeded",
                    "changed": True,
                    "transport": {"outcome": "completed"},
                },
            },
        },
    }

    coll = _TurnExecutionCollection([doc])
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._rag_get_item(
        namespace="#V#user@org",
        collection="turn_execution_records",
        session_id="req-9",
    )

    assert result["success"] is True
    assert result["request_id"] == "req-9"
    assert result["decision"] == "partial"
    assert result["requires_follow_up"] is True
    assert result["overall_outcome"] == "unresolved_follow_up_needed"
    assert result["failure_mode"] == "postcondition_inconclusive"
    assert (
        result["execution_correctness"]["metric_labels"]["unresolved_follow_up_needed"]
        is True
    )
    assert result["item_kind"] == "turn_execution_record"
    assert result["provenance"]["item_kind"] == "turn_execution_record_item"
    assert result["workflow_routing_diagnostics"]["selector"]["response"]["text"] == (
        "#V#tool_calling_workflow"
    )
    assert (
        result["workflow_routing_diagnostics"]["dispatch"]["last_successful_boundary"]
        == "workflow_handoff"
    )
    assert result["late_effect_observation_count"] == 1
    assert result["late_effect_observations_truncated"] is False
    assert result["late_effect_observations"] == [
        {
            "schema_version": "late_effect_observation.v1",
            "observation_id": "late-observation-9",
            "effect_id": "effect_2",
            "execution_id": "mcp_late_9",
            "capability_name": "add_relationship",
            "outcome": "late_success",
            "effect_status": "succeeded",
            "changed": True,
            "observed_at_utc": "2026-02-19T01:00:02Z",
            "storage_transformed": True,
            "receipt": {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
            },
        }
    ]
    assert result["effect_observation_journal_count"] == 4
    assert result["effect_observation_journal_truncated"] is False
    journal_by_id = {
        item["effect_id"]: item
        for item in result["effect_observation_journal"]
    }
    assert journal_by_id["effect_2"]["latest_phase"] == "late_terminal"
    assert journal_by_id["effect_2"]["outcome_resolved"] is True
    assert journal_by_id["effect_2"]["late_terminal"]["receipt"] == {
        "success": True,
        "effect_status": "succeeded",
        "changed": True,
    }
    assert "private_detail" not in json.dumps(journal_by_id["effect_2"])
    assert journal_by_id["effect_unresolved"]["outcome_resolved"] is False
    assert (
        journal_by_id["effect_missing_late_payload"]["outcome_resolved"]
        is False
    )
    assert (
        journal_by_id["effect_missing_turn_receipt"]["outcome_resolved"]
        is False
    )


def test_rag_get_item_does_not_expose_late_effects_across_namespaces(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    coll = _TurnExecutionCollection(
        [
                {
                    "request_id": "req-private-late-effect",
                    "session_id": "chat-private-late-effect",
                    "namespace": "#V#user@org_a",
                "late_effect_observations": [
                    {
                        "observation_id": "private-observation",
                        "effect_id": "private-effect",
                        "outcome": "late_success",
                    }
                ],
                "effect_observation_journal": {
                    "private-effect": {
                        "dispatch_intent": {
                            "phase": "dispatch_intent",
                        }
                    }
                },
            }
        ]
    )
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._rag_get_item(
        namespace="#V#user@org_b",
        collection="turn_execution_records",
        session_id="req-private-late-effect",
    )

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert "late_effect_observations" not in result
    assert "effect_observation_journal" not in result


def test_rag_list_indexed_supports_episode_critique_improvement_suggestion_summary(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    coll = _EpisodeCritiqueCollection(
        [
            {
                "memory_id": "#V#episode_critique_memory_1",
                "request_id": "req-1839-a",
                "episode_id": "ep-1839-a",
                "workflow_id": "#V#specialised_vontology_search_workflow",
                "created_at_utc": "2026-04-12T07:50:00Z",
                "updated_at_utc": "2026-04-12T07:51:00Z",
                "namespace": "#V#user@org",
                "verdict": "fail",
                "critic_summary_text": "Dispatch collapsed and the follow-up action stayed too weak.",
                "confidence": 0.89,
                "unresolved_check_count": 2,
                "implicated_tool_names": ["concept_exists"],
                "implicated_concept_ids": ["#V#yuchen_su"],
                "routing_decision": "create_task",
                "routing_reason_codes": ["repeat_threshold_met"],
                "routing_fingerprint": "fp-1839-a",
                "routing_repeat_count": 1,
                "routing_task_action": "created_task",
                "routing_jira_action": None,
                "remediation_task_ids": ["#V#task_1839"],
                "remediation_issue_keys": ["JVNAUTOSCI-1839"],
                "improvement_suggestion_count": 1,
                "improvement_suggestion_categories": ["workflow_change"],
                "improvement_target_workflow_ids": [
                    "#V#specialised_vontology_search_workflow"
                ],
                "improvement_target_tool_names": ["fetch_concept_content"],
            }
        ]
    )
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"episode_critique_memories": coll}),
    )

    result = cat._rag_list_indexed(
        namespace="#V#user@org",
        collection="episode_critique_memories",
        limit=10,
        offset=0,
    )

    assert result["success"] is True
    assert result["collection"] == "episode_critique_memories"
    assert result["provenance"]["item_kind"] == "episode_critique_memory_list"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["memory_id"] == "#V#episode_critique_memory_1"
    assert item["critic_summary_text"] == (
        "Dispatch collapsed and the follow-up action stayed too weak."
    )
    assert item["improvement_suggestion_count"] == 1
    assert item["improvement_suggestion_categories"] == ["workflow_change"]
    assert item["improvement_target_workflow_ids"] == [
        "#V#specialised_vontology_search_workflow"
    ]
    assert item["improvement_target_tool_names"] == ["fetch_concept_content"]


def test_turn_execution_list_includes_rag_indexing_state_from_chat_history(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-indexed-1",
            "session_id": "chat-indexed-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:20:00Z",
            "completion_gate": {"decision": "completed", "requires_follow_up": False},
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow"
            },
            "prompt": {"preview": "All done"},
            "critic": {"summary": {"not_verified_count": 0}},
        },
        {
            "request_id": "req-partial-1",
            "session_id": "chat-partial-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:21:00Z",
            "completion_gate": {"decision": "partial", "requires_follow_up": True},
            "required_effects": [{"effect_id": "effect_1", "status": "satisfied"}],
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "prompt": {"preview": "Attempted write"},
            "critic": {"summary": {"inconclusive_count": 1}},
        },
    ]
    chat_docs = [
        {
            "session_id": "chat-indexed-1",
            "namespace": "#V#user@org",
            "history": [{"content": "A"}, {"content": "B"}],
            "rag_indexed_success": 2,
            "rag_indexed_failed": 0,
        },
        {
            "session_id": "chat-partial-1",
            "namespace": "#V#user@org",
            "history": [{"content": "A"}, {"content": "B"}, {"content": "C"}],
            "rag_indexed_success": 1,
            "rag_indexed_failed": 1,
        },
    ]

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB(
            {
                "turn_execution_records": _TurnExecutionCollection(turn_docs),
                "chat_history": _ChatHistoryCollection(chat_docs),
            }
        ),
    )

    result = cat._turn_execution_list(namespace="#V#user@org", limit=20, offset=0)

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    by_request_id = {item["request_id"]: item for item in result["items"]}

    indexed_state = by_request_id["req-indexed-1"]["rag_indexing_state"]
    assert indexed_state["status"] == "indexed"
    assert indexed_state["sync_state"] == "synchronised"
    assert indexed_state["fully_indexed"] is True
    assert indexed_state["messages_pending_indexing"] == 0

    partial_state = by_request_id["req-partial-1"]["rag_indexing_state"]
    assert partial_state["status"] == "partial"
    assert partial_state["sync_state"] == "error"
    assert partial_state["fully_indexed"] is False
    assert partial_state["messages_pending_indexing"] == 1

    assert result["rag_indexing_state_counts"]["indexed"] == 1
    assert result["rag_indexing_state_counts"]["partial"] == 1
    assert "rag_indexing_lookup_warning" not in result


def test_turn_execution_get_includes_rag_indexing_state_from_chat_history(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_doc = {
        "request_id": "req-failed-indexing",
        "session_id": "chat-failed-indexing",
        "namespace": "#V#user@org",
        "created_at_utc": "2026-02-19T01:22:00Z",
        "updated_at_utc": "2026-02-19T01:23:00Z",
        "completion_gate": {"decision": "partial", "requires_follow_up": True},
        "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
        "postcondition_checks": [{"check_id": "check_1", "status": "inconclusive"}],
        "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
        "prompt": {"preview": "Attempted update"},
        "critic": {"summary": {"inconclusive_count": 1}},
        "terminal_outcome_receipt": _terminal_receipt(
            outcome="inconclusive",
            causal_stage="unknown",
            cause_code="causal_evidence_missing",
            retryability="now",
            recovery_affordances=[{"action_type": "inspect"}],
        ),
    }
    chat_docs = [
        {
            "session_id": "chat-failed-indexing",
            "namespace": "#V#user@org",
            "history": [{"content": "A"}, {"content": "B"}],
            "rag_indexed_success": 0,
            "rag_indexed_failed": 2,
        }
    ]

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB(
            {
                "turn_execution_records": _TurnExecutionCollection([turn_doc]),
                "chat_history": _ChatHistoryCollection(chat_docs),
            }
        ),
    )

    result = cat._turn_execution_get(
        namespace="#V#user@org",
        request_id="req-failed-indexing",
    )

    assert result["success"] is True
    state = result["rag_indexing_state"]
    assert state["status"] == "indexing_failed"
    assert state["sync_state"] == "error"
    assert state["indexed"] is False
    assert state["reason_code"] == "all_indexing_attempts_failed"
    projection = result["terminal_outcome_receipt_projection"]
    assert projection["available"] is True
    assert projection["outcome"] == "inconclusive"
    assert "rag_indexing_lookup_warning" not in result


def test_turn_execution_list_reports_lookup_warning_when_chat_history_unavailable(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-warning-1",
            "session_id": "chat-warning-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:24:00Z",
            "completion_gate": {"decision": "partial", "requires_follow_up": True},
            "required_effects": [],
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "prompt": {"preview": "Attempted update"},
            "critic": {"summary": {"inconclusive_count": 1}},
        }
    ]

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": _TurnExecutionCollection(turn_docs)}),
    )

    result = cat._turn_execution_list(namespace="#V#user@org", limit=20, offset=0)

    assert result["success"] is True
    assert result["rag_indexing_lookup_warning"]["reason_code"] == (
        "chat_history_lookup_unavailable"
    )
    state = result["items"][0]["rag_indexing_state"]
    assert state["status"] == "unknown"
    assert state["reason_code"] == "chat_history_lookup_unavailable"


def test_turn_execution_list_and_get_wrappers(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-wrap-1",
            "session_id": "chat-wrap-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:00:00Z",
            "completion_gate": {
                "decision": "escalation_required",
                "requires_follow_up": True,
                "safe_to_claim_completion": False,
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "prompt": {"preview": "JVNAUTOSCI-1202: Relations were not added"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        }
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    listed = cat._turn_execution_list(namespace="#V#user@org", limit=10, offset=0)
    assert listed["success"] is True
    assert listed["collection"] == "turn_execution_records"
    assert len(listed["items"]) == 1
    assert listed["items"][0]["request_id"] == "req-wrap-1"
    assert "session_id" not in listed["items"][0]
    assert listed["items"][0]["identifier_binding"] == {
        "mode": "turn_execution_record_projection",
        "request_id_field": "request_id",
        "chat_session_id_field": "chat_session_id",
        "request_id_aliases_session_id": False,
    }

    got = cat._turn_execution_get(namespace="#V#user@org", request_id="req-wrap-1")
    assert got["success"] is True
    assert got["request_id"] == "req-wrap-1"
    assert "session_id" not in got
    assert got["identifier_binding"] == {
        "mode": "turn_execution_record_projection",
        "request_id_field": "request_id",
        "chat_session_id_field": "chat_session_id",
        "request_id_aliases_session_id": False,
    }


def test_turn_execution_get_rejects_session_id_alias(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": _TurnExecutionCollection([])}),
    )

    result = cat._turn_execution_get(namespace="#V#user@org", session_id="req-wrap-1")

    assert result["success"] is False
    assert result["error_code"] == "missing_parameter"


def test_turn_execution_get_diagnostics_returns_embedded_payload(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat
    from src.backend.services.conversation_scope_binding_service import (
        verify_conversation_scope_binding,
        verify_history_location_binding,
    )

    chat_docs = [
        {
            "user_id": "#V#user",
            "session_id": "chat-diag-1",
            "namespace": "#V#user@org",
            "organisation_concept_id": "#V#org",
            "history": [
                {"role": "user", "content": "Show the turn diagnostics"},
                {
                    "role": "assistant",
                    "content": "Done",
                    "timestamp": "2026-04-01T00:00:02Z",
                    "llm_debug_data": {
                        "request_id": "req-diag-1",
                        "interaction_timestamp_utc": "2026-04-01T00:00:03Z",
                        "code_version": "v20260401+g1234567",
                        "code_version_details": {
                            "schema_version": "runtime_code_version.v1",
                            "version": "v20260401+g1234567",
                        },
                        "workflow_routing_diagnostics": {
                            "schema_version": "workflow_routing_diagnostics.v1",
                            "dispatch": {"dispatch_terminal_status": "completed"},
                        },
                        "turn_execution_diagnostics": {
                            "request_id": "req-diag-1",
                            "generated_at_utc": "2026-04-01T00:00:03Z",
                            "prompt_preview": "Show the turn diagnostics",
                            "turn_context_handoff_decision": {
                                "mode": "task_state_summary",
                                "summary": "Use the current diagnostics task state.",
                                "routing_evidence_scope": "summary_only",
                                "expected_outcome_scope": "summary_only",
                                "answer_scope": "summary_only",
                            },
                            "turn_context_handoff_mode": "task_state_summary",
                            "turn_context_handoff_summary": (
                                "Use the current diagnostics task state."
                            ),
                            "turn_context_handoff_messages": [],
                            "turn_context_handoff_lineage": ["history_index:0"],
                            "turn_context_handoff_risks": [],
                            "workflow_selection": {
                                "selected_workflow_id": "#V#chat_assistant_workflow"
                            },
                            "aux_llm_calls": [
                                {
                                    "type": "workflow_execution_trace",
                                    "execution_id": "exec-diag-1",
                                    "instance_id": "#V#wf_instance_diag_1",
                                    "workflow_id": "#V#chat_assistant_workflow",
                                }
                            ],
                            "progress_events": [],
                            "activity_history": [],
                            "phase_history": [],
                            "tool_history": [],
                            "stage_diagnostics": [],
                            "workflow_stage_model": {
                                "schema_version": "conversation_turn_stage_model.v1",
                                "stages": [],
                            },
                            "workflow_stage_path": {
                                "schema_version": "conversation_turn_stage_path.v1",
                                "path": [],
                            },
                            "timing_breakdown": {
                                "schema_version": "conversation_turn_timing_breakdown.v1",
                                "stages": [],
                                "llm_calls_by_stage_model": [],
                                "totals": {
                                    "elapsed_ms": 42,
                                    "observed_timeline_ms": 42,
                                    "phase_elapsed_ms": 42,
                                    "llm_elapsed_ms": 0,
                                    "llm_call_count": 0,
                                },
                            },
                        },
                    },
                },
            ],
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_chat_history_collection_service",
        lambda read_only=True: _DiagnosticsChatHistoryCollection(chat_docs),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_records_collection",
        lambda: None,
    )

    result = cat._turn_execution_get_diagnostics(
        namespace="#V#user@org",
        request_id="req-diag-1",
    )

    assert result["success"] is True
    assert result["schema_version"] == "turn_execution_diagnostics.v1"
    assert result["request_id"] == "req-diag-1"
    assert result["chat_session_id"] == "chat-diag-1"
    assert result["history_location"] == {
        "session_id": "chat-diag-1",
        "history_index": 1,
    }
    assert result["source_system"] == "mongo.chat_history"
    assert result["workflow_routing_diagnostics"]["schema_version"] == (
        "workflow_routing_diagnostics.v1"
    )
    assert result["context_adjudication"]["mode"] == "task_state_summary"
    assert result["context_adjudication"]["summary"] == (
        "Use the current diagnostics task state."
    )
    assert result["context_adjudication"]["routing_evidence_scope"] == "summary_only"
    assert result["context_adjudication"]["lineage"] == ["history_index:0"]
    assert result["mcp_access"]["turn_execution_get_diagnostics"]["tool_name"] == (
        "turn_execution_get_diagnostics"
    )
    debug_args = result["mcp_access"]["chat_history_get_debug_entry"]["arguments"]
    assert debug_args["namespace"] == "#V#user@org"
    assert debug_args["organisation_concept_id"] == "#V#org"
    verified_history_ref = verify_history_location_binding(
        debug_args["history_location_ref"]
    )
    assert verified_history_ref["success"] is True
    assert verified_history_ref["chat_session_id"] == "chat-diag-1"
    assert verified_history_ref["history_index"] == 1

    locator_args = result["mcp_access"]["conversation_telemetry_get_locator"][
        "arguments"
    ]
    assert locator_args["namespace"] == "#V#user@org"
    assert locator_args["organisation_concept_id"] == "#V#org"
    verified_conversation_ref = verify_conversation_scope_binding(
        locator_args["conversation_ref"]
    )
    assert verified_conversation_ref["success"] is True
    assert verified_conversation_ref["chat_session_id"] == "chat-diag-1"
    assert result["mcp_access"]["workflow_execution_traces"] == [
        {
            "execution_id": "exec-diag-1",
            "instance_id": "#V#wf_instance_diag_1",
            "workflow_id": "#V#chat_assistant_workflow",
            "trace_role": "selected_workflow",
            "mcp_access": {
                "tool_name": "workflow_get_execution_trace",
                "arguments": {
                    "execution_id": "exec-diag-1",
                    "instance_id": "#V#wf_instance_diag_1",
                },
                "purpose": "Fetch the durable workflow execution trace referenced by this turn.",
            },
        }
    ]
    assert result["provenance"]["item_kind"] == "turn_execution_diagnostics_item"
    assert result["provenance"]["source_system"] == "mongo.chat_history"


def test_turn_execution_get_diagnostics_reconstructs_from_projection(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-lossy-1",
            "session_id": "chat-lossy-1",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "prompt": {"preview": "Attempted update"},
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "workflow_discovery": {"match_count": 1},
            },
            "workflow_routing_diagnostics": {
                "schema_version": "workflow_routing_diagnostics.v1",
                "dispatch": {"dispatch_terminal_status": "completed"},
            },
            "execution": {
                "tool_invocations": [
                    {"tool_name": "add_relationship", "status": "completed"}
                ],
                "workflow_stage_model": {
                    "schema_version": "conversation_turn_stage_model.v1",
                    "stages": [],
                },
                "workflow_stage_path": {
                    "schema_version": "conversation_turn_stage_path.v1",
                    "path": [],
                },
            },
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_chat_history_collection_service",
        lambda read_only=True: None,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_records_collection",
        lambda: _TurnExecutionCollection(turn_docs),
    )

    result = cat._turn_execution_get_diagnostics(
        namespace="#V#user@org",
        request_id="req-lossy-1",
    )

    assert result["success"] is True
    assert result["request_id"] == "req-lossy-1"
    assert result["source_system"] == "mongo.turn_execution_records"
    assert result["reconstruction"]["lossy"] is True
    assert result["tool_call_count"] == 1
    assert result["workflow_selection"] == {
        "selected_workflow_id": "#V#tool_calling_workflow",
        "workflow_discovery": {"match_count": 1},
    }
    assert result["workflow_stage_path"]["schema_version"] == (
        "conversation_turn_stage_path.v1"
    )


def test_turn_execution_get_prefers_identity_bearing_top_level_invocations(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-durable-identities",
            "session_id": "chat-durable-identities",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "tool_invocations": [
                {
                    "tool": "represented_workflow_test",
                    "status": "error",
                    "call_id": "call-durable-identities",
                    "execution_id": "mcp-durable-identities",
                    "effect_id": "effect-durable-identities",
                    "effect_status": "partial",
                    "error_code": "tool_timeout_after_durable_submission",
                    "capability_display_name": "Paper Representation Workflow",
                    "workflow_id": "#V#paper_workflow",
                    "evidence": {
                        "instance_id": "instance-durable-identities",
                        "durable_submission_status": "pending",
                        "provenance": {
                            "capability_kind": "represented_workflow",
                            "execution_method": "workflow_execute",
                            "represented_workflow_id": "#V#paper_workflow",
                        },
                    },
                }
            ],
            "execution": {
                "tool_invocations": [
                    {
                        "tool": "represented_workflow_test",
                        "status": "timeout",
                    }
                ]
            },
        }
    ]
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": _TurnExecutionCollection(turn_docs)}),
    )

    result = cat._turn_execution_get(
        namespace="#V#user@org",
        request_id="req-durable-identities",
    )

    assert result["success"] is True
    assert result["tool_invocation_summary"] == [
        {
            "tool": "represented_workflow_test",
            "status": "error",
            "call_id": "call-durable-identities",
            "execution_id": "mcp-durable-identities",
            "effect_id": "effect-durable-identities",
            "effect_status": "partial",
            "error_code": "tool_timeout_after_durable_submission",
            "capability_kind": "represented_workflow",
            "capability_display_name": "Paper Representation Workflow",
            "execution_method": "workflow_execute",
            "represented_workflow_id": "#V#paper_workflow",
            "workflow_id": "#V#paper_workflow",
            "instance_id": "instance-durable-identities",
            "durable_submission_status": "pending",
        }
    ]


def test_turn_execution_get_diagnostics_hydrates_empty_progress_tool_history(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    chat_docs = [
        {
            "user_id": "#V#user",
            "session_id": "chat-receipt-1",
            "namespace": "#V#user@org",
            "organisation_concept_id": "#V#org",
            "history": [
                {
                    "role": "assistant",
                    "content": "Inspect the durable workflow instance before retrying.",
                    "llm_debug_data": {
                        "request_id": "req-receipt-1",
                        "tool_invocations": [
                            {
                                "tool": "turn_capabilities",
                                "status": "ok",
                                "call_id": "call-capabilities",
                            },
                            {
                                "tool": "represented_workflow_test",
                                "status": "error",
                                "call_id": "call-workflow",
                                "effect_id": "effect-workflow",
                                "effect_status": "partial",
                                "error_code": (
                                    "tool_timeout_after_durable_submission"
                                ),
                                "capability_display_name": (
                                    "Paper Representation Workflow"
                                ),
                                "execution_id": "mcp-workflow",
                                "workflow_id": "#V#paper_workflow",
                                "instance_id": "instance-workflow",
                                "transport": {
                                    "outcome": "timed_out",
                                    "timeout_phase": "handler",
                                },
                            },
                            {
                                "tool": "turn_read_evidence",
                                "status": "ok",
                                "call_id": "call-read",
                            },
                        ],
                        "turn_execution_diagnostics": {
                            "request_id": "req-receipt-1",
                            "generated_at_utc": "2026-07-28T15:02:31Z",
                            "tool_history": [],
                            "tool_call_count": 0,
                            "tool_call_start_count": 0,
                            "tool_call_end_count": 0,
                            "tool_success_count": 0,
                            "tool_failure_count": 0,
                            "tool_pending_count": 0,
                            "progress_events": [],
                            "activity_history": [],
                            "phase_history": [],
                            "stage_diagnostics": [],
                        },
                    },
                }
            ],
        }
    ]
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_chat_history_collection_service",
        lambda read_only=True: _DiagnosticsChatHistoryCollection(chat_docs),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_records_collection",
        lambda: None,
    )

    result = cat._turn_execution_get_diagnostics(
        namespace="#V#user@org",
        request_id="req-receipt-1",
    )

    assert result["success"] is True
    assert result["tool_call_count"] == 3
    assert result["tool_call_start_count"] == 3
    assert result["tool_call_end_count"] == 3
    assert result["tool_success_count"] == 2
    assert result["tool_failure_count"] == 1
    workflow_call = result["tool_history"][1]
    assert workflow_call["tool"] == "represented_workflow_test"
    assert workflow_call["effect_status"] == "partial"
    assert workflow_call["error_code"] == (
        "tool_timeout_after_durable_submission"
    )
    assert workflow_call["execution_id"] == "mcp-workflow"
    assert workflow_call["capability_display_name"] == (
        "Paper Representation Workflow"
    )
    assert workflow_call["workflow_id"] == "#V#paper_workflow"
    assert workflow_call["instance_id"] == "instance-workflow"
    assert "effective_arguments" not in workflow_call


def test_turn_execution_list_filters_by_session_id(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-session-1",
            "session_id": "chat-session-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-04-01T00:00:01Z",
            "completion_gate": {"decision": "completed", "requires_follow_up": False},
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow"
            },
            "prompt": {"preview": "First session"},
        },
        {
            "request_id": "req-session-2",
            "session_id": "chat-session-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-04-01T00:00:02Z",
            "completion_gate": {"decision": "completed", "requires_follow_up": False},
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow"
            },
            "prompt": {"preview": "Second session"},
        },
    ]

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB(
            {
                "turn_execution_records": _TurnExecutionCollection(turn_docs),
                "chat_history": _ChatHistoryCollection([]),
            }
        ),
    )

    result = cat._turn_execution_list(
        namespace="#V#user@org",
        session_id="chat-session-2",
        limit=10,
        offset=0,
    )

    assert result["success"] is True
    assert [item["request_id"] for item in result["items"]] == ["req-session-2"]


def test_turn_execution_search_failures_reports_modes_and_recommendations(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-fail-1",
            "session_id": "chat-fail-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "JVNAUTOSCI-1202: Relations were not added"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="recoverable_failure",
                causal_stage="execution",
                cause_code="required_action_missing",
                retryability="now",
                recovery_affordances=[{"action_type": "retry"}],
            ),
        },
        {
            "request_id": "req-fail-2",
            "session_id": "chat-fail-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:00:10Z",
            "completion_gate": {
                "decision": "failed",
                "decision_reason": "Mutation attempt failed or was blocked.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_9"],
            },
            "required_effects": [{"effect_id": "effect_9", "status": "not_satisfied"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "Proceed with predicate update"},
            "critic": {"summary": {"error_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="verified_partial",
                causal_stage="verification",
                cause_code="verification_inconclusive",
                retryability="now",
                recovery_affordances=[{"action_type": "inspect"}],
            ),
        },
        {
            "request_id": "req-ok-1",
            "session_id": "chat-ok-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:10:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the current status?"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="verified_success",
                causal_stage="not_applicable",
                cause_code=None,
                retryability="not_applicable",
            ),
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_search_failures(
        namespace="#V#user@org",
        limit=20,
        offset=0,
    )

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    assert result["provenance"]["item_kind"] == "turn_execution_failure_report"
    assert result["returned_count"] == 2
    assert result["likely_failure_count"] == 2
    assert result["failure_mode_counts"]["mutation_not_executed"] == 1
    assert result["failure_mode_counts"]["mutation_failed_or_blocked"] == 1
    assert "req-fail-1" in result["example_request_ids"]
    assert "req-fail-2" in result["example_request_ids"]
    first_item = result["items"][0]
    assert isinstance(first_item.get("execution_correctness"), dict)
    assert (
        first_item["execution_correctness"]["metric_labels"][
            "unresolved_follow_up_needed"
        ]
        is True
    )
    recommendations = result["recommendations"]
    assert recommendations
    recommendation_text = " ".join(recommendations).lower()
    assert "authority" in recommendation_text
    assert "capability" in recommendation_text
    assert "execution" in recommendation_text
    assert "#v#conversation_turn_execution_workflow" not in recommendation_text


def test_turn_execution_search_failures_flags_completed_record_with_failed_dispatch(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-false-success-1",
            "session_id": "chat-false-success-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-03-31T00:18:37Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                "selector_verdict": "rag_selected",
            },
            "workflow_routing_diagnostics": {
                "dispatch": {
                    "selected_execution_mode": "custom_workflow",
                    "dispatch_terminal_status": "failed",
                    "dispatch_terminal_failure_reason": (
                        "metadata_validation_failed:"
                        "metadata_write_context_key_missing:"
                        "#V#onboarding_step_collect_student_info:"
                        "#V#workflow_context_key_validated_type_name"
                    ),
                    "zero_tool_reason_code": (
                        "custom_workflow_failed_before_tool_invocation"
                    ),
                }
            },
            "prompt": {"preview": "Show me all the SAIL PhD students"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
        }
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_search_failures(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        include_completed=True,
    )

    assert result["success"] is True
    assert result["returned_count"] == 1
    assert result["likely_failure_count"] == 1
    assert result["failure_mode_counts"]["false_completion_gate_state"] == 1
    assert "req-false-success-1" in result["example_request_ids"]
    item = result["items"][0]
    assert item["overall_outcome"] == "false_success"
    assert item["failure_mode"] == "false_completion_gate_state"
    assert item["likely_failure_to_act"] is True
    assert item["execution_correctness"]["metric_labels"]["false_success"] is True
    recommendations = result["recommendations"]
    assert recommendations
    recommendation_text = " ".join(recommendations).lower()
    assert "effects" in recommendation_text
    assert "evidence" in recommendation_text
    assert "terminal state" in recommendation_text
    assert "completion-gate" not in recommendation_text


def test_turn_execution_build_benchmark_returns_metrics_and_replay_cases(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = [
        {
            "request_id": "req-fail-1",
            "session_id": "chat-fail-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "JVNAUTOSCI-1202: Relations were not added"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="recoverable_failure",
                causal_stage="execution",
                cause_code="required_action_missing",
                retryability="now",
                recovery_affordances=[{"action_type": "retry"}],
            ),
        },
        {
            "request_id": "req-fail-2",
            "session_id": "chat-fail-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:00:10Z",
            "completion_gate": {
                "decision": "partial",
                "decision_reason": "Mutation execution observed but verification is inconclusive.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_2"],
            },
            "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "Proceed with predicate update"},
            "critic": {"summary": {"inconclusive_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="verified_partial",
                causal_stage="verification",
                cause_code="verification_inconclusive",
                retryability="now",
                recovery_affordances=[{"action_type": "inspect"}],
            ),
        },
        {
            "request_id": "req-ok-1",
            "session_id": "chat-ok-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:10:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the current status?"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="verified_success",
                causal_stage="not_applicable",
                cause_code=None,
                retryability="not_applicable",
            ),
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        max_cases=2,
    )

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    assert result["provenance"]["item_kind"] == "turn_execution_benchmark_report"

    metrics = result.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics["scanned_count"] == 3
    assert metrics["likely_failure_count"] == 2
    assert metrics["likely_failure_rate_pct"] == 66.67
    assert metrics["failure_mode_counts"]["mutation_not_executed"] == 1
    assert metrics["metric_schema"]["summary_schema_version"] == (
        "turn_execution_correctness.v1"
    )
    assert metrics["outcome_label_counts"]["successful_completion"] == 1
    assert metrics["outcome_label_counts"]["unresolved_follow_up_needed"] == 2
    assert metrics["outcome_label_rates_pct"]["successful_completion_rate_pct"] == 33.33
    receipt_metrics = metrics["terminal_outcome_receipts"]
    assert receipt_metrics["valid_receipt_coverage_pct"] == 100.0
    assert receipt_metrics["typed_non_success_coverage_pct"] == 100.0
    assert receipt_metrics["causal_stage_coverage_pct"] == 100.0
    assert receipt_metrics["recovery_affordance_coverage_pct"] == 100.0
    assert receipt_metrics["outcome_counts"] == {
        "recoverable_failure": 1,
        "verified_partial": 1,
        "verified_success": 1,
    }
    assert isinstance(result.get("benchmark_fingerprint"), str)
    assert len(result["benchmark_fingerprint"]) == 16

    replay_cases = result.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert len(replay_cases) == 2
    first_case = replay_cases[0]
    assert first_case["failure_mode"] == "mutation_not_executed"
    assert first_case["confidence"] == "high"
    assert first_case["pass_criteria"]["action_attempted"] is True
    assert first_case["pass_criteria"]["postcondition_satisfied"] is True
    assert first_case["pass_criteria"]["no_false_success"] is True
    assert first_case["evidence"]["terminal_outcome_receipt_projection"][
        "outcome"
    ] == "recoverable_failure"
    triage = first_case.get("triage")
    assert isinstance(triage, dict)
    assert "JVNAUTOSCI-1202" in triage.get("jira_issue_keys", [])
    assert "https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1202" in triage.get(
        "jira_browse_urls", []
    )

    seeded_cases = result.get("seeded_cases")
    assert replay_cases == seeded_cases

    triage_index = result.get("triage_index")
    assert isinstance(triage_index, dict)
    assert triage_index["issue_link_count"] == 1
    assert triage_index["issue_links"][0]["issue_key"] == "JVNAUTOSCI-1202"

    imposition = result.get("imposition_assessment")
    assert isinstance(imposition, dict)
    assert imposition.get("success") is True
    assert imposition.get("profile", {}).get("profile_id") == (
        "autopilot_minimal_imposition_v1"
    )
    assert imposition.get("dimension_counts", {}).get("missing_count") == 1


def test_turn_execution_build_benchmark_surfaces_discovery_timeout_learning_candidates(
    monkeypatch,
):
    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = [
        {
            "request_id": "req-timeout-grounded-1",
            "session_id": "chat-timeout-grounded-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-04-24T07:57:25Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "Workflow reported completion.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [
                {
                    "effect_id": "grounded_entity_information_evidence",
                    "effect_type": "grounded_evidence",
                    "status": "not_executed",
                }
            ],
            "workflow_selection": {
                "selected_workflow_id": "#V#entity_information_retrieval_workflow",
                "selector_verdict": "rag_selected",
                "selector_source": "selector_override",
            },
            "workflow_routing_diagnostics": {
                "discovery": {
                    "budget_exhausted": True,
                    "timeout_budget_seconds": 10.0,
                    "budget_exhaustion_stage": "workflow_discovery_for_turn",
                    "budget_exhaustion_detail": (
                        "workflow_discovery_for_turn timed out after 10.000s"
                    ),
                    "match_absence_reason": "workflow_discovery_budget_exhausted",
                    "candidate_count": 0,
                    "match_count": 0,
                    "search_time_ms": 10000.0,
                },
                "dispatch": {
                    "selected_execution_mode": "custom_workflow",
                    "dispatch_workflow_id": "#V#entity_information_retrieval_workflow",
                },
            },
            "execution": {
                "summary": {
                    "workflow_required_effects_declared_count": 1,
                    "workflow_required_effects_required_tools": [
                        "get_predicate_incidence",
                        "find_relations_with_argument",
                    ],
                },
                "tool_invocations": [],
            },
            "prompt": {"preview": "Who am I ?"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": True,
            },
        }
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    gateway = _build_gateway()
    result = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 1,
        },
    ).payload

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["workflow_discovery_metrics"]["budget_exhausted_count"] == 1
    assert metrics["workflow_discovery_metrics"]["zero_candidate_timeout_count"] == 1
    assert metrics["workflow_discovery_metrics"]["timeout_false_success_count"] == 1
    assert metrics["required_evidence_metrics"]["missing_required_tool_turn_count"] == 1
    assert metrics["outcome_label_counts"]["workflow_discovery_timeout"] == 1
    assert metrics["outcome_label_counts"]["required_evidence_missing"] == 1

    recommendation = result["workflow_discovery_timeout_policy_recommendation"]
    assert recommendation["action"] == "review_and_raise_timeout_budget"
    assert recommendation["suggested_min_timeout_seconds"] >= 15.0
    assert any(
        gap.get("gap_id") == "workflow_discovery_timeout_learning_candidates"
        for gap in result["capability_gaps"]
    )
    assert any(
        gap.get("gap_id") == "grounded_required_evidence_missing"
        for gap in result["capability_gaps"]
    )

    replay_case = result["replay_cases"][0]
    assert replay_case["evidence"]["workflow_discovery"]["budget_exhausted"] is True
    assert (
        replay_case["evidence"]["required_evidence"]["missing_required_tool_count"] == 2
    )


def test_turn_execution_build_benchmark_flags_regression_when_baseline_is_better(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = [
        {
            "request_id": "req-fail-1",
            "session_id": "chat-fail-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "Follow up on JVNAUTOSCI-1204 benchmark"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-ok-1",
            "session_id": "chat-ok-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:10:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the current status?"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        baseline_likely_failure_rate_pct=10.0,
        baseline_false_success_rate_pct=5.0,
        baseline_unresolved_follow_up_rate_pct=5.0,
        regression_tolerance_pct=0.5,
    )

    assert result["success"] is True
    regression = result.get("regression_assessment")
    assert isinstance(regression, dict)
    assert regression["baseline_provided"] is True
    assert regression["regression_detected"] is True
    comparisons = regression.get("comparisons")
    assert isinstance(comparisons, list)
    assert any(
        row.get("metric") == "likely_failure_rate_pct" and row.get("regressed") is True
        for row in comparisons
    )


def test_turn_execution_build_benchmark_reports_gap_when_no_records(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    _install_minimal_imposition_profile_loader(monkeypatch)
    coll = _TurnExecutionCollection([])
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        max_cases=5,
    )

    assert result["success"] is True
    metrics = result.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics["scanned_count"] == 0

    capability_gaps = result.get("capability_gaps")
    assert isinstance(capability_gaps, list)
    assert any(
        gap.get("gap_id") == "no_turn_execution_records" for gap in capability_gaps
    )
    assert any(
        gap.get("gap_id") == "minimal_imposition_telemetry_missing"
        for gap in capability_gaps
    )


def test_turn_execution_build_benchmark_includes_latency_and_trend_views(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    _install_minimal_imposition_profile_loader(monkeypatch)
    coll = _TurnExecutionCollection(_build_dashboard_trace_docs())
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        include_completed=True,
        max_cases=3,
    )

    assert result["success"] is True

    metrics = result.get("metrics")
    assert isinstance(metrics, dict)
    latency_metrics = metrics.get("latency_metrics")
    assert isinstance(latency_metrics, dict)
    assert latency_metrics.get("observed_pre_dispatch_count") == 3
    assert latency_metrics.get("avg_pre_dispatch_duration_ms") == 21.0
    assert latency_metrics.get("max_pre_dispatch_duration_ms") == 33
    assert latency_metrics.get("slowest_request_id") == "req-dash-2"

    latency_views = result.get("latency_views")
    assert isinstance(latency_views, dict)
    step_breakdown = latency_views.get("pre_dispatch_step_breakdown")
    assert isinstance(step_breakdown, list)
    selector_step = next(
        step
        for step in step_breakdown
        if step.get("step_id") == "selector_candidate_preparation"
    )
    assert selector_step.get("observed_count") == 3
    assert selector_step.get("slowest_request_id") == "req-dash-2"

    trend_views = result.get("trend_views")
    assert isinstance(trend_views, dict)
    assert trend_views.get("bucket_granularity") == "day"
    buckets = trend_views.get("turn_outcome_buckets")
    assert isinstance(buckets, list)
    assert len(buckets) == 2
    assert buckets[0].get("bucket_id") == "2026-02-20"
    assert buckets[0].get("scanned_count") == 1
    assert buckets[1].get("bucket_id") == "2026-02-21"
    assert buckets[1].get("scanned_count") == 2
    assert buckets[1].get("outcome_label_counts", {}).get("false_success") == 1
    assert buckets[1].get("outcome_label_counts", {}).get("successful_completion") == 1
    assert buckets[1].get("avg_pre_dispatch_duration_ms") == 22.5


def test_turn_execution_build_benchmark_hesitancy_trace_gateway_e2e(monkeypatch):
    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = _build_hesitancy_trace_docs()
    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
            "include_completed": True,
            "baseline_unresolved_follow_up_rate_pct": 70.0,
            "regression_tolerance_pct": 1.0,
        },
    ).payload

    assert payload["success"] is True
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics.get("scanned_count") == 3
    assert metrics.get("likely_failure_count") == 2
    assert metrics.get("unresolved_follow_up_count") == 2

    selection_metrics = metrics.get("selection_metrics")
    assert isinstance(selection_metrics, dict)
    assert selection_metrics.get("likely_failure_tool_workflow_count") == 2
    assert selection_metrics.get("likely_failure_plain_response_count") == 0
    assert selection_metrics.get("likely_failure_tool_workflow_rate_pct") == 100.0

    gate_metrics = metrics.get("gate_metrics")
    assert isinstance(gate_metrics, dict)
    assert gate_metrics.get("false_success_count") == 0
    assert gate_metrics.get("requires_follow_up_count") == 2

    retry_metrics = metrics.get("retry_metrics")
    assert isinstance(retry_metrics, dict)
    assert retry_metrics.get("follow_up_count") == 2
    assert retry_metrics.get("follow_up_with_retry_signal_count") == 2
    assert retry_metrics.get("bounded_retry_stop_count") == 1
    assert retry_metrics.get("stall_latency_stop_count") == 1

    user_imposition_metrics = metrics.get("user_imposition_metrics")
    assert isinstance(user_imposition_metrics, dict)
    assert user_imposition_metrics.get("follow_up_turn_count") == 2
    assert user_imposition_metrics.get("follow_up_turn_rate_pct") == 66.67
    assert user_imposition_metrics.get("escalation_signal_count") == 1

    signals = payload.get("benchmark_signals")
    assert isinstance(signals, list)
    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in signals
        if isinstance(signal, dict)
    }
    assert signal_by_id["workflow_selection_prefers_tool_path"]["status"] == "pass"
    assert signal_by_id["completion_gate_false_success_guard"]["status"] == "pass"
    assert signal_by_id["retry_guardrail_signals_recorded"]["status"] == "pass"
    assert signal_by_id["user_imposition_rate_vs_baseline"]["status"] == "pass"

    summary = payload.get("benchmark_signal_summary")
    assert isinstance(summary, dict)
    assert summary.get("fail_count") == 0
    assert summary.get("pass_count", 0) >= 4

    replay_cases = payload.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert replay_cases
    triage = replay_cases[0].get("triage")
    assert isinstance(triage, dict)
    assert "JVNAUTOSCI-1326" in triage.get("jira_issue_keys", [])

    imposition = payload.get("imposition_assessment")
    assert isinstance(imposition, dict)
    assert imposition.get("success") is True
    assert imposition.get("weighted_score_pct") is not None


def test_turn_execution_build_benchmark_projects_represented_recovery_signal(
    monkeypatch,
):
    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = [
        {
            "request_id": "req-represented-recovery",
            "session_id": "chat-represented-recovery",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-07-12T00:00:00Z",
            "completion_gate": {
                "decision": "partial",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect-1"],
            },
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "required_effects": [{"effect_id": "effect-1", "status": "not_executed"}],
            "prompt": {"preview": "Represented recovery signal"},
            "terminal_outcome_receipt": _terminal_receipt(
                outcome="verified_partial",
                causal_stage="execution",
                cause_code="required_action_missing",
                retryability="after_external_change",
                recovery_affordances=[{"action_type": "retry"}],
            ),
        }
    ]
    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    payload = _build_gateway().invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
            "include_completed": True,
        },
    ).payload

    retry_metrics = payload["metrics"]["retry_metrics"]
    assert retry_metrics["follow_up_count"] == 1
    assert retry_metrics["follow_up_with_retry_signal_count"] == 1
    signal_by_id = {
        signal["signal_id"]: signal for signal in payload["benchmark_signals"]
    }
    assert signal_by_id["retry_guardrail_signals_recorded"]["status"] == "pass"


def test_turn_execution_build_benchmark_hesitancy_signals_detect_plain_response_regression(
    monkeypatch,
):
    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = _build_hesitancy_trace_docs()
    docs[0]["workflow_selection"] = {
        "selected_workflow_id": "#V#chat_assistant_workflow",
        "selector_verdict": "plain_response",
    }

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
            "include_completed": True,
            "baseline_unresolved_follow_up_rate_pct": 70.0,
            "regression_tolerance_pct": 1.0,
        },
    ).payload

    assert payload["success"] is True
    signals = payload.get("benchmark_signals")
    assert isinstance(signals, list)
    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in signals
        if isinstance(signal, dict)
    }
    assert signal_by_id["workflow_selection_prefers_tool_path"]["status"] == "fail"
    details = signal_by_id["workflow_selection_prefers_tool_path"].get("details")
    assert isinstance(details, dict)
    assert details.get("observed_plain_response_count") == 1

    summary = payload.get("benchmark_signal_summary")
    assert isinstance(summary, dict)
    assert summary.get("fail_count", 0) >= 1


def test_turn_execution_build_benchmark_retains_corrective_evidence_replay_flags(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    _install_minimal_imposition_profile_loader(monkeypatch)
    coll = _TurnExecutionCollection(
        _build_corrective_evidence_failure_docs(follow_up_tool="concept_exists")
    )
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        max_cases=5,
    )

    assert result["success"] is True
    replay_cases = result.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert len(replay_cases) == 1
    replay_case = replay_cases[0]
    assert "selector_dispatch_divergence" in replay_case.get("diagnostic_flags", [])
    assert "weak_follow_up_action" in replay_case.get("diagnostic_flags", [])

    evidence = replay_case.get("evidence")
    assert isinstance(evidence, dict)
    assert (
        evidence.get("selector_intended_workflow_id")
        == "#V#specialised_vontology_search_workflow"
    )
    assert evidence.get("dispatch_workflow_id") == "#V#chat_assistant_workflow"
    assert evidence.get("selector_dispatch_divergence") is True
    assert evidence.get("weak_follow_up_action") is True
    tool_summary = evidence.get("tool_invocation_summary")
    assert isinstance(tool_summary, list)
    assert tool_summary[0]["tool"] == "concept_exists"

    action_quality_metrics = result.get("metrics", {}).get("action_quality_metrics")
    assert isinstance(action_quality_metrics, dict)
    assert action_quality_metrics.get("selector_dispatch_divergence_count") == 1
    assert action_quality_metrics.get("weak_follow_up_action_count") == 1
    assert (
        action_quality_metrics.get(
            "weak_follow_up_with_selector_dispatch_divergence_count"
        )
        == 1
    )


def test_turn_execution_build_benchmark_corrective_evidence_signal_changes_with_stronger_follow_up(
    monkeypatch,
):
    _install_minimal_imposition_profile_loader(monkeypatch)
    gateway = _build_gateway()

    weak_coll = _TurnExecutionCollection(
        _build_corrective_evidence_failure_docs(follow_up_tool="concept_exists")
    )
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": weak_coll}),
    )
    weak_payload = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
        },
    ).payload

    weak_signals = weak_payload.get("benchmark_signals")
    assert isinstance(weak_signals, list)
    weak_signal_by_id = {
        signal.get("signal_id"): signal
        for signal in weak_signals
        if isinstance(signal, dict)
    }
    assert (
        weak_signal_by_id[
            "likely_failure_cases_avoid_selector_dispatch_divergence_with_weak_follow_up"
        ]["status"]
        == "fail"
    )

    strong_coll = _TurnExecutionCollection(
        _build_corrective_evidence_failure_docs(follow_up_tool="fetch_concept_content")
    )
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": strong_coll}),
    )
    strong_payload = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
        },
    ).payload

    strong_signals = strong_payload.get("benchmark_signals")
    assert isinstance(strong_signals, list)
    strong_signal_by_id = {
        signal.get("signal_id"): signal
        for signal in strong_signals
        if isinstance(signal, dict)
    }
    assert (
        strong_signal_by_id[
            "likely_failure_cases_avoid_selector_dispatch_divergence_with_weak_follow_up"
        ]["status"]
        == "pass"
    )


def test_turn_execution_build_benchmark_links_episode_evaluation_context_and_follow_up_strengthening(
    monkeypatch,
):
    _install_minimal_imposition_profile_loader(monkeypatch)
    coll = _TurnExecutionCollection(_build_corrective_evidence_follow_up_docs())
    critique_coll = _EpisodeCritiqueCollection(
        [
            {
                "memory_id": "#V#episode_critique_memory_corr_1",
                "request_id": "req-corr-family-1",
                "workflow_id": "#V#specialised_vontology_search_workflow",
                "namespace": "#V#user@org",
                "created_at_utc": "2026-04-12T07:46:30Z",
                "verdict": "fail",
                "critic_summary_text": "Dispatch collapsed and the first follow-up action stayed too weak.",
                "improvement_suggestion_count": 1,
                "improvement_suggestion_categories": ["workflow_change"],
                "improvement_target_workflow_ids": [
                    "#V#specialised_vontology_search_workflow"
                ],
                "improvement_target_tool_names": ["fetch_concept_content"],
            },
            {
                "memory_id": "#V#episode_critique_memory_corr_2",
                "request_id": "req-corr-family-2",
                "workflow_id": "#V#specialised_vontology_search_workflow",
                "namespace": "#V#user@org",
                "created_at_utc": "2026-04-12T07:47:00Z",
                "verdict": "fail",
                "critic_summary_text": "The follow-up improved action quality but dispatch still diverged.",
                "improvement_suggestion_count": 1,
                "improvement_suggestion_categories": ["workflow_change"],
                "improvement_target_workflow_ids": [
                    "#V#specialised_vontology_search_workflow"
                ],
                "improvement_target_tool_names": ["fetch_concept_content"],
            },
        ]
    )
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB(
            {
                "turn_execution_records": coll,
                "episode_critique_memories": critique_coll,
            }
        ),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
        },
    ).payload

    assert payload["success"] is True
    replay_cases = payload.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert len(replay_cases) == 2

    first_case = next(
        case
        for case in replay_cases
        if isinstance(case, dict) and case.get("request_id") == "req-corr-family-1"
    )
    evidence = first_case.get("evidence")
    assert isinstance(evidence, dict)
    follow_up = evidence.get("follow_up")
    assert isinstance(follow_up, dict)
    assert follow_up.get("available") is True
    assert follow_up.get("follow_up_request_id") == "req-corr-family-2"
    assert follow_up.get("follow_up_action_strengthened") is True

    episode_evaluation = evidence.get("episode_evaluation")
    assert isinstance(episode_evaluation, dict)
    assert episode_evaluation.get("available") is True
    assert episode_evaluation.get("memory_id") == "#V#episode_critique_memory_corr_1"
    assert episode_evaluation.get("useful_suggestion_present") is True
    assert episode_evaluation.get("improvement_target_tool_names") == [
        "fetch_concept_content"
    ]
    assert episode_evaluation.get("useful_target_tool_names") == []

    diagnosis_layers = evidence.get("diagnosis_layers")
    assert isinstance(diagnosis_layers, dict)
    assert diagnosis_layers.get("routing_layer", {}).get("diagnosable") is True
    assert diagnosis_layers.get("action_layer", {}).get("diagnosable") is True
    assert diagnosis_layers.get("telemetry_layer", {}).get("diagnosable") is True
    assert (
        diagnosis_layers.get("episode_evaluation_layer", {}).get("diagnosable") is True
    )

    replay_diagnostic_metrics = payload.get("metrics", {}).get(
        "replay_diagnostic_metrics"
    )
    assert isinstance(replay_diagnostic_metrics, dict)
    assert replay_diagnostic_metrics.get("selected_case_count") == 2
    assert replay_diagnostic_metrics.get("diagnostic_candidate_case_count") == 2
    assert replay_diagnostic_metrics.get("telemetry_sufficient_case_count") == 2
    assert replay_diagnostic_metrics.get("episode_evaluation_available_count") == 2
    assert replay_diagnostic_metrics.get("corrective_evidence_follow_up_count") == 1
    assert (
        replay_diagnostic_metrics.get("corrective_evidence_stronger_follow_up_count")
        == 1
    )

    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in payload.get("benchmark_signals") or []
        if isinstance(signal, dict)
    }
    assert (
        signal_by_id["replay_cases_preserve_diagnostic_layer_coverage"]["status"]
        == "pass"
    )
    assert (
        signal_by_id["corrective_evidence_follow_up_strengthens_verification_action"][
            "status"
        ]
        == "pass"
    )


def test_turn_execution_build_selector_benchmark_gateway_e2e():
    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_selector_benchmark",
        {"case_set": "phase1_seed"},
    ).payload

    assert payload["success"] is True
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics.get("scanned_count") == 4
    assert metrics.get("matched_case_count") == 3
    assert metrics.get("selector_accuracy_pct") == 75.0
    assert metrics.get("baseline_accuracy_pct") == 25.0
    assert (
        metrics.get("outcome_label_counts", {}).get("tool_or_workflow_misrouting") == 1
    )
    assert (
        metrics.get("outcome_label_counts", {}).get("abstain_escalate_no_safe_route")
        == 0
    )

    corpus = payload.get("corpus")
    assert isinstance(corpus, dict)
    assert corpus.get("source") == "vontology"
    assert corpus.get("case_set") == "phase1_seed"

    signals = payload.get("benchmark_signals")
    assert isinstance(signals, list)
    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in signals
        if isinstance(signal, dict)
    }
    assert signal_by_id["selector_benchmark_corpus_present"]["status"] == "pass"
    assert signal_by_id["selector_accuracy_not_worse_than_baseline"]["status"] == "pass"
    assert signal_by_id["abstain_cases_routed_safely"]["status"] == "not_evaluated"


def test_turn_execution_build_context_answering_benchmark_gateway_e2e():
    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_context_answering_benchmark",
        {"case_set": "phase1_seed"},
    ).payload

    assert payload["success"] is True
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics.get("scanned_count") == 6
    assert metrics.get("exact_path_case_count") == 6
    assert metrics.get("telemetry_check_count") == 12
    assert (
        metrics.get("authoritative_source_counts", {}).get("represented_context") == 1
    )
    assert metrics.get("execution_mode_counts", {}).get("tool_pipeline") == 1
    assert metrics.get("answer_property_counts", {}).get("answer_first") == 6

    corpus = payload.get("corpus")
    assert isinstance(corpus, dict)
    assert corpus.get("source") == "vontology"
    assert corpus.get("case_set") == "phase1_seed"

    replay_cases = payload.get("replay_cases")
    assert isinstance(replay_cases, list)
    represented_case = next(
        case
        for case in replay_cases
        if isinstance(case, dict)
        and case.get("case_id") == "represented_context_tool_pipeline_answer"
    )

    assert represented_case["expected_execution_mode"] == "tool_pipeline"

    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in payload.get("benchmark_signals") or []
        if isinstance(signal, dict)
    }
    assert signal_by_id["context_grounded_benchmark_corpus_present"]["status"] == "pass"
    assert (
        signal_by_id["required_context_grounding_classes_present"]["status"] == "pass"
    )
    assert signal_by_id["authoritative_sources_represented"]["status"] == "pass"
    assert (
        signal_by_id["execution_modes_cover_direct_tool_and_workflow_paths"]["status"]
        == "pass"
    )
    assert signal_by_id["benchmark_backed_by_exact_path_validation"]["status"] == "pass"


@pytest.mark.parametrize(
    "method_name",
    (
        "context_bundle_build_benchmark",
        "turn_execution_build_selector_benchmark",
        "turn_execution_build_context_answering_benchmark",
    ),
)
def test_turn_benchmark_host_bundle_path_requires_operator_authority(method_name):
    gateway = _build_gateway()

    payload = gateway.invoke(
        method_name,
        {"bundle_path": "/tmp/private-benchmark.json"},
    ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "host_path_authority_required"


def test_turn_execution_build_selector_benchmark_supports_entity_representation_case_set():
    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_selector_benchmark",
        {"case_set": "entity_representation_generalisation"},
    ).payload

    assert payload["success"] is True
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics.get("scanned_count") == 10
    assert metrics.get("matched_case_count") == metrics.get("scanned_count")
    assert metrics.get("selector_accuracy_pct") == 100.0
    assert metrics.get("baseline_accuracy_pct") == 0.0
    assert metrics.get("outcome_label_counts", {}).get("successful_completion") == 10
    assert (
        metrics.get("outcome_label_counts", {}).get("abstain_escalate_no_safe_route")
        in {None, 0}
    )
    assert (
        metrics.get("outcome_label_counts", {}).get("tool_or_workflow_misrouting") == 0
    )

    corpus = payload.get("corpus")
    assert isinstance(corpus, dict)
    assert corpus.get("source") == "vontology"
    assert corpus.get("case_set") == "entity_representation_generalisation"

    replay_cases = payload.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert {
        case.get("case_id")
        for case in replay_cases
        if isinstance(case, dict)
    } == {
        "entity_people_follow_up_reasoning_override",
        "entity_person_representation_success",
        "entity_company_representation_success",
        "entity_event_representation_success",
        "entity_event_write_with_tool_calling_competitor",
        "entity_event_write_without_explicit_vontology_phrasing",
        "entity_event_write_with_storage_verb",
        "entity_event_write_with_capture_verb",
        "entity_place_representation_success",
        "entity_ambiguity_low_imposition_route",
    }
    follow_up_case = next(
        case
        for case in replay_cases
        if isinstance(case, dict)
        and case.get("case_id") == "entity_people_follow_up_reasoning_override"
    )
    assert follow_up_case["selected_workflow_id"] == "#V#entity_representation_workflow"

    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in payload.get("benchmark_signals") or []
        if isinstance(signal, dict)
    }
    assert signal_by_id["selector_benchmark_corpus_present"]["status"] == "pass"
    assert signal_by_id["selector_accuracy_not_worse_than_baseline"]["status"] == "pass"
    assert signal_by_id["abstain_cases_routed_safely"]["status"] == "not_evaluated"
    assert (
        signal_by_id["selector_misrouting_examples_detected"]["status"]
        == "not_evaluated"
    )


def test_turn_execution_build_selector_benchmark_supports_corrective_evidence_failure_family_case_set():
    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_selector_benchmark",
        {"case_set": "corrective_evidence_failure_family"},
    ).payload

    assert payload["success"] is True
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics.get("scanned_count") == 2
    assert metrics.get("matched_case_count") == 2
    assert metrics.get("selector_accuracy_pct") == 100.0

    replay_cases = payload.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert len(replay_cases) == 2
    assert all(
        case.get("replay_family_id") == "strong_ai_lab_su_yuchen_corrective_evidence"
        for case in replay_cases
        if isinstance(case, dict)
    )
    follow_up_case = next(
        case
        for case in replay_cases
        if isinstance(case, dict)
        and case.get("case_id") == "su_yuchen_follow_up_requires_stronger_verification"
    )
    assert "follow_up_turn" in follow_up_case.get("case_tags", [])
    assert (
        follow_up_case.get("source_session_id")
        == "e1ca7cde-9341-420f-8cc4-908db1218bd8"
    )


def test_turn_execution_build_dashboard_gateway_e2e(monkeypatch):
    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = _build_dashboard_trace_docs()
    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_dashboard",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "include_completed": True,
            "max_cases": 3,
            "selector_case_set": "phase1_seed",
            "baseline_false_success_rate_pct": 0.0,
            "baseline_selector_accuracy_pct": 90.0,
            "baseline_pre_dispatch_avg_duration_ms": 15.0,
            "regression_tolerance_pct": 1.0,
            "latency_regression_tolerance_pct": 10.0,
        },
    ).payload

    assert payload["success"] is True
    assert payload["collection"] == "turn_execution_dashboard"
    assert payload["provenance"]["item_kind"] == "turn_execution_dashboard_report"

    overview = payload.get("overview")
    assert isinstance(overview, dict)
    assert overview.get("turn_execution", {}).get("false_success_rate_pct") == 33.33
    assert overview.get("selector_routing", {}).get("selector_accuracy_pct") == 80.0
    assert (
        overview.get("pre_dispatch_latency", {}).get("avg_pre_dispatch_duration_ms")
        == 21.0
    )
    assert overview.get("minimal_imposition", {}).get("weighted_score_pct") is not None

    summary_cards = payload.get("summary_cards")
    assert isinstance(summary_cards, list)
    card_by_id = {
        card.get("card_id"): card for card in summary_cards if isinstance(card, dict)
    }
    assert card_by_id["false_success_rate"]["status"] == "fail"
    assert card_by_id["selector_accuracy"]["status"] == "fail"
    assert card_by_id["avg_pre_dispatch_duration"]["status"] == "fail"
    assert "minimal_imposition_score" in card_by_id
    assert "valid_terminal_receipt_coverage" in card_by_id
    assert "typed_non_success_coverage" in card_by_id
    assert "recovery_affordance_coverage" in card_by_id

    regression_views = payload.get("regression_views")
    assert isinstance(regression_views, dict)
    active_regressions = regression_views.get("active_regressions")
    assert isinstance(active_regressions, list)
    regression_keys = {
        (row.get("source_surface"), row.get("metric"))
        for row in active_regressions
        if isinstance(row, dict)
    }
    assert ("turn_execution", "false_success_rate_pct") in regression_keys
    assert ("selector_routing", "selector_accuracy_pct") in regression_keys
    assert ("pre_dispatch_latency", "avg_pre_dispatch_duration_ms") in regression_keys

    trend_views = payload.get("trend_views")
    assert isinstance(trend_views, dict)
    assert len(trend_views.get("turn_outcome_buckets") or []) == 2

    drilldowns = payload.get("drilldowns")
    assert isinstance(drilldowns, dict)
    assert len(drilldowns.get("turn_execution_replay_cases") or []) == 2
    assert len(drilldowns.get("selector_replay_cases") or []) == 5

    signals = payload.get("benchmark_signals")
    assert isinstance(signals, list)
    assert any(
        signal.get("source_surface") == "pre_dispatch_latency"
        for signal in signals
        if isinstance(signal, dict)
    )
    imposition = payload.get("imposition_assessment")
    assert isinstance(imposition, dict)
    assert imposition.get("success") is True


def test_turn_execution_build_dashboard_gateway_accepts_omitted_limit_and_offset(
    monkeypatch,
):
    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = _build_dashboard_trace_docs()
    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_dashboard",
        {
            "namespace": "#V#user@org",
        },
    ).payload

    assert payload["success"] is True
    turn_filters = payload.get("filters", {}).get("turn_execution")
    assert isinstance(turn_filters, dict)
    assert turn_filters.get("limit") == 20
    assert turn_filters.get("offset") == 0
    assert payload.get("summary_cards")


def test_turn_execution_dashboard_projects_strict_operational_campaign(
    monkeypatch,
):
    from src.backend.services.operational_certification_contract_service import (
        stable_payload_digest,
    )
    from src.backend.services.operational_certification_runner_service import (
        build_campaign_experiment_observation,
    )

    _install_minimal_imposition_profile_loader(monkeypatch)
    docs = _build_dashboard_trace_docs()
    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )
    campaign = {
        "schema_version": "operational_certification_campaign_result.v1",
        "suite_id": "first_sail_operational_suite",
        "certified": False,
        "contract_sha256": "c" * 64,
        "pass_rates": {
            "pass^1": 1.0,
            "pass^3": 0.8,
            "pass^5": 0.6,
        },
        "families": {
            "generic": {
                "pass_rates": {
                    "pass^1": 1.0,
                    "pass^3": 0.8,
                    "pass^5": 0.6,
                }
            }
        },
        "certification_gate_results": [
            {
                "gate_id": "pass_five",
                "kind": "hard_interface",
                "passed": False,
                "check": {"required": True},
            }
        ],
        "failed_certification_gate_ids": ["pass_five"],
        "blockers": [],
        "experiment_persistence_required": True,
        "experiment_persistence_complete": True,
    }
    campaign["certification_gate_results"][0]["result_sha256"] = (
        stable_payload_digest(campaign["certification_gate_results"][0])
    )
    campaign["report_sha256"] = stable_payload_digest(campaign)
    provenance = {
        "effective_namespace": "#V#user@org",
        "effective_user_id": "#V#user",
        "effective_org_id": "#V#org",
    }
    observation = build_campaign_experiment_observation(
        campaign,
        execution_provenance=provenance,
    )
    monkeypatch.setattr(
        "src.backend.services.experiment_run_service.get_experiment_run_state",
        lambda run_id: {
            "run_id": run_id,
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "observations": [observation],
        },
    )

    payload = _build_gateway().invoke(
        "turn_execution_build_dashboard",
        {
            "namespace": "#V#user@org",
            "user_concept_id": "#V#user",
            "organisation_concept_id": "#V#org",
            "operational_certification_run_id": "#V#experiment_run_1",
        },
    ).payload

    operational = payload["overview"]["operational_certification"]
    assert operational["run_id"] == "#V#experiment_run_1"
    assert operational["certified"] is False
    assert operational["report_sha256"] == campaign["report_sha256"]
    assert operational["family_pass_rates"]["generic"]["pass^3"] == 0.8
    signal = next(
        item
        for item in payload["benchmark_signals"]
        if item.get("signal_id") == "operational_certification_campaign"
    )
    assert signal["status"] == "fail"
    assert payload["data_sources"]["operational_certification"] == {
        "run_id": "#V#experiment_run_1",
        "report_sha256": campaign["report_sha256"],
        "contract_sha256": "c" * 64,
        "source": "experiment_run.operational_certification_campaign",
    }
    assert payload["effective_namespace"] == "#V#user@org"
    assert payload["user_id"] == "#V#user"
    assert payload["org_id"] == "#V#org"


def test_turn_execution_dashboard_requires_explicit_operational_caller_scope(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.backend.services.experiment_run_service.get_experiment_run_state",
        lambda run_id: {
            "run_id": run_id,
            "namespace": "#V#private@org",
            "user_id": "#V#private",
            "org_id": "#V#org",
            "observations": [],
        },
    )

    payload = _build_gateway().invoke(
        "turn_execution_build_dashboard",
        {"operational_certification_run_id": "#V#private_run"},
    ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "operational_certification_scope_required"


def test_turn_execution_dashboard_rejects_inconsistent_campaign_envelope(
    monkeypatch,
):
    from src.backend.services.operational_certification_contract_service import (
        stable_payload_digest,
    )

    campaign = {
        "schema_version": "operational_certification_campaign_result.v1",
        "suite_id": "tampered_suite",
        "contract_sha256": "d" * 64,
        "certified": True,
        "certification_gate_results": [
            {"gate_id": "failed_gate", "passed": False, "check": {}}
        ],
        "failed_certification_gate_ids": ["failed_gate"],
        "blockers": [{"code": "blocked", "blocking": True}],
    }
    campaign["report_sha256"] = stable_payload_digest(campaign)
    observation = {
        "schema_version": "wrong_observation.v1",
        "observation_type": "operational_certification_campaign",
        "verdict": "pass",
        "observed_outcome": {
            "certified": True,
            "report_sha256": campaign["report_sha256"],
        },
        "execution_provenance": {
            "effective_namespace": "#V#user@org",
            "effective_user_id": "#V#user",
            "effective_org_id": "#V#org",
        },
        "evidence": {"operational_certification_campaign_result": campaign},
    }
    monkeypatch.setattr(
        "src.backend.services.experiment_run_service.get_experiment_run_state",
        lambda run_id: {
            "run_id": run_id,
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "observations": [observation],
        },
    )

    payload = _build_gateway().invoke(
        "turn_execution_build_dashboard",
        {
            "namespace": "#V#user@org",
            "user_concept_id": "#V#user",
            "organisation_concept_id": "#V#org",
            "operational_certification_run_id": "#V#tampered_run",
        },
    ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "operational_certification_integrity_mismatch"


def test_turn_execution_backfill_wrapper_returns_provenance(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.backfill_turn_execution_records_from_chat_history",
        lambda **kwargs: {
            "success": True,
            "namespace": kwargs.get("namespace"),
            "dry_run": kwargs.get("dry_run"),
            "synthesise_missing_records": kwargs.get("synthesise_missing_records"),
            "candidate_records": 3,
            "upserted_count": 0,
        },
    )

    result = cat._turn_execution_backfill_from_chat_history(
        namespace="#V#user@org",
        dry_run=True,
        limit_sessions=100,
        synthesise_missing_records=False,
    )

    assert result["success"] is True
    assert result["namespace"] == "#V#user@org"
    assert result["synthesise_missing_records"] is False
    assert result["candidate_records"] == 3
    assert result["provenance"]["item_kind"] == "turn_execution_backfill_report"


def test_turn_execution_namespace_coverage_wrapper_returns_provenance(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_turn_execution_namespace_coverage_report",
        lambda **kwargs: {
            "success": True,
            "namespace_filter": kwargs.get("namespace"),
            "namespaces_scanned": 1,
            "coverage_by_namespace": [
                {
                    "namespace": "#V#user@org",
                    "assistant_messages_scanned": 10,
                    "assistant_messages_with_turn_execution_record": 4,
                    "projected_records_total": 4,
                }
            ],
        },
    )

    result = cat._turn_execution_namespace_coverage_report(
        namespace="#V#user@org",
        limit_namespaces=5,
        limit_sessions_per_namespace=50,
        limit_projected_records_per_namespace=500,
    )

    assert result["success"] is True
    assert result["namespace_filter"] == "#V#user@org"
    assert result["namespaces_scanned"] == 1
    assert (
        result["provenance"]["item_kind"] == "turn_execution_namespace_coverage_report"
    )
