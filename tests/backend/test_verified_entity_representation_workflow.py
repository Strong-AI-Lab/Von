"""Verified entity representation workflow examples with read-back gating.

Covers the concrete instantiation scope for:
- JVNAUTOSCI-823 (parent)
- JVNAUTOSCI-827 (organisation workflow instance)
- JVNAUTOSCI-826 (researcher affiliation extension)
- JVNAUTOSCI-828 (negative-path branching + retries)
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, MutableMapping
from unittest.mock import MagicMock, patch

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.engine import WorkflowDefinition, WorkflowExecutor
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


_HAI_WORKFLOW_ID = "#V#entity_representation_workflow"
_AFFILIATION_WORKFLOW_ID = "#V#researcher_affiliation_extension_workflow"


def _build_hai_workflow_docs() -> Dict[str, Dict[str, Any]]:
    return {
        _HAI_WORKFLOW_ID: {
            "concept_id": _HAI_WORKFLOW_ID,
            "name": "Verified Organisation Representation Workflow",
            "relationships": {
                "hasInitialStep": "#V#check_org_exists",
                "hasStep": [
                    "#V#check_org_exists",
                    "#V#create_org",
                    "#V#check_org_exists_retry",
                    "#V#verify_org_type",
                    "#V#add_org_type",
                    "#V#verify_org_type_retry",
                    "#V#decision_org_type_verified",
                    "#V#verify_org_evidence",
                    "#V#attach_org_evidence",
                    "#V#verify_org_evidence_retry",
                    "#V#hai_completed",
                    "#V#escalate_missing_org",
                    "#V#escalate_type_mismatch",
                    "#V#escalate_evidence_missing",
                ],
            },
        },
        "#V#check_org_exists": {
            "concept_id": "#V#check_org_exists",
            "relationships": {
                "invokesAction": "test_org_exists",
                "hasInputMap": ["target_name=Stanford HAI"],
                "onTrueNextStep": "#V#verify_org_type",
                "onFalseNextStep": "#V#create_org",
            },
        },
        "#V#create_org": {
            "concept_id": "#V#create_org",
            "relationships": {
                "invokesAction": "create_org",
                "hasInputMap": ["name=Stanford Institute for Human-Centered Artificial Intelligence"],
                "nextStep": "#V#check_org_exists_retry",
            },
        },
        "#V#check_org_exists_retry": {
            "concept_id": "#V#check_org_exists_retry",
            "relationships": {
                "invokesAction": "test_org_exists",
                "hasInputMap": ["target_name=Stanford HAI"],
                "onTrueNextStep": "#V#verify_org_type",
                "onFalseNextStep": "#V#escalate_missing_org",
            },
        },
        "#V#verify_org_type": {
            "concept_id": "#V#verify_org_type",
            "relationships": {
                "invokesAction": "test_org_type_membership",
                "onTrueNextStep": "#V#decision_org_type_verified",
                "onFalseNextStep": "#V#add_org_type",
            },
        },
        "#V#add_org_type": {
            "concept_id": "#V#add_org_type",
            "relationships": {
                "invokesAction": "add_org_type",
                "hasInputMap": ["type=#V#organisation"],
                "nextStep": "#V#verify_org_type_retry",
            },
        },
        "#V#verify_org_type_retry": {
            "concept_id": "#V#verify_org_type_retry",
            "relationships": {
                "invokesAction": "test_org_type_membership",
                "onTrueNextStep": "#V#decision_org_type_verified",
                "onFalseNextStep": "#V#escalate_type_mismatch",
            },
        },
        "#V#decision_org_type_verified": {
            "concept_id": "#V#decision_org_type_verified",
            "relationships": {
                # Decision step: no action, branch off prior verification result.
                "onTrueNextStep": "#V#verify_org_evidence",
                "onFalseNextStep": "#V#escalate_type_mismatch",
            },
        },
        "#V#verify_org_evidence": {
            "concept_id": "#V#verify_org_evidence",
            "relationships": {
                "invokesAction": "test_org_evidence",
                "onTrueNextStep": "#V#hai_completed",
                "onFalseNextStep": "#V#attach_org_evidence",
            },
        },
        "#V#attach_org_evidence": {
            "concept_id": "#V#attach_org_evidence",
            "relationships": {
                "invokesAction": "attach_org_evidence",
                "hasInputMap": ["source_url=https://en.wikipedia.org/wiki/Stanford_Human-Centered_AI_Institute"],
                "nextStep": "#V#verify_org_evidence_retry",
            },
        },
        "#V#verify_org_evidence_retry": {
            "concept_id": "#V#verify_org_evidence_retry",
            "relationships": {
                "invokesAction": "test_org_evidence",
                "onTrueNextStep": "#V#hai_completed",
                "onFalseNextStep": "#V#escalate_evidence_missing",
            },
        },
        "#V#hai_completed": {
            "concept_id": "#V#hai_completed",
            "relationships": {},
        },
        "#V#escalate_missing_org": {
            "concept_id": "#V#escalate_missing_org",
            "relationships": {
                "invokesAction": "fail_workflow",
                "hasInputMap": ["reason=missing_organisation"],
            },
        },
        "#V#escalate_type_mismatch": {
            "concept_id": "#V#escalate_type_mismatch",
            "relationships": {
                "invokesAction": "fail_workflow",
                "hasInputMap": ["reason=type_mismatch"],
            },
        },
        "#V#escalate_evidence_missing": {
            "concept_id": "#V#escalate_evidence_missing",
            "relationships": {
                "invokesAction": "fail_workflow",
                "hasInputMap": ["reason=evidence_missing"],
            },
        },
    }


def _build_affiliation_workflow_docs() -> Dict[str, Dict[str, Any]]:
    return {
        _AFFILIATION_WORKFLOW_ID: {
            "concept_id": _AFFILIATION_WORKFLOW_ID,
            "name": "Researcher Affiliation Extension Workflow",
            "relationships": {
                "hasInitialStep": "#V#verify_person_exists",
                "hasStep": [
                    "#V#verify_person_exists",
                    "#V#verify_org_exists_for_affiliation",
                    "#V#verify_affiliation_exists",
                    "#V#add_affiliation",
                    "#V#verify_affiliation_exists_retry",
                    "#V#verify_affiliation_evidence",
                    "#V#attach_affiliation_evidence",
                    "#V#verify_affiliation_evidence_retry",
                    "#V#affiliation_completed",
                    "#V#escalate_missing_person",
                    "#V#escalate_missing_affiliation_org",
                    "#V#escalate_affiliation_missing",
                    "#V#escalate_affiliation_evidence_missing",
                ],
            },
        },
        "#V#verify_person_exists": {
            "concept_id": "#V#verify_person_exists",
            "relationships": {
                "invokesAction": "test_person_exists",
                "hasInputMap": ["person_name=Yejin Choi"],
                "onTrueNextStep": "#V#verify_org_exists_for_affiliation",
                "onFalseNextStep": "#V#escalate_missing_person",
            },
        },
        "#V#verify_org_exists_for_affiliation": {
            "concept_id": "#V#verify_org_exists_for_affiliation",
            "relationships": {
                "invokesAction": "test_org_exists",
                "hasInputMap": ["target_name=Stanford HAI"],
                "onTrueNextStep": "#V#verify_affiliation_exists",
                "onFalseNextStep": "#V#escalate_missing_affiliation_org",
            },
        },
        "#V#verify_affiliation_exists": {
            "concept_id": "#V#verify_affiliation_exists",
            "relationships": {
                "invokesAction": "test_affiliation_exists",
                "onTrueNextStep": "#V#verify_affiliation_evidence",
                "onFalseNextStep": "#V#add_affiliation",
            },
        },
        "#V#add_affiliation": {
            "concept_id": "#V#add_affiliation",
            "relationships": {
                "invokesAction": "add_affiliation",
                "hasInputMap": ["predicate=#V#hasAffiliation"],
                "nextStep": "#V#verify_affiliation_exists_retry",
            },
        },
        "#V#verify_affiliation_exists_retry": {
            "concept_id": "#V#verify_affiliation_exists_retry",
            "relationships": {
                "invokesAction": "test_affiliation_exists",
                "onTrueNextStep": "#V#verify_affiliation_evidence",
                "onFalseNextStep": "#V#escalate_affiliation_missing",
            },
        },
        "#V#verify_affiliation_evidence": {
            "concept_id": "#V#verify_affiliation_evidence",
            "relationships": {
                "invokesAction": "test_affiliation_evidence",
                "onTrueNextStep": "#V#affiliation_completed",
                "onFalseNextStep": "#V#attach_affiliation_evidence",
            },
        },
        "#V#attach_affiliation_evidence": {
            "concept_id": "#V#attach_affiliation_evidence",
            "relationships": {
                "invokesAction": "attach_affiliation_evidence",
                "hasInputMap": ["source_url=https://hai.stanford.edu/people/yejin-choi"],
                "nextStep": "#V#verify_affiliation_evidence_retry",
            },
        },
        "#V#verify_affiliation_evidence_retry": {
            "concept_id": "#V#verify_affiliation_evidence_retry",
            "relationships": {
                "invokesAction": "test_affiliation_evidence",
                "onTrueNextStep": "#V#affiliation_completed",
                "onFalseNextStep": "#V#escalate_affiliation_evidence_missing",
            },
        },
        "#V#affiliation_completed": {
            "concept_id": "#V#affiliation_completed",
            "relationships": {},
        },
        "#V#escalate_missing_person": {
            "concept_id": "#V#escalate_missing_person",
            "relationships": {
                "invokesAction": "fail_workflow",
                "hasInputMap": ["reason=missing_person"],
            },
        },
        "#V#escalate_missing_affiliation_org": {
            "concept_id": "#V#escalate_missing_affiliation_org",
            "relationships": {
                "invokesAction": "fail_workflow",
                "hasInputMap": ["reason=missing_organisation"],
            },
        },
        "#V#escalate_affiliation_missing": {
            "concept_id": "#V#escalate_affiliation_missing",
            "relationships": {
                "invokesAction": "fail_workflow",
                "hasInputMap": ["reason=missing_affiliation"],
            },
        },
        "#V#escalate_affiliation_evidence_missing": {
            "concept_id": "#V#escalate_affiliation_evidence_missing",
            "relationships": {
                "invokesAction": "fail_workflow",
                "hasInputMap": ["reason=missing_affiliation_evidence"],
            },
        },
    }


def _build_repo_find(docs: Dict[str, Dict[str, Any]]):
    def _mock_find(
        query: MutableMapping[str, Any], projection=None, limit=None
    ) -> Iterator[Dict[str, Any]]:
        concept_ids = query.get("concept_id", {})
        if isinstance(concept_ids, dict) and "$in" in concept_ids:
            ids = concept_ids["$in"]
            return iter([docs[cid] for cid in ids if cid in docs])
        return iter([])

    return _mock_find


def _build_repo_find_one(docs: Dict[str, Dict[str, Any]]):
    def _mock_find_one(query: MutableMapping[str, Any], projection=None):
        concept_id = query.get("concept_id")
        if isinstance(concept_id, str):
            return docs.get(concept_id)
        return None

    return _mock_find_one


def _build_mock_gateway(
    payload_sequences: Dict[str, list[Dict[str, Any]] | Dict[str, Any]],
):
    gateway = MagicMock()
    gateway.enabled = True
    gateway.describe_methods.return_value = {}

    counters: Dict[str, int] = {}

    def _invoke(tool_name: str, payload: MutableMapping[str, Any] | None = None):
        del payload  # payload is not relevant for these deterministic tests.
        sequence = payload_sequences.get(tool_name)
        if isinstance(sequence, list):
            index = counters.get(tool_name, 0)
            if sequence:
                result_payload = sequence[index] if index < len(sequence) else sequence[-1]
            else:
                result_payload = {}
            counters[tool_name] = index + 1
        elif isinstance(sequence, dict):
            result_payload = sequence
        else:
            result_payload = {"result": True}

        result = MagicMock()
        result.payload = result_payload
        result.duration_ms = 5.0
        return result

    gateway.invoke.side_effect = _invoke
    return gateway


def _build_orchestrator_with_gateway(gateway):
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    with patch.object(InternalMCPChatOrchestrator, "__init__", lambda self: None):
        orchestrator = InternalMCPChatOrchestrator()  # type: ignore[call-arg]
        orchestrator._gateway = gateway  # type: ignore[assignment]
        orchestrator._logger = MagicMock()
    return orchestrator


def _load_definition(workflow_id: str, docs: Dict[str, Dict[str, Any]]) -> WorkflowDefinition:
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
            return_value="verified entity representation example workflow",
        ),
    ):
        definition = load_workflow_definition_from_vontology(workflow_id)
    assert definition is not None
    return definition


def _build_registry(gateway) -> ActionRegistry:
    registry = ActionRegistry()
    orchestrator = _build_orchestrator_with_gateway(gateway)
    registry.set_fallback_handler(orchestrator._action_mcp_tool_invoke)

    def _fail_workflow(request: WorkflowActionRequest) -> WorkflowActionResult:
        reason = str(request.inputs.get("reason") or "workflow_escalated")
        return WorkflowActionResult(status="failed", error=f"workflow_abort:{reason}")

    registry.register(ActionSpec(action_id="fail_workflow", handler=_fail_workflow))
    return registry


def _run_workflow(
    *,
    workflow_id: str,
    docs: Dict[str, Dict[str, Any]],
    payload_sequences: Dict[str, list[Dict[str, Any]] | Dict[str, Any]],
):
    definition = _load_definition(workflow_id, docs)
    gateway = _build_mock_gateway(payload_sequences)
    registry = _build_registry(gateway)
    executor = WorkflowExecutor(registry=registry, max_transitions=40)
    environment = WorkflowEnvironment(
        llm_client=None,
        gateway=gateway,
        user_namespace="#V#test_user",
    )
    result = executor.run(definition, environment=environment)
    return result, gateway


class TestVerifiedOrganisationWorkflow:
    def test_missing_organisation_branches_to_create_then_retests(self):
        docs = _build_hai_workflow_docs()
        result, gateway = _run_workflow(
            workflow_id=_HAI_WORKFLOW_ID,
            docs=docs,
            payload_sequences={
                "test_org_exists": [
                    {"result": False},
                    {"result": True},
                ],
                "create_org": {"created": True},
                "test_org_type_membership": {"result": True},
                "test_org_evidence": {"result": True},
            },
        )

        assert result.completed
        assert result.final_state == "#V#hai_completed"
        called_tools = [call.args[0] for call in gateway.invoke.call_args_list]
        assert called_tools.count("test_org_exists") == 2
        assert "create_org" in called_tools
        # Critical: tool invocation success alone must not bypass false test results.
        assert called_tools.index("create_org") < called_tools.index("test_org_type_membership")

    def test_missing_type_membership_branches_to_add_type_then_retests(self):
        docs = _build_hai_workflow_docs()
        result, gateway = _run_workflow(
            workflow_id=_HAI_WORKFLOW_ID,
            docs=docs,
            payload_sequences={
                "test_org_exists": {"result": True},
                "test_org_type_membership": [
                    {"result": False},
                    {"result": True},
                ],
                "add_org_type": {"updated": True},
                "test_org_evidence": {"result": True},
            },
        )

        assert result.completed
        assert result.final_state == "#V#hai_completed"
        called_tools = [call.args[0] for call in gateway.invoke.call_args_list]
        assert called_tools.count("add_org_type") == 1
        assert called_tools.count("test_org_type_membership") == 2

    def test_missing_evidence_branches_to_attach_source_then_retests(self):
        docs = _build_hai_workflow_docs()
        result, gateway = _run_workflow(
            workflow_id=_HAI_WORKFLOW_ID,
            docs=docs,
            payload_sequences={
                "test_org_exists": {"result": True},
                "test_org_type_membership": {"result": True},
                "test_org_evidence": [
                    {"result": False},
                    {"result": True},
                ],
                "attach_org_evidence": {"attached": True},
            },
        )

        assert result.completed
        assert result.final_state == "#V#hai_completed"
        called_tools = [call.args[0] for call in gateway.invoke.call_args_list]
        assert called_tools.count("attach_org_evidence") == 1
        assert called_tools.count("test_org_evidence") == 2

    def test_unresolvable_type_mismatch_escalates_with_failure_report(self):
        docs = _build_hai_workflow_docs()
        result, gateway = _run_workflow(
            workflow_id=_HAI_WORKFLOW_ID,
            docs=docs,
            payload_sequences={
                "test_org_exists": {"result": True},
                "test_org_type_membership": [
                    {"result": False},
                    {"result": False},
                ],
                "add_org_type": {"updated": True},
            },
        )

        assert not result.completed
        assert result.final_state == "#V#escalate_type_mismatch"
        assert result.error == "workflow_abort:type_mismatch"
        called_tools = [call.args[0] for call in gateway.invoke.call_args_list]
        assert called_tools.count("add_org_type") == 1
        # fail_workflow is handled by explicit action registration, not the MCP gateway.
        assert "fail_workflow" not in called_tools


class TestResearcherAffiliationWorkflow:
    def test_adds_affiliation_and_verifies_evidence(self):
        docs = _build_affiliation_workflow_docs()
        result, gateway = _run_workflow(
            workflow_id=_AFFILIATION_WORKFLOW_ID,
            docs=docs,
            payload_sequences={
                "test_person_exists": {"result": True},
                "test_org_exists": {"result": True},
                "test_affiliation_exists": [
                    {"result": False},
                    {"result": True},
                ],
                "add_affiliation": {"updated": True},
                "test_affiliation_evidence": [
                    {"result": False},
                    {"result": True},
                ],
                "attach_affiliation_evidence": {"attached": True},
            },
        )

        assert result.completed
        assert result.final_state == "#V#affiliation_completed"
        called_tools = [call.args[0] for call in gateway.invoke.call_args_list]
        assert called_tools.count("add_affiliation") == 1
        assert called_tools.count("attach_affiliation_evidence") == 1
        assert called_tools.count("test_affiliation_exists") == 2
        assert called_tools.count("test_affiliation_evidence") == 2
