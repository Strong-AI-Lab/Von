"""Tests for workflow introspection maintenance workflow (JVNAUTOSCI-1298)."""

from __future__ import annotations

from unittest.mock import patch

from src.backend.workflows.action_registry import WorkflowActionRequest, WorkflowEnvironment


def _request(*, action_id: str, data: dict, namespace: str = "#V#user@org"):
    return WorkflowActionRequest(
        action_id=action_id,
        inputs={},
        environment=WorkflowEnvironment(llm_client=None, user_namespace=namespace),
        data=data,
    )


def test_workflow_structure():
    from src.backend.workflows.durable.workflow_introspection_maintenance_workflow import (
        WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
        build_workflow_introspection_maintenance_workflow_test_definition,
    )

    wf = build_workflow_introspection_maintenance_workflow_test_definition()
    assert wf.workflow_id == WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID
    assert wf.initial_state == "assess"
    assert set(wf.states.keys()) == {
        "assess",
        "diagnose",
        "plan",
        "apply",
        "verify",
        "complete",
        "failed",
    }


def test_assess_prefers_user_namespace_for_prompt_lookup(monkeypatch):
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    lookup_calls: list[str] = []

    def _fake_invoke(tool_name: str, payload: dict):
        if tool_name == "workflow_list_definitions":
            return {"success": True, "count": 1, "definitions": []}
        if tool_name == "chat_get_prompt_context":
            lookup_calls.append(str(payload.get("namespace")))
            if payload.get("namespace") == "#V#user":
                return {
                    "success": True,
                    "behaviour_prompt_concepts": [
                        {"concept_id": "#V#general_prompt", "content": "always use task_create"}
                    ],
                }
            return {"success": False, "error": "not_found"}
        if tool_name == "turn_execution_get":
            return {
                "success": True,
                "selected_workflow_id": "#V#tool_calling_workflow",
                "decision": "completed",
            }
        if tool_name == "fetch_concept":
            return {"success": True, "concept_id": "#V#tool_calling_workflow"}
        return {"success": True}

    monkeypatch.setattr(mod, "_invoke_mcp_tool", _fake_invoke)
    monkeypatch.setattr(mod, "_available_tool_names", lambda: ["task_create", "workflow_list_definitions"])

    result = mod._handle_assess_context(
        _request(
            action_id="workflow_introspection.assess_context",
            data={"request_id": "req-1", "github_owner": "Strong-AI-Lab", "github_repo": "Von"},
            namespace="#V#user@org",
        )
    )
    assert result.ok
    assert result.outputs["maintenance_context"]["prompt_lookup_namespace"] == "#V#user"
    assert lookup_calls[0] == "#V#user"


def test_assess_collects_github_evidence(monkeypatch):
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    def _fake_invoke(tool_name: str, payload: dict):
        if tool_name == "workflow_list_definitions":
            return {"success": True, "count": 1, "definitions": []}
        if tool_name == "chat_get_prompt_context":
            return {"success": True, "behaviour_prompt_concepts": []}
        if tool_name == "turn_execution_get":
            return {"success": True, "selected_workflow_id": "#V#tool_calling_workflow"}
        if tool_name == "fetch_concept":
            return {"success": True, "concept_id": "#V#tool_calling_workflow"}
        if tool_name == "github_get_auth_config":
            return {"success": True, "proxy_tools_available": True}
        if tool_name == "github_list_commits":
            return {"success": True, "items": []}
        if tool_name == "github_list_pull_requests":
            return {"success": True, "items": []}
        if tool_name == "github_get_file_contents":
            return {"success": True, "content": "x", "path": payload.get("path")}
        return {"success": True}

    monkeypatch.setattr(mod, "_invoke_mcp_tool", _fake_invoke)
    monkeypatch.setattr(mod, "_available_tool_names", lambda: ["task_create"])

    result = mod._handle_assess_context(
        _request(
            action_id="workflow_introspection.assess_context",
            data={
                "request_id": "req-gh-1",
                "github_owner": "Strong-AI-Lab",
                "github_repo": "Von",
                "incident_text": "Please inspect src/backend/workflows/durable/workflow_introspection_maintenance_workflow.py",
            },
            namespace="#V#user@org",
        )
    )
    assert result.ok
    github_evidence = result.outputs["maintenance_evidence"]["github_evidence"]
    assert github_evidence["success"] is True
    assert github_evidence["repository"] == "Strong-AI-Lab/Von"


