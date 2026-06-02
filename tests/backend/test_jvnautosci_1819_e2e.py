import pytest
from unittest.mock import MagicMock, patch

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    OrchestratorResult,
)
from src.backend.workflows.definitions import TOOL_CALLING_WORKFLOW_ID
from src.backend.workflows import WorkflowDefinition, WorkflowStateSpec
from src.backend.workflows.engine import WorkflowResult


@pytest.fixture
def mock_orchestrator():
    with patch(
        "src.backend.integrations.internal_mcp.orchestrator.PromptTemplateService"
    ), patch(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPGateway"
    ) as mock_gateway_class:
        mock_gateway = mock_gateway_class.return_value
        orchestrator = InternalMCPChatOrchestrator(gateway=mock_gateway)
        return orchestrator


def _make_workflow_definition(workflow_id, metadata):
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={"start": WorkflowStateSpec(state_id="start")},
        metadata=metadata,
    )


def _install_custom_workflow_selection(mock_orchestrator, wf_id, mock_llm):
    mock_orchestrator._workflow_selector = MagicMock()
    mock_orchestrator._workflow_selector.enabled.return_value = True
    mock_orchestrator._workflow_selector.prepare_selection_prompt.return_value = (
        MagicMock(
            prompt_text="select",
            prompt_id="selector_prompt",
            discovered_workflow_ids=[],
            candidate_entries=(),
            requested_prompt_ids=[],
            prompt_provenance={},
            policy_recommendation={},
            prompt_failure_reason=None,
            prompt_failure_detail=None,
        )
    )
    mock_llm.return_value = ("selected_text", "model", None)
    mock_orchestrator._workflow_selector.resolve_selection.return_value = MagicMock(
        workflow_id=wf_id,
        execution_mode="custom_workflow",
        verdict="rag_selected",
        prompt_id="test",
        discovered_workflow_ids=[],
        confidence_score=1.0,
        reasoning="test",
        selection_metadata={},
    )


def test_probe_reports_missing_required_context_key(mock_orchestrator):
    wf_id = "#V#arxiv_paper_representation_workflow"
    workflow_definition = _make_workflow_definition(
        wf_id,
        metadata={
            "launch_contract": {
                "schema_version": "launch_contract.v1",
                "preconditions": [
                    {
                        "type": "context_key_present",
                        "key": "file_copy_concept_id",
                        "required": True,
                    }
                ],
            }
        },
    )

    with patch.object(
        mock_orchestrator,
        "_resolve_workflow_registration_and_definition",
        return_value=(None, workflow_definition),
    ):
        probe = mock_orchestrator._probe_custom_workflow_launchability(
            wf_id,
            build_custom_workflow_dispatch_data=lambda **_: {},
        )

    assert probe["launchable"] is False
    assert probe["pre_action_validation"]["applied"] is True
    assert probe["pre_action_validation"]["reason_code"] == "context_key_present"
    assert probe["pre_action_validation"]["symbol"] == "file_copy_concept_id"
    assert (
        "Missing required context key: file_copy_concept_id"
        in probe["pre_action_validation"]["message"]
    )


def test_probe_allows_satisfied_contract(mock_orchestrator):
    wf_id = "#V#arxiv_paper_representation_workflow"
    workflow_definition = _make_workflow_definition(
        wf_id,
        metadata={
            "launch_contract": {
                "schema_version": "launch_contract.v1",
                "preconditions": [
                    {
                        "type": "context_key_present",
                        "key": "file_copy_concept_id",
                        "required": True,
                    }
                ],
            }
        },
    )

    with patch.object(
        mock_orchestrator,
        "_resolve_workflow_registration_and_definition",
        return_value=(None, workflow_definition),
    ):
        probe = mock_orchestrator._probe_custom_workflow_launchability(
            wf_id,
            build_custom_workflow_dispatch_data=lambda **_: {
                "file_copy_concept_id": "#V#copy_123"
            },
        )

    assert probe["launchable"] is True
    assert probe["pre_action_validation"]["ok"] is True
    assert probe["launch_input_resolution"]["unresolved_required_inputs"] == []


