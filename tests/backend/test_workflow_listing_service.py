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
    assert entry["metadata_resolution"]["requested_mode"] == "fast"
    assert entry["metadata_resolution"]["effective_mode"] == "fast"
    assert entry["metadata_resolution"]["resolved_vontology_metadata"] is False


def test_build_workflow_listing_entry_authoritative_metadata_keeps_lazy_definition(
    monkeypatch,
) -> None:
    registry = WorkflowRegistry()
    registry.register_lazy(
        LazyWorkflowRegistration(
            workflow_id="#V#paper_workflow",
            purpose="Fallback paper workflow purpose",
            source="vontology",
        )
    )

    def _unexpected_registration_load(*_args, **_kwargs):
        raise AssertionError("authoritative metadata must not load lazy definition")

    monkeypatch.setattr(registry, "get_registration", _unexpected_registration_load)
    monkeypatch.setattr(
        mod,
        "resolve_workflow_description",
        lambda *_args, **_kwargs: (
            "Represent scholarly papers from authoritative source metadata.",
            "text_relation:hasDescription",
        ),
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_initial_step",
        lambda _workflow_id: "#V#workflow_step_start",
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_background_launch_policy",
        lambda _workflow_id: (None, "none"),
    )

    entry = mod.build_workflow_listing_entry(
        registry=registry,
        workflow_id="#V#paper_workflow",
        resolve_vontology_metadata=True,
        metadata_mode="auto",
        metadata_reason_code="exact_workflow_id_authoritative_metadata",
    )

    assert entry["workflow_id"] == "#V#paper_workflow"
    assert entry["description"] == (
        "Represent scholarly papers from authoritative source metadata."
    )
    assert entry["description_source"] == "text_relation:hasDescription"
    assert entry["initial_state"] == "#V#workflow_step_start"
    assert entry["definition_loaded"] is False
    assert entry["definition_identity"]["build_state"] == "pending_lazy_definition"
    assert entry["metadata_resolution"]["requested_mode"] == "auto"
    assert entry["metadata_resolution"]["effective_mode"] == "authoritative"
    assert entry["metadata_resolution"]["resolved_vontology_metadata"] is True
    assert entry["metadata_resolution"]["reason_code"] == (
        "exact_workflow_id_authoritative_metadata"
    )