def test_diagnose_detects_alias_scope_and_cross_domain_signals():
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    data = {
        "maintenance_context": {
            "namespace": "#V#user@org",
            "selected_workflow_id": "#V#tool_calling_workflow",
        },
        "maintenance_evidence": {
            "incident_text": "I asked for ontology concept maintenance, not task creation.",
            "known_tool_names": ["task_create", "workflow_create_instance"],
            "prompt_context": {
                "behaviour_prompt_concepts": [
                    {
                        "concept_id": "#V#general_prompt",
                        "content": "When the user asks you to create, track, or manage tasks -> always use create_task.",
                    }
                ]
            },
            "turn_execution": {
                "decision": "completed",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "critic": {"summary": {"not_verified_count": 1}},
            },
        },
    }
    result = mod._handle_diagnose_conflation(
        _request(action_id="workflow_introspection.diagnose_conflation", data=data)
    )
    assert result.ok
    diagnosis = result.outputs["maintenance_diagnosis"]
    causes = {item["cause_id"] for item in diagnosis["root_causes"]}
    assert "prompt_scope_overreach" in causes
    assert "prompt_tool_alias_drift" in causes
    assert "cross_domain_conflation_risk" in causes
    assert "completion_gate_false_positive" in causes


def test_plan_repairs_builds_prompt_patch_and_workflow_note():
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    data = {
        "maintenance_context": {
            "namespace": "#V#user@org",
            "request_id": "req-1",
            "selected_workflow_id": "#V#tool_calling_workflow",
            "apply_repairs": True,
            "dry_run": False,
            "max_operations": 10,
        },
        "maintenance_evidence": {
            "prompt_context": {
                "behaviour_prompt_concepts": [
                    {
                        "concept_id": "#V#general_prompt",
                        "content": "always use create_task",
                    }
                ]
            }
        },
        "maintenance_diagnosis": {
            "conflation_detected": True,
            "root_causes": [{"cause_id": "prompt_scope_overreach"}],
            "implicated_prompt_concept_ids": ["#V#general_prompt"],
            "directive_findings": [
                {
                    "prompt_concept_id": "#V#general_prompt",
                    "tool_name": "create_task",
                    "canonical_tool_name": "task_create",
                    "is_absolute": True,
                    "scope_overreach": True,
                }
            ],
        },
    }
    result = mod._handle_plan_repairs(
        _request(action_id="workflow_introspection.plan_repairs", data=data)
    )
    assert result.ok
    repair_plan = result.outputs["maintenance_repair_plan"]
    assert repair_plan["operation_count"] >= 1
    tools = {item["tool_name"] for item in repair_plan["operations"]}
    assert "upsert_singleton_text_relation" in tools
    assert "upsert_text_relation" in tools


def test_plan_repairs_adds_jira_remediation_operation_when_evidence_available():
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    data = {
        "maintenance_context": {
            "namespace": "#V#user@org",
            "request_id": "req-1361",
            "selected_workflow_id": "#V#tool_calling_workflow",
            "apply_repairs": True,
            "dry_run": False,
            "max_operations": 10,
            "emit_jira_remediation": True,
            "jira_project_key": "JVNAUTOSCI",
            "jira_issue_type": "Task",
        },
        "maintenance_evidence": {
            "incident_text": "Investigate workflow failure in src/backend/workflows/durable/workflow_introspection_maintenance_workflow.py",
            "prompt_context": {"behaviour_prompt_concepts": []},
            "github_evidence": {
                "success": True,
                "repository": "Strong-AI-Lab/Von",
                "errors": [],
            },
        },
        "maintenance_diagnosis": {
            "conflation_detected": True,
            "root_causes": [{"cause_id": "prompt_scope_overreach"}],
            "directive_findings": [],
            "implicated_prompt_concept_ids": [],
        },
    }
    result = mod._handle_plan_repairs(
        _request(action_id="workflow_introspection.plan_repairs", data=data)
    )
    assert result.ok
    repair_plan = result.outputs["maintenance_repair_plan"]
    tools = {item["tool_name"] for item in repair_plan["operations"]}
    assert "__jira_self_diagnosis_upsert__" in tools


