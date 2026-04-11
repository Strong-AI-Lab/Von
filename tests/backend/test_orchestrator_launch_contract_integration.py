import pytest
from unittest.mock import MagicMock, patch
from src.backend.integrations.internal_mcp.orchestrator import InternalMCPOrchestrator
from src.backend.workflows import WorkflowDefinition, WorkflowStateSpec

@pytest.fixture
def mock_orchestrator():
    with patch("src.backend.integrations.internal_mcp.orchestrator.PromptTemplateService"), \
         patch("src.backend.integrations.internal_mcp.orchestrator.InternalMCPGateway"):
        orchestrator = InternalMCPOrchestrator()
        return orchestrator

def test_orchestrator_blocks_launch_when_contract_unsatisfied(mock_orchestrator):
    # Setup: a workflow with a contract requiring 'missing_key'
    wf_id = "#V#test_contract_workflow"
    contract = {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {"type": "context_key_present", "key": "required_data", "required": True}
        ]
    }
    
    mock_wf_def = WorkflowDefinition(
        workflow_id=wf_id,
        initial_state="start",
        states={"start": WorkflowStateSpec(state_id="start")},
        metadata={"launch_contract": contract}
    )
    
    with patch.object(mock_orchestrator, "_resolve_workflow_registration_and_definition", return_value=(None, mock_wf_def)), \
         patch("src.backend.integrations.internal_mcp.orchestrator._build_custom_workflow_dispatch_data", return_value={}), \
         patch("src.backend.integrations.internal_mcp.orchestrator._maybe_apply_narration_routing", side_effect=lambda text, **kwargs: text):
        
        # We manually call the internal probe to verify it blocks
        probe = mock_orchestrator._get_cached_custom_workflow_launchability_probe(wf_id)
        
        assert probe["launchable"] is False
        assert probe["pre_action_validation"]["ok"] is False
        assert probe["pre_action_validation"]["reason_code"] == "context_key_present"
        assert probe["pre_action_validation"]["symbol"] == "required_data"
        assert "Missing required context key: required_data" in probe["pre_action_validation"]["message"]

def test_orchestrator_allows_launch_when_contract_satisfied(mock_orchestrator):
    wf_id = "#V#test_contract_workflow"
    contract = {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {"type": "context_key_present", "key": "required_data", "required": True}
        ]
    }
    
    mock_wf_def = WorkflowDefinition(
        workflow_id=wf_id,
        initial_state="start",
        states={"start": WorkflowStateSpec(state_id="start")},
        metadata={"launch_contract": contract}
    )
    
    # Provide the required key in the probe data
    probe_data = {"required_data": "some_value"}
    
    with patch.object(mock_orchestrator, "_resolve_workflow_registration_and_definition", return_value=(None, mock_wf_def)), \
         patch("src.backend.integrations.internal_mcp.orchestrator._build_custom_workflow_dispatch_data", return_value=probe_data):
        
        probe = mock_orchestrator._get_cached_custom_workflow_launchability_probe(wf_id)
        
        assert probe["launchable"] is True
        assert probe["pre_action_validation"]["ok"] is True

def test_orchestrator_synthesizes_contract_for_legacy_workflow(mock_orchestrator):
    wf_id = "#V#legacy_workflow"
    # No launch_contract, but initial state has reads_context_keys
    mock_wf_def = WorkflowDefinition(
        workflow_id=wf_id,
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                metadata={"reads_context_keys": ["legacy_key"]}
            )
        },
        metadata={} # No explicit contract
    )
    
    with patch.object(mock_orchestrator, "_resolve_workflow_registration_and_definition", return_value=(None, mock_wf_def)), \
         patch("src.backend.integrations.internal_mcp.orchestrator._build_custom_workflow_dispatch_data", return_value={}):
        
        probe = mock_orchestrator._get_cached_custom_workflow_launchability_probe(wf_id)
        
        # Should have synthesized a contract requiring 'legacy_key'
        assert probe["launchable"] is False
        assert probe["pre_action_validation"]["symbol"] == "legacy_key"
        assert "Missing required context key: legacy_key" in probe["pre_action_validation"]["message"]