def test_e2e_dispatch_gate_falls_back_to_tool_pipeline_when_not_launchable(
    mock_orchestrator,
):
    wf_id = "#V#arxiv_paper_representation_workflow"
    workflow_definition = _make_workflow_definition(
        wf_id,
        metadata={
            "launch_contract": {
                "schema_version": "launch_contract.v1",
                "preconditions": [
                    {
                        "type": "context_key_present",
                        "key": "file_copy_concept_id",
                        "required": True,
                    }
                ],
            }
        },
    )

    with patch.object(
        mock_orchestrator, "_prepare_selector_candidates", return_value=([], [], [])
    ), patch.object(
        mock_orchestrator, "_run_llm_with_fallbacks"
    ) as mock_llm, patch.object(
        mock_orchestrator,
        "_resolve_workflow_registration_and_definition",
        return_value=(None, workflow_definition),
    ), patch.object(
        mock_orchestrator,
        "_maybe_apply_narration_routing",
        side_effect=lambda screen_text, **kwargs: screen_text,
    ), patch.object(
        mock_orchestrator,
        "_build_ontology_preflight",
        return_value=MagicMock(message="", telemetry={}),
    ), patch.object(
        mock_orchestrator, "_build_augmented_context", return_value=[]
    ), patch.object(
        mock_orchestrator, "_load_workflow_model_policy", return_value=({}, {})
    ), patch(
        "src.backend.integrations.internal_mcp.orchestrator.get_model_registry_snapshot",
        return_value={},
        create=True,
    ), patch.object(
        mock_orchestrator,
        "_resolve_workflow_id_for_action_contract",
        return_value=TOOL_CALLING_WORKFLOW_ID,
    ), patch.object(mock_orchestrator, "execute_workflow") as mock_execute:
        _install_custom_workflow_selection(mock_orchestrator, wf_id, mock_llm)
        mock_execute.return_value = WorkflowResult(
            data={"response_text": "Fallback tool response"},
            final_state="completed",
            completed=True,
        )
        result = mock_orchestrator.run(
            prompt="Summarize this arxiv:2403.05530",
            context=[],
            llm_client=MagicMock(),
            model="test_model",
            user_namespace="#V#test_user",
        )

    assert isinstance(result, OrchestratorResult)
    assert mock_execute.called
    assert mock_execute.call_args.args[0] == TOOL_CALLING_WORKFLOW_ID
    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selected_custom_workflow_launchability_requires_safe_general_fallback"
        for entry in result.aux_llm_calls
    )
    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_dispatch_prepare_step"
        and entry.get("step_id") == "launchability_probe"
        and "Missing required context key: file_copy_concept_id"
        in str(entry.get("result_summary"))
        for entry in result.aux_llm_calls
    )
    assert any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_dispatch_prepare_step"
        and entry.get("step_id") == "safe_general_fallback"
        and "using the general tool workflow instead"
        in str(entry.get("result_summary"))
        for entry in result.aux_llm_calls
    )


def test_e2e_dispatch_gate_allows_satisfied_contract(mock_orchestrator):
    wf_id = "#V#arxiv_paper_representation_workflow"
    workflow_definition = _make_workflow_definition(wf_id, metadata={})

    with patch.object(
        mock_orchestrator, "_prepare_selector_candidates", return_value=([], [], [])
    ), patch.object(
        mock_orchestrator, "_run_llm_with_fallbacks"
    ) as mock_llm, patch.object(
        mock_orchestrator,
        "_resolve_workflow_registration_and_definition",
        return_value=(None, workflow_definition),
    ), patch.object(
        mock_orchestrator,
        "_probe_custom_workflow_launchability",
        return_value={
            "workflow_id": wf_id,
            "initial_state_id": "start",
            "launchable": True,
            "launch_input_resolution": {
                "status": "resolved",
                "contract_source": "explicit_contract",
                "resolved_inputs": ["file_copy_concept_id"],
                "unresolved_required_inputs": [],
            },
            "pre_action_validation": {
                "applied": True,
                "ok": True,
                "reason_code": None,
                "symbol": None,
                "message": None,
            },
        },
    ), patch.object(
        mock_orchestrator,
        "_build_ontology_preflight",
        return_value=MagicMock(message="", telemetry={}),
    ), patch.object(
        mock_orchestrator, "_build_augmented_context", return_value=[]
    ), patch.object(
        mock_orchestrator, "_load_workflow_model_policy", return_value=({}, {})
    ), patch(
        "src.backend.integrations.internal_mcp.orchestrator.get_model_registry_snapshot",
        return_value={},
        create=True,
    ), patch.object(mock_orchestrator, "execute_workflow") as mock_execute:
        _install_custom_workflow_selection(mock_orchestrator, wf_id, mock_llm)
        mock_execute.return_value = WorkflowResult(
            data={"orchestrator_result": {"response_text": "Workflow Executed Successfully"}},
            final_state="completed",
            completed=True,
        )
        result = mock_orchestrator.run(
            prompt="Summarize paper",
            context=[],
            llm_client=MagicMock(),
            model="test_model",
            user_namespace="#V#test_user",
        )

    assert isinstance(result, OrchestratorResult)
    assert mock_execute.call_args.args[0] == wf_id
    assert result.response_text == "Workflow Executed Successfully"
    assert mock_execute.called