def test_apply_repairs_executes_operations(monkeypatch):
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    calls: list[tuple[str, dict]] = []

    def _fake_invoke(tool_name: str, payload: dict):
        calls.append((tool_name, payload))
        return {"success": True}

    monkeypatch.setattr(mod, "_invoke_mcp_tool", _fake_invoke)

    result = mod._handle_apply_repairs(
        _request(
            action_id="workflow_introspection.apply_repairs",
            data={
                "maintenance_repair_plan": {
                    "apply_requested": True,
                    "dry_run": False,
                    "operations": [
                        {
                            "operation_id": "op-1",
                            "tool_name": "upsert_singleton_text_relation",
                            "payload": {"concept_id": "#V#general_prompt"},
                        }
                    ],
                }
            },
        )
    )
    assert result.ok
    report = result.outputs["maintenance_apply_report"]
    assert report["successful_operations"] == 1
    assert calls[0][0] == "upsert_singleton_text_relation"


def test_apply_repairs_executes_jira_self_diagnosis_upsert(monkeypatch):
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    monkeypatch.setattr(
        mod,
        "upsert_deduplicated_jira_issue",
        lambda **kwargs: {
            "success": True,
            "mode": "updated_existing",
            "issue_key": "JVNAUTOSCI-1600",
            "fingerprint": kwargs.get("fingerprint"),
        },
    )

    result = mod._handle_apply_repairs(
        _request(
            action_id="workflow_introspection.apply_repairs",
            data={
                "maintenance_repair_plan": {
                    "apply_requested": True,
                    "dry_run": False,
                    "operations": [
                        {
                            "operation_id": "jira-remediation",
                            "tool_name": "__jira_self_diagnosis_upsert__",
                            "payload": {
                                "project_key": "JVNAUTOSCI",
                                "issue_type": "Task",
                                "summary": "Remediation summary",
                                "description": "Generated by Codex",
                                "fingerprint": "abc12345",
                                "request_id": "req-1361",
                            },
                        }
                    ],
                }
            },
        )
    )
    assert result.ok
    report = result.outputs["maintenance_apply_report"]
    assert report["successful_operations"] == 1
    assert report["results"][0]["tool_name"] == "__jira_self_diagnosis_upsert__"
    assert report["results"][0]["issue_key"] == "JVNAUTOSCI-1600"


def test_verify_repairs_fails_when_guardrail_not_observable(monkeypatch):
    from src.backend.workflows.durable import workflow_introspection_maintenance_workflow as mod

    monkeypatch.setattr(
        mod,
        "_invoke_mcp_tool",
        lambda tool_name, payload: (
            {
                "success": True,
                "behaviour_prompt_concepts": [
                    {"concept_id": "#V#general_prompt", "content": "no marker here"}
                ],
            }
            if tool_name == "chat_get_prompt_context"
            else {"success": True}
        ),
    )

    result = mod._handle_verify_repairs(
        _request(
            action_id="workflow_introspection.verify_repairs",
            data={
                "maintenance_context": {"prompt_lookup_namespace": "#V#user"},
                "maintenance_apply_report": {"applied": True},
                "maintenance_repair_plan": {
                    "operations": [
                        {
                            "tool_name": "upsert_singleton_text_relation",
                            "payload": {"concept_id": "#V#general_prompt"},
                        }
                    ]
                },
            },
        )
    )
    assert result.ok is False
    assert "repair_verification_failed" in str(result.error)


def test_registered_in_registry_factory():
    from src.backend.workflows.durable.workflow_introspection_maintenance_workflow import (
        WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
    )
    from src.backend.workflows.durable.registry_factory import (
        _build_workflow_registry,
        build_durable_action_registry,
    )
    from workflow_test_support import (
        bootstrap_authoritative_support_maintenance_workflows,
    )

    bootstrap_authoritative_support_maintenance_workflows()

    with patch(
        "src.backend.workflows.durable.registry_factory.discover_workflow_ids",
        return_value=[WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID],
    ):
        workflow_registry = _build_workflow_registry(allow_bootstrap=False)
        assert WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID in workflow_registry.all_workflow_ids()

    action_registry = build_durable_action_registry()
    assert action_registry.has("workflow_introspection.assess_context")
    assert action_registry.has("workflow_introspection.apply_repairs")
