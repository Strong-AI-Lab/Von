"""Coverage for Jira hygiene workflow tools and Vontology workflow execution.

JVNAUTOSCI-929 / JVNAUTOSCI-930:
- discover -> propose -> approval -> execute -> audit
- workflow-first execution through Vontology process graph + MCP tool calls
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, MutableMapping
from unittest.mock import MagicMock, patch

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_jira_hygiene_discover_and_propose_through_gateway(monkeypatch) -> None:
    import src.backend.integrations.internal_mcp.catalogue as catalogue

    def _fake_search(**kwargs):
        jql = str(kwargs.get("jql") or "")
        if "issuetype = Epic" in jql:
            return {
                "issues": [
                    {
                        "key": "JVNAUTOSCI-800",
                        "fields": {
                            "summary": "Identity and auth hardening",
                            "issuetype": {"name": "Epic"},
                            "status": {"name": "In Progress"},
                        },
                    },
                    {
                        "key": "JVNAUTOSCI-801",
                        "fields": {
                            "summary": "Planning workflow orchestration",
                            "issuetype": {"name": "Epic"},
                            "status": {"name": "To Do"},
                        },
                    },
                ]
            }
        return {
            "issues": [
                {
                    "key": "JVNAUTOSCI-9100",
                    "fields": {
                        "summary": "Harden identity write telemetry behaviour",
                        "issuetype": {"name": "Task"},
                        "status": {"name": "To Do"},
                        "labels": ["identity", "telemetry"],
                    },
                },
                {
                    "key": "JVNAUTOSCI-9101",
                    "fields": {
                        "summary": "Cross-stream workflow telemetry note",
                        "issuetype": {"name": "Task"},
                        "status": {"name": "In Progress"},
                        "parent": {"key": "JVNAUTOSCI-801"},
                        "labels": ["telemetry"],
                    },
                },
                {
                    "key": "JVNAUTOSCI-9102",
                    "fields": {
                        "summary": "Completely unrelated backlog item",
                        "issuetype": {"name": "Task"},
                        "status": {"name": "To Do"},
                    },
                },
            ]
        }

    monkeypatch.setattr(catalogue, "_jira_search", _fake_search)
    gateway = _build_gateway()

    discover = gateway.invoke(
        "jira_hygiene_discover",
        {
            "project_key": "JVNAUTOSCI",
            "include_cross_cutting": True,
            "max_issues": 50,
            "max_epics": 20,
        },
    ).payload
    assert discover.get("success") is True
    assert discover.get("discovery_counts", {}).get("epics") == 2
    assert discover.get("discovery_counts", {}).get("orphans") == 2
    assert discover.get("discovery_counts", {}).get("cross_cutting") == 1

    proposal = gateway.invoke(
        "jira_hygiene_propose",
        {
            "epic_catalogue": discover.get("epic_catalogue"),
            "orphan_candidates": discover.get("orphan_candidates"),
            "cross_cutting_candidates": discover.get("cross_cutting_candidates"),
            "batch_size": 5,
        },
    ).payload
    assert proposal.get("success") is True
    assert proposal.get("proposal_summary", {}).get("ready_to_execute_count", 0) >= 1
    assert proposal.get("execution_plan", {}).get("batch_size") == 5
    assert isinstance(proposal.get("ready_to_execute"), list)


def test_jira_hygiene_approval_and_execute_retries_through_gateway(monkeypatch) -> None:
    import src.backend.integrations.internal_mcp.catalogue as catalogue

    update_attempts: dict[str, int] = {}
    comment_calls: list[str] = []

    def _fake_update_issue(**kwargs):
        issue_key = str(kwargs.get("issue_key"))
        attempt = update_attempts.get(issue_key, 0) + 1
        update_attempts[issue_key] = attempt
        if issue_key == "JVNAUTOSCI-9200" and attempt == 1:
            return {"success": False, "error": "429 Too Many Requests"}
        return {"success": True, "issue_key": issue_key}

    def _fake_add_comment(**kwargs):
        issue_key = str(kwargs.get("issue_key"))
        comment_calls.append(issue_key)
        return {"success": True, "issue_key": issue_key}

    monkeypatch.setattr(catalogue, "_jira_update_issue", _fake_update_issue)
    monkeypatch.setattr(catalogue, "_jira_add_comment", _fake_add_comment)

    gateway = _build_gateway()
    ready = [
        {
            "operation_id": "assign-JVNAUTOSCI-9200",
            "issue_key": "JVNAUTOSCI-9200",
            "operation": "assign_epic",
            "target_epic_key": "JVNAUTOSCI-800",
        },
        {
            "operation_id": "comment-JVNAUTOSCI-9201",
            "issue_key": "JVNAUTOSCI-9201",
            "operation": "add_comment",
            "comment_text": "Cross-cutting note.",
            "related_epic_key": "JVNAUTOSCI-801",
        },
    ]

    approval = gateway.invoke(
        "jira_hygiene_check_approval",
        {
            "ready_to_execute": ready,
            "execution_mode": "execute",
            "approved": True,
            "batch_size": 2,
        },
    ).payload
    assert approval.get("result") is True
    assert approval.get("approval_decision", {}).get("approved") is True

    execution = gateway.invoke(
        "jira_hygiene_execute_batches",
        {
            "approved_operations": approval.get("approved_operations"),
            "execution_mode": approval.get("execution_mode"),
            "approved": True,
            "batch_size": approval.get("resolved_batch_size"),
            "max_retries": 2,
            "retry_backoff_seconds": 0,
        },
    ).payload
    assert execution.get("success") is True
    assert execution.get("execution_summary", {}).get("executed_operations_count") == 2
    assert execution.get("execution_summary", {}).get("retry_attempts_total") == 1
    assert update_attempts.get("JVNAUTOSCI-9200") == 2
    assert "JVNAUTOSCI-9201" in comment_calls


def test_jira_hygiene_emit_audit_can_post_epic_comments(monkeypatch) -> None:
    import src.backend.integrations.internal_mcp.catalogue as catalogue

    epic_comment_calls: list[str] = []

    def _fake_add_comment(**kwargs):
        issue_key = str(kwargs.get("issue_key"))
        epic_comment_calls.append(issue_key)
        return {"success": True, "issue_key": issue_key}

    monkeypatch.setattr(catalogue, "_jira_add_comment", _fake_add_comment)

    gateway = _build_gateway()
    payload = gateway.invoke(
        "jira_hygiene_emit_audit",
        {
            "project_key": "JVNAUTOSCI",
            "execution_mode": "execute",
            "proposal_summary": {"ready_to_execute_count": 2},
            "execution_summary": {
                "executed": True,
                "failed_operations_count": 0,
                "executed_operations_count": 2,
            },
            "approved_operations": [
                {
                    "operation_id": "assign-A",
                    "issue_key": "JVNAUTOSCI-9300",
                    "operation": "assign_epic",
                    "target_epic_key": "JVNAUTOSCI-800",
                },
                {
                    "operation_id": "assign-B",
                    "issue_key": "JVNAUTOSCI-9301",
                    "operation": "assign_epic",
                    "target_epic_key": "JVNAUTOSCI-800",
                },
            ],
            "needs_decision": [{"issue_key": "JVNAUTOSCI-9302"}],
            "emit_epic_comments": True,
        },
    ).payload
    assert payload.get("success") is True
    result_payload = payload.get("jira_hygiene_result", {})
    assert result_payload.get("success") is True
    assert result_payload.get("ambiguous_issues_remaining") == ["JVNAUTOSCI-9302"]
    assert epic_comment_calls == ["JVNAUTOSCI-800"]


_WORKFLOW_ID = "#V#jira_hygiene_orphan_triage_workflow"
_DISCOVER_STEP_ID = "#V#jira_hygiene_discover_step"
_PROPOSE_STEP_ID = "#V#jira_hygiene_propose_step"
_APPROVAL_STEP_ID = "#V#jira_hygiene_approval_step"
_EXECUTE_STEP_ID = "#V#jira_hygiene_execute_step"
_AUDIT_STEP_ID = "#V#jira_hygiene_audit_step"
_COMPLETED_STEP_ID = "#V#jira_hygiene_completed_step"


def _build_workflow_docs() -> Dict[str, Dict[str, Any]]:
    return {
        _WORKFLOW_ID: {
            "concept_id": _WORKFLOW_ID,
            "name": "Jira Hygiene Workflow",
            "relationships": {
                "hasInitialStep": _DISCOVER_STEP_ID,
                "hasStep": [
                    _DISCOVER_STEP_ID,
                    _PROPOSE_STEP_ID,
                    _APPROVAL_STEP_ID,
                    _EXECUTE_STEP_ID,
                    _AUDIT_STEP_ID,
                    _COMPLETED_STEP_ID,
                ],
            },
        },
        _DISCOVER_STEP_ID: {
            "concept_id": _DISCOVER_STEP_ID,
            "name": "Discover",
            "relationships": {
                "invokesAction": "jira_hygiene_discover",
                "hasInputMap": ["project_key=JVNAUTOSCI", "include_cross_cutting=true"],
                "workflow_step_maps_tool_output_field_to_context_key": [
                    "#V#workflow_mapping_tool_field_epic_catalogue_to_epic_catalogue",
                    "#V#workflow_mapping_tool_field_orphan_candidates_to_orphan_candidates",
                    "#V#workflow_mapping_tool_field_cross_cutting_candidates_to_cross_cutting_candidates",
                ],
                "nextStep": _PROPOSE_STEP_ID,
            },
        },
        _PROPOSE_STEP_ID: {
            "concept_id": _PROPOSE_STEP_ID,
            "name": "Propose",
            "relationships": {
                "invokesAction": "jira_hygiene_propose",
                "workflow_step_maps_context_key_to_tool_param": [
                    "#V#workflow_mapping_epic_catalogue_to_epic_catalogue_param",
                    "#V#workflow_mapping_orphan_candidates_to_orphan_candidates_param",
                    "#V#workflow_mapping_cross_cutting_candidates_to_cross_cutting_candidates_param",
                ],
                "workflow_step_maps_tool_output_field_to_context_key": [
                    "#V#workflow_mapping_tool_field_ready_to_execute_to_ready_ops",
                    "#V#workflow_mapping_tool_field_needs_decision_to_needs_decision",
                    "#V#workflow_mapping_tool_field_proposal_summary_to_proposal_summary",
                ],
                "nextStep": _APPROVAL_STEP_ID,
            },
        },
        _APPROVAL_STEP_ID: {
            "concept_id": _APPROVAL_STEP_ID,
            "name": "Approval",
            "relationships": {
                "invokesAction": "jira_hygiene_check_approval",
                "hasInputMap": ["execution_mode=execute", "approved=true"],
                "workflow_step_maps_context_key_to_tool_param": [
                    "#V#workflow_mapping_ready_ops_to_ready_to_execute_param"
                ],
                "workflow_step_maps_tool_output_field_to_context_key": [
                    "#V#workflow_mapping_tool_field_approved_operations_to_approved_operations",
                    "#V#workflow_mapping_tool_field_resolved_batch_size_to_resolved_batch_size",
                ],
                "onTrueNextStep": _EXECUTE_STEP_ID,
                "onFalseNextStep": _AUDIT_STEP_ID,
            },
        },
        _EXECUTE_STEP_ID: {
            "concept_id": _EXECUTE_STEP_ID,
            "name": "Execute",
            "relationships": {
                "invokesAction": "jira_hygiene_execute_batches",
                "hasInputMap": ["execution_mode=execute", "approved=true"],
                "workflow_step_maps_context_key_to_tool_param": [
                    "#V#workflow_mapping_approved_operations_to_approved_operations_param",
                    "#V#workflow_mapping_resolved_batch_size_to_batch_size_param",
                ],
                "workflow_step_maps_tool_output_field_to_context_key": [
                    "#V#workflow_mapping_tool_field_execution_summary_to_execution_summary"
                ],
                "nextStep": _AUDIT_STEP_ID,
            },
        },
        _AUDIT_STEP_ID: {
            "concept_id": _AUDIT_STEP_ID,
            "name": "Audit",
            "relationships": {
                "invokesAction": "jira_hygiene_emit_audit",
                "hasInputMap": [
                    "execution_mode=execute",
                    "project_key=JVNAUTOSCI",
                    "emit_epic_comments=false",
                ],
                "workflow_step_maps_context_key_to_tool_param": [
                    "#V#workflow_mapping_proposal_summary_to_proposal_summary_param",
                    "#V#workflow_mapping_execution_summary_to_execution_summary_param",
                    "#V#workflow_mapping_approved_operations_to_approved_operations_param",
                    "#V#workflow_mapping_needs_decision_to_needs_decision_param",
                ],
                "workflow_step_maps_tool_output_field_to_context_key": [
                    "#V#workflow_mapping_tool_field_jira_hygiene_result_to_jira_hygiene_result"
                ],
                "nextStep": _COMPLETED_STEP_ID,
            },
        },
        _COMPLETED_STEP_ID: {
            "concept_id": _COMPLETED_STEP_ID,
            "name": "Completed",
            "relationships": {},
        },
        "#V#workflow_mapping_tool_field_ready_to_execute_to_ready_ops": {
            "concept_id": "#V#workflow_mapping_tool_field_ready_to_execute_to_ready_ops",
            "concept_data": {
                "workflow_mapping_spec": {
                    "schema_version": 1,
                    "mapping_type": "tool_output_field_to_context_key",
                    "workflow_step_id": _PROPOSE_STEP_ID,
                    "tool_id": "jira_hygiene_propose",
                    "tool_output_field_name": "ready_to_execute",
                    "target_context_key_concept_id": "#V#ready_ops",
                }
            },
        },
        "#V#workflow_mapping_ready_ops_to_ready_to_execute_param": {
            "concept_id": "#V#workflow_mapping_ready_ops_to_ready_to_execute_param",
            "concept_data": {
                "workflow_mapping_spec": {
                    "schema_version": 1,
                    "mapping_type": "context_key_to_tool_param",
                    "workflow_step_id": _APPROVAL_STEP_ID,
                    "tool_id": "jira_hygiene_check_approval",
                    "context_key_concept_id": "#V#ready_ops",
                    "tool_param_name": "ready_to_execute",
                }
            },
        },
    }


def _build_repo_find(docs: Dict[str, Dict[str, Any]]):
    def _mock_find(
        query: MutableMapping[str, Any], projection=None, limit=None  # noqa: ARG001
    ) -> Iterator[Dict[str, Any]]:
        concept_ids = query.get("concept_id", {})
        if isinstance(concept_ids, dict) and "$in" in concept_ids:
            ids = concept_ids["$in"]
            return iter([docs[cid] for cid in ids if cid in docs])
        return iter([])

    return _mock_find


def _build_repo_find_one(docs: Dict[str, Dict[str, Any]]):
    def _mock_find_one(query: MutableMapping[str, Any], projection=None):  # noqa: ARG001
        concept_id = query.get("concept_id")
        if isinstance(concept_id, str):
            return docs.get(concept_id)
        return None

    return _mock_find_one


def _build_orchestrator_with_gateway(gateway: InternalMCPGateway):
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    with patch.object(InternalMCPChatOrchestrator, "__init__", lambda self: None):
        orchestrator = InternalMCPChatOrchestrator()  # type: ignore[call-arg]
        orchestrator._gateway = gateway  # type: ignore[assignment]
        orchestrator._logger = MagicMock()
    return orchestrator


def test_vontology_defined_jira_hygiene_workflow_executes_via_gateway(monkeypatch) -> None:
    import src.backend.integrations.internal_mcp.catalogue as catalogue

    docs = _build_workflow_docs()
    update_calls: list[str] = []
    comment_calls: list[str] = []

    def _fake_search(**kwargs):
        jql = str(kwargs.get("jql") or "")
        if "issuetype = Epic" in jql:
            return {
                "issues": [
                    {
                        "key": "JVNAUTOSCI-800",
                        "fields": {
                            "summary": "Identity and auth hardening",
                            "issuetype": {"name": "Epic"},
                            "status": {"name": "In Progress"},
                        },
                    }
                ]
            }
        return {
            "issues": [
                {
                    "key": "JVNAUTOSCI-9400",
                    "fields": {
                        "summary": "Identity write telemetry hardening",
                        "issuetype": {"name": "Task"},
                        "status": {"name": "To Do"},
                        "labels": ["identity", "telemetry"],
                    },
                }
            ]
        }

    def _fake_update_issue(**kwargs):
        issue_key = str(kwargs.get("issue_key"))
        update_calls.append(issue_key)
        return {"success": True, "issue_key": issue_key}

    def _fake_add_comment(**kwargs):
        issue_key = str(kwargs.get("issue_key"))
        comment_calls.append(issue_key)
        return {"success": True, "issue_key": issue_key}

    monkeypatch.setattr(catalogue, "_jira_search", _fake_search)
    monkeypatch.setattr(catalogue, "_jira_update_issue", _fake_update_issue)
    monkeypatch.setattr(catalogue, "_jira_add_comment", _fake_add_comment)

    with (
        patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find",
            side_effect=_build_repo_find(docs),
        ),
        patch(
            "src.backend.workflows.vontology_loader.ConceptsRepository.find_one",
            side_effect=_build_repo_find_one(docs),
        ),
        patch(
            "src.backend.workflows.vontology_loader.best_effort_workflow_narrative_text",
            return_value="Jira hygiene workflow.",
        ),
    ):
        definition = load_workflow_definition_from_vontology(_WORKFLOW_ID)

    assert definition is not None

    gateway = _build_gateway()
    orchestrator = _build_orchestrator_with_gateway(gateway)
    registry = ActionRegistry()
    registry.set_fallback_handler(orchestrator._action_mcp_tool_invoke)
    executor = WorkflowExecutor(registry=registry, max_transitions=14)
    environment = WorkflowEnvironment(
        llm_client=None,
        gateway=gateway,
        user_namespace="#V#test_user",
    )

    result = executor.run(definition, environment=environment, data={})

    assert result.completed is True
    assert result.final_state == _COMPLETED_STEP_ID
    assert result.data.get("execution_summary", {}).get("executed_operations_count") == 1
    hygiene_result = result.data.get("jira_hygiene_result")
    assert isinstance(hygiene_result, dict)
    assert hygiene_result.get("success") is True
    assert update_calls == ["JVNAUTOSCI-9400"]
    assert comment_calls == []
