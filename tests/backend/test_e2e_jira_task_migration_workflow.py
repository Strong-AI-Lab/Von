"""End-to-end coverage for workflow-first Jira->Von task migration.

JVNAUTOSCI-1223 requires migration orchestration to be represented as a
Vontology process graph that executes through MCP tools.
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


_WORKFLOW_ID = "#V#jira_task_migration_workflow"
_DISCOVER_STEP_ID = "#V#discover_jira_issues_step"
_IMPORT_STEP_ID = "#V#import_jira_issues_step"
_COMPLETED_STEP_ID = "#V#jira_migration_completed_step"


def _build_workflow_docs() -> Dict[str, Dict[str, Any]]:
    return {
        _WORKFLOW_ID: {
            "concept_id": _WORKFLOW_ID,
            "name": "Jira Task Migration Workflow",
            "relationships": {
                "hasInitialStep": _DISCOVER_STEP_ID,
                "hasStep": [_DISCOVER_STEP_ID, _IMPORT_STEP_ID, _COMPLETED_STEP_ID],
            },
        },
        _DISCOVER_STEP_ID: {
            "concept_id": _DISCOVER_STEP_ID,
            "name": "Discover Jira Issues",
            "relationships": {
                "invokesAction": "jira_search",
                "workflow_step_maps_context_key_to_tool_param": [
                    "#V#workflow_mapping_jira_query_to_jql_param"
                ],
                "workflow_step_maps_tool_output_field_to_context_key": [
                    "#V#workflow_mapping_tool_field_issues_to_jira_discovered_issues"
                ],
                "workflow_step_writes_context_key": [
                    "#V#workflow_context_key_jira_discovered_issues"
                ],
                "nextStep": _IMPORT_STEP_ID,
            },
        },
        _IMPORT_STEP_ID: {
            "concept_id": _IMPORT_STEP_ID,
            "name": "Import Jira Issues",
            "relationships": {
                "invokesAction": "task_import_jira_issues",
                "workflow_step_maps_context_key_to_tool_param": [
                    "#V#workflow_mapping_jira_discovered_issues_to_issue_keys_param"
                ],
                "workflow_step_maps_tool_output_field_to_context_key": [
                    "#V#workflow_mapping_tool_field_summary_to_jira_migration_summary",
                    "#V#workflow_mapping_tool_field_project_parity_to_jira_project_parity",
                ],
                "workflow_step_writes_context_key": [
                    "#V#workflow_context_key_jira_migration_summary",
                    "#V#workflow_context_key_jira_project_parity",
                ],
                "nextStep": _COMPLETED_STEP_ID,
            },
        },
        _COMPLETED_STEP_ID: {
            "concept_id": _COMPLETED_STEP_ID,
            "name": "Migration Complete",
            "relationships": {},
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


def test_workflow_defined_jira_migration_executes_via_mcp_gateway(monkeypatch):
    docs = _build_workflow_docs()
    search_calls: list[dict[str, Any]] = []
    get_issue_calls: list[str] = []

    class _FakeProxy:
        async def search(
            self,
            *,
            jql: str,
            max_results=None,
            start_at=None,
            next_page_token=None,
            fields=None,
        ):
            search_calls.append(
                {
                    "jql": jql,
                    "max_results": max_results,
                    "start_at": start_at,
                    "next_page_token": next_page_token,
                    "fields": fields,
                }
            )
            return {
                "issues": [
                    {"key": "JVNAUTOSCI-4100"},
                    {"key": "JVNAUTOSCI-4101"},
                ]
            }

        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            get_issue_calls.append(issue_key)
            return {
                "key": issue_key,
                "fields": {
                    "summary": f"Imported {issue_key}",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                    "issuelinks": [],
                },
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.find_task_by_external_reference",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.create_task",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry_run import must not create tasks")
        ),
    )

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
            return_value="Workflow-first Jira task migration.",
        ),
    ):
        definition = load_workflow_definition_from_vontology(_WORKFLOW_ID)

    assert definition is not None

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    orchestrator = _build_orchestrator_with_gateway(gateway)
    registry = ActionRegistry()
    registry.set_fallback_handler(orchestrator._action_mcp_tool_invoke)
    executor = WorkflowExecutor(registry=registry, max_transitions=8)
    environment = WorkflowEnvironment(
        llm_client=None,
        gateway=gateway,
        user_namespace="#V#test_user",
    )

    result = executor.run(
        definition,
        environment=environment,
        data={
            "jira_query": "project = JVNAUTOSCI ORDER BY created DESC",
        },
    )

    assert result.completed is True
    assert result.final_state == _COMPLETED_STEP_ID
    assert result.data.get("jira_discovered_issues") == [
        {"key": "JVNAUTOSCI-4100"},
        {"key": "JVNAUTOSCI-4101"},
    ]
    migration_summary = result.data.get("jira_migration_summary")
    assert isinstance(migration_summary, dict)
    assert migration_summary.get("total_issues") == 2
    project_parity = result.data.get("jira_project_parity")
    assert isinstance(project_parity, dict)
    assert project_parity.get("summary", {}).get("projects_scanned") == 1

    assert len(search_calls) == 1
    assert search_calls[0]["jql"] == "project = JVNAUTOSCI ORDER BY created DESC"
    assert get_issue_calls == ["JVNAUTOSCI-4100", "JVNAUTOSCI-4101"]
