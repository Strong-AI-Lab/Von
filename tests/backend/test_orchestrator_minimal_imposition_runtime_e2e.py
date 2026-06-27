"""End-to-end write-policy tests for the minimal-imposition runtime model."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence, cast

from orchestrator_test_harness import build_db_independent_orchestrator
from src.backend.services import settings_service


@dataclass(frozen=True)
class _TransportResult:
    payload: Any
    duration_ms: float


class _RuntimeProfileGateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {
            "download_paper": {
                "category": "write",
                "description": "Download an arXiv paper and store as an artefact",
            },
            "update_concept": {
                "category": "write",
                "description": "Update a concept.",
            },
            "delete_concept": {
                "category": "write",
                "description": "Delete a concept.",
            },
            "jira_update_issue": {
                "category": "write",
                "description": "Update a Jira issue.",
            },
            "workflow_bind_event": {
                "category": "write",
                "description": "Bind an event to a workflow.",
            },
        }

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> _TransportResult:
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        return _TransportResult(payload={"success": True}, duration_ms=1.0)


class _CapturingLLM:
    def __init__(self, responses: Sequence[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


def _write_request_evidence_response(
    tool_name: str,
    *,
    request_state: str = "low_confidence",
    confirmation_state: str = "low_confidence",
    rationale: str = "test stub",
) -> str:
    return (
        '{'
        '"schema_version":"write_tool_request_evidence.v1",'
        '"tool_evidence":['
        "{"
        f'"tool_name":"{tool_name}",'
        f'"request_state":"{request_state}",'
        f'"confirmation_state":"{confirmation_state}",'
        f'"rationale":"{rationale}"'
        "}"
        "]"
        "}"
    )


def _runtime_profile() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "profile_concept_id": "#V#minimal_imposition_runtime_profile_write_policy_v1",
            "decision_policy": {
                "default_allow_additive_low_risk": True,
                "require_clear_request_for_recoverable_mutation": True,
                "allow_recent_request_context_for_recoverable_mutation": True,
                "require_confirmation_for_destructive": True,
                "require_explicit_request_for_external": True,
                "process_sensitive_additive_requires_clear_request": True,
            },
            "tool_risk_classes": {
                "download_paper": "additive_low_risk",
                "update_concept": "mutative_non_destructive",
                "delete_concept": "destructive",
                "jira_update_issue": "external_non_vontology",
                "workflow_bind_event": "additive_low_risk",
            },
            "tool_feature_overrides": {
                "workflow_bind_event": {
                    "blast_radius": "high",
                    "process_sensitive": True,
                    "requires_clear_request": True,
                    "organisation_policy_sensitive": True,
                }
            },
        },
        {
            "resolved_profile_concept_id": "#V#minimal_imposition_runtime_profile_write_policy_v1",
            "loaded_profile_concept_id": "#V#minimal_imposition_runtime_profile_write_policy_v1",
        },
    )


def test_runtime_profile_allows_low_risk_additive_write_without_interruption(
    monkeypatch,
):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: _runtime_profile(),
    )
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
            _write_request_evidence_response("download_paper"),
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2510.06248",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert any(call["tool"] == "download_paper" for call in gateway.invocations)
    record = next(
        record
        for record in result.tool_invocations
        if record.get("tool") == "download_paper"
    )
    assert record.get("write_policy_scenario_id") == "low_risk_additive_internal_write"
    assert record.get("write_policy_confidence_state") == "implicit_low_risk_default"
    assert record.get("write_policy_intervention_kind") == "auto_allow"


def test_runtime_profile_blocks_low_confidence_recoverable_mutation(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: _runtime_profile(),
    )
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"update_concept","payload":{"concept_id":"#V#guarded","update_data":{"description":"updated"}}}',
            _write_request_evidence_response("update_concept"),
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Tell me about this concept.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert not any(call.get("tool") == "update_concept" for call in gateway.invocations)
    blocked = next(
        record
        for record in result.tool_invocations
        if record.get("tool") == "update_concept"
    )
    assert blocked.get("blocked") is True
    assert (
        blocked.get("write_policy_blocked_reason")
        == "mutative_non_destructive_request_required"
    )
    assert blocked.get("write_policy_confidence_state") == "low_confidence"
    assert blocked.get("write_policy_intervention_kind") == "require_explicit_request"
    assert "insufficient_request_evidence" in (
        blocked.get("write_policy_unresolved_risk_factors") or []
    )


def test_runtime_profile_allows_explicit_recoverable_mutation(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: _runtime_profile(),
    )
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"update_concept","payload":{"concept_id":"#V#guarded","update_data":{"description":"updated"}}}',
            _write_request_evidence_response(
                "update_concept",
                request_state="explicit_request",
                rationale="current prompt explicitly requests the update",
            ),
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Update the Vontology concept description now.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert any(call.get("tool") == "update_concept" for call in gateway.invocations)
    record = next(
        record
        for record in result.tool_invocations
        if record.get("tool") == "update_concept"
    )
    assert record.get("write_policy_scenario_id") == "reversible_update"
    assert record.get("write_policy_confidence_state") == "explicit_request"


def test_runtime_profile_blocks_high_fan_out_process_sensitive_additive_write(
    monkeypatch,
):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: _runtime_profile(),
    )
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: _runtime_profile(),
    )
    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"workflow_bind_event","payload":{"event_type":"task.created","workflow_id":"#V#rumination_workflow"}}',
            _write_request_evidence_response("workflow_bind_event"),
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Review the current workflow event bindings only.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert not any(
        call.get("tool") == "workflow_bind_event" for call in gateway.invocations
    )
    blocked = next(
        record
        for record in result.tool_invocations
        if record.get("tool") == "workflow_bind_event"
    )
    assert blocked.get("write_policy_scenario_id") == "high_fan_out_change"
    assert blocked.get("write_policy_risk_features", {}).get("blast_radius") == "high"
    assert blocked.get("write_policy_intervention_kind") == "require_explicit_request"


def test_runtime_profile_requires_confirmation_for_destructive_write(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: _runtime_profile(),
    )
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"delete_concept","payload":{"concept_id":"#V#guarded"}}',
            _write_request_evidence_response("delete_concept"),
            "Done.",
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Delete concept #V#guarded.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert not any(call.get("tool") == "delete_concept" for call in gateway.invocations)
    blocked = next(
        record
        for record in result.tool_invocations
        if record.get("tool") == "delete_concept"
    )
    assert blocked.get("write_policy_blocked_reason") == (
        "destructive_confirmation_required"
    )
    assert blocked.get("write_policy_intervention_kind") == "require_confirmation"


def test_runtime_profile_requires_explicit_request_for_external_write(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: _runtime_profile(),
    )
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"jira_update_issue","payload":{"issue_key":"JVNAUTOSCI-1","update_fields":{"summary":"new"}}}',
            _write_request_evidence_response("jira_update_issue"),
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="Summarise the Jira issue state.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert not any(call.get("tool") == "jira_update_issue" for call in gateway.invocations)
    blocked = next(
        record
        for record in result.tool_invocations
        if record.get("tool") == "jira_update_issue"
    )
    assert blocked.get("write_policy_blocked_reason") == (
        "external_write_requires_explicit_request"
    )
    assert blocked.get("write_policy_scenario_id") == "low_confidence_evidence"
    assert blocked.get("write_policy_intervention_kind") == "require_explicit_request"


def test_write_policy_resolution_passes_turn_contract_to_workflow(monkeypatch):
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    captured: dict[str, Any] = {}

    def _execute_workflow(workflow_id: str, **kwargs: Any) -> Any:
        captured["workflow_id"] = workflow_id
        captured["data"] = dict(kwargs.get("data") or {})
        return SimpleNamespace(
            data={
                "allowed_write_tools": ["workflow_execute"],
                "write_policy_reason": "explicit_external_write_request",
                "write_policy_decision_basis": "explicit_external_write_request",
                "write_policy_outcome": "allowed",
                "write_policy_user_denial_detected": False,
                "write_policy_tool_outcomes": {"workflow_execute": "allowed"},
                "write_policy_risk_classes": {
                    "workflow_execute": "external_non_vontology"
                },
                "write_policy_blocked_reasons": {},
                "write_policy_authority_block_sources": {},
                "write_policy_scenario_ids": {
                    "workflow_execute": "external_system_write"
                },
                "write_policy_risk_features": {},
                "write_policy_unresolved_risk_factors": {},
                "write_policy_intervention_kinds": {
                    "workflow_execute": "auto_allow"
                },
                "write_policy_confidence_states": {
                    "workflow_execute": "explicit_request"
                },
                "write_policy_request_evidence": {},
                "write_policy_request_evidence_diagnostics": {},
                "write_policy_profile_diagnostics": {},
                "write_policy_requires_confirmation": [],
                "mutation_guardrail_events": [],
            }
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    decision = orchestrator._resolve_allowed_write_tools(
        prompt=(
            "Look in last 10 email addresses for the most recent talking about "
            "an arxiv file and represent the paper."
        ),
        requested_write_tools=["workflow_execute"],
        requested_write_payloads={
            "workflow_execute": {
                "workflow_id": "#V#arxiv_paper_representation_workflow",
            }
        },
        recent_user_prompts=[],
        llm_client=_CapturingLLM([]),
        model=None,
        user_namespace="#V#user",
        auxiliary_system_prompt=None,
        trace=None,
        turn_expected_outcome_contract_state={
            "required_tools": ["gmail_get_message"],
            "workflow_concept_ids": ["#V#arxiv_paper_representation_workflow"],
        },
        activated_conditional_required_tools=["workflow_execute"],
        guardrail_surface="execution",
        guardrail_stage="tool_execute",
    )

    assert decision.allowed_tools == frozenset({"workflow_execute"})
    assert captured["workflow_id"] == "#V#write_tool_policy_workflow"
    assert captured["data"]["turn_expected_outcome_contract_state"] == {
        "required_tools": ["gmail_get_message"],
        "workflow_concept_ids": ["#V#arxiv_paper_representation_workflow"],
    }
    assert captured["data"]["activated_conditional_required_tools"] == [
        "workflow_execute"
    ]


def test_runtime_profile_loader_failure_falls_back_to_baseline_policy(monkeypatch):
    monkeypatch.setattr(
        settings_service, "get_disable_write_tool_conservatism", lambda: False
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: (_ for _ in ()).throw(RuntimeError("profile store unavailable")),
    )
    gateway = _RuntimeProfileGateway()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        max_tool_invocations=1,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_: (_ for _ in ()).throw(RuntimeError("profile store unavailable")),
    )
    llm = _CapturingLLM(
        [
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
            _write_request_evidence_response("download_paper"),
            "Done.",
        ]
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2510.06248",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert any(call["tool"] == "download_paper" for call in gateway.invocations)
    record = next(
        record
        for record in result.tool_invocations
        if record.get("tool") == "download_paper"
    )
    assert record.get("blocked") is not True
    diagnostics = record.get("write_policy_profile_diagnostics") or {}
    assert diagnostics.get("status") == "runtime_profile_unavailable"
    assert diagnostics.get("error") == "profile store unavailable"
