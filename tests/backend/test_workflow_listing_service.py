from src.backend.workflows import workflow_listing_service as mod
from src.backend.workflows.workflow_registry import LazyWorkflowRegistration, WorkflowRegistry


def test_build_workflow_listing_entry_fast_path_uses_registration_metadata(
    monkeypatch,
) -> None:
    registry = WorkflowRegistry()
    registry.register_lazy(
        LazyWorkflowRegistration(
            workflow_id="#V#alpha_workflow",
            purpose="Alpha workflow from registration metadata",
            source="vontology",
        )
    )

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("slow Vontology resolver should not run on fast path")

    monkeypatch.setattr(mod, "resolve_workflow_description", _unexpected)
    monkeypatch.setattr(mod, "resolve_workflow_initial_step", _unexpected)
    monkeypatch.setattr(mod, "resolve_workflow_background_launch_policy", _unexpected)

    entry = mod.build_workflow_listing_entry(
        registry=registry,
        workflow_id="#V#alpha_workflow",
        resolve_vontology_metadata=False,
    )

    assert entry["workflow_id"] == "#V#alpha_workflow"
    assert entry["description"] == "Alpha workflow from registration metadata"
    assert entry["description_source"] == "registration.purpose"
    assert entry["source"] == "vontology"
    assert entry["definition_loaded"] is False
    assert entry["definition_identity"]["build_state"] == "pending_lazy_definition"

