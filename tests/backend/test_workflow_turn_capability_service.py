from __future__ import annotations


def _capability():
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
    )

    return WorkflowTurnCapability(
        name="represented_workflow_test",
        workflow_id="#V#test_workflow",
        display_name="Test workflow",
        description="Produce a represented work product.",
        relevance_score=0.91,
        input_schema={"type": "object", "properties": {}},
        launch_input_contract_source="text_relation:test",
    )


def test_discovery_exposes_only_executable_routing_eligible_workflows(
    monkeypatch,
):
    from src.backend.services.workflow_turn_capability_service import (
        discover_turn_workflow_capabilities,
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service."
        "discover_workflows_for_turn",
        lambda *_args, **_kwargs: {
            "matches": [
                {
                    "concept_id": "#V#usable_workflow",
                    "name": "Usable workflow",
                    "description": "Produce the requested reusable work product.",
                    "relevance_score": 0.87,
                    "is_executable": True,
                    "routing_eligible": True,
                    "match_source": "semantic",
                    "routing_index_metadata": {
                        "required_tools": [
                            "fetch_concept",
                            "find_relations_with_argument",
                        ],
                        "compact_executability": {
                            "step_count": 3,
                        },
                    },
                },
                {
                    "concept_id": "#V#draft_workflow",
                    "name": "Draft workflow",
                    "relevance_score": 0.99,
                    "is_executable": True,
                    "routing_eligible": False,
                },
                {
                    "concept_id": "#V#broken_workflow",
                    "name": "Broken workflow",
                    "relevance_score": 0.98,
                    "is_executable": False,
                    "routing_eligible": True,
                },
            ],
            "candidate_count": 3,
            "search_time_ms": 12.5,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.workflows.vontology_loader."
        "resolve_workflow_launch_input_contract",
        lambda workflow_id: (
            {
                "required_inputs": ["record_id", "prompt"],
                "input_mappings": [
                    {
                        "target_context_key": "prompt",
                        "source_expression": "inputs.prompt",
                        "required": True,
                    },
                    {
                        "target_context_key": "record_id",
                        "source_expression": "inputs.record_id",
                        "required": True,
                        "description": "The represented record to process.",
                    },
                ],
            },
            "text_relation:launch_contract",
        ),
    )

    capabilities, diagnostic = discover_turn_workflow_capabilities(
        "produce the reusable work product",
        namespace="#V#user@org",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        turn_id="turn-123",
        registered_capability_categories={
            "fetch_concept": "read",
            "find_relations_with_argument": "read",
        },
    )

    assert [item.workflow_id for item in capabilities] == ["#V#usable_workflow"]
    capability = capabilities[0]
    assert capability.name.startswith("represented_workflow_")
    inputs_schema = capability.input_schema["properties"]["inputs"]
    assert list(inputs_schema["properties"]) == ["record_id"]
    assert inputs_schema["required"] == ["record_id"]
    assert "prompt" in capability.input_schema["x-von-server-provided-inputs"]
    assert capability.semantic_effect is False
    assert capability.semantic_effect_source == ("declared_registered_read_components")
    catalogue_entry = capability.to_catalogue_entry()
    assert catalogue_entry["semantic_effect"] is False
    assert catalogue_entry["effect_profile"] == {
        "schema_version": "capability_effect_profile.v1",
        "semantic_effect": False,
        "semantic_effect_source": "declared_registered_read_components",
        "operational_state_effect": True,
        "operational_state_effect_reason": (
            "workflow invocation creates or advances a durable instance"
        ),
    }
    assert catalogue_entry["plan_profile"]["component_capability_names"] == [
        "fetch_concept",
        "find_relations_with_argument",
    ]
    assert catalogue_entry["plan_profile"]["cost_profile"]["declared_step_count"] == 3
    assert diagnostic["status"] == "completed"
    assert diagnostic["match_count"] == 1
    assert diagnostic["candidate_count"] == 3


def test_discovery_requires_authenticated_actor_without_querying_index(
    monkeypatch,
):
    from src.backend.services.workflow_turn_capability_service import (
        discover_turn_workflow_capabilities,
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service."
        "discover_workflows_for_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("actorless discovery must not query the index")
        ),
    )

    capabilities, diagnostic = discover_turn_workflow_capabilities(
        "produce a represented work product",
        namespace="#V#user@org",
        user_concept_id=None,
        organisation_concept_id="#V#org",
        turn_id="turn-123",
    )

    assert capabilities == []
    assert diagnostic["status"] == "authenticated_actor_required"


def test_discovery_marks_declared_write_workflow_without_hiding_unknowns(
    monkeypatch,
):
    from src.backend.services.workflow_turn_capability_service import (
        discover_turn_workflow_capabilities,
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda *_args, **_kwargs: {
            "matches": [
                {
                    "concept_id": "#V#write_workflow",
                    "name": "Write workflow",
                    "description": "Create a represented work product.",
                    "relevance_score": 0.9,
                    "is_executable": True,
                    "routing_eligible": True,
                    "routing_index_metadata": {
                        "required_tools": ["create_concepts"],
                    },
                },
                {
                    "concept_id": "#V#unknown_workflow",
                    "name": "Unknown workflow",
                    "description": "Run an unclassified represented process.",
                    "relevance_score": 0.8,
                    "is_executable": True,
                    "routing_eligible": True,
                    "routing_index_metadata": {
                        "required_tools": ["unavailable_private_operation"],
                    },
                },
            ],
            "candidate_count": 2,
            "errors": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.workflows.vontology_loader.resolve_workflow_launch_input_contract",
        lambda _workflow_id: (None, None),
    )

    capabilities, _diagnostic = discover_turn_workflow_capabilities(
        "create or inspect a represented work product",
        namespace="#V#user@org",
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        turn_id="turn-effects",
        registered_capability_categories={
            "create_concepts": "write",
        },
    )

    by_id = {item.workflow_id: item for item in capabilities}
    assert by_id["#V#write_workflow"].semantic_effect is True
    assert by_id["#V#write_workflow"].semantic_effect_source == (
        "declared_registered_write_component"
    )
    assert by_id["#V#unknown_workflow"].semantic_effect is None
    unknown_entry = by_id["#V#unknown_workflow"].to_catalogue_entry()
    assert unknown_entry["semantic_effect"] is None
    assert unknown_entry["effect_profile"]["operational_state_effect"] is True


def test_execution_arguments_bind_workflow_and_actor_server_side():
    from src.backend.services.workflow_turn_capability_service import (
        build_workflow_execution_arguments,
    )

    effective, diagnostic = build_workflow_execution_arguments(
        _capability(),
        {
            "workflow_id": "#V#spoofed_workflow",
            "user_id": "#V#spoofed_user",
            "org_id": "#V#spoofed_org",
            "namespace": "#V#spoofed@org",
            "inputs": {
                "record_id": "#V#record",
                "user_concept_id": "#V#spoofed_user",
            },
            "await_terminal": "false",
            "timeout_seconds": 500,
        },
        prompt="Process this record.",
        context=[{"role": "user", "content": "Earlier context"}],
        request_workflow_launch_inputs={
            "file_copy_concept_id": "#V#authorised_file",
            "organisation_concept_id": "#V#spoofed_org",
        },
        user_concept_id="#V#real_user",
        organisation_concept_id="#V#real_org",
        namespace="#V#real_user@real_org",
        maximum_wait_seconds=20,
    )

    assert effective["workflow_id"] == "#V#test_workflow"
    assert effective["user_id"] == "#V#real_user"
    assert effective["org_id"] == "#V#real_org"
    assert effective["namespace"] == "#V#real_user@real_org"
    assert effective["await_terminal"] is False
    assert effective["timeout_seconds"] == 20
    assert effective["inputs"]["record_id"] == "#V#record"
    assert effective["inputs"]["file_copy_concept_id"] == "#V#authorised_file"
    assert effective["inputs"]["user_concept_id"] == "#V#real_user"
    assert effective["inputs"]["organisation_concept_id"] == "#V#real_org"
    assert effective["inputs"]["prompt"] == "Process this record."
    assert effective["inputs"]["augmented_context"] == [
        {"role": "user", "content": "Earlier context"}
    ]
    assert diagnostic["ignored_model_arguments"] == [
        "namespace",
        "org_id",
        "user_id",
        "workflow_id",
    ]


def test_workflow_receipt_distinguishes_completion_partial_failure_and_no_start():
    from src.backend.services.workflow_turn_capability_service import (
        normalise_workflow_effect_receipt,
    )

    capability = _capability()
    completed = normalise_workflow_effect_receipt(
        {
            "success": True,
            "instance_id": "instance-1",
            "created_new": True,
            "final_status": "completed",
        },
        capability=capability,
    )
    partial = normalise_workflow_effect_receipt(
        {
            "success": True,
            "instance_id": "instance-2",
            "created_new": True,
            "final_status": "running",
            "timed_out": True,
        },
        capability=capability,
    )
    terminal_failure = normalise_workflow_effect_receipt(
        {
            "success": True,
            "instance_id": "instance-3",
            "created_new": True,
            "final_status": "failed",
        },
        capability=capability,
    )
    no_start = normalise_workflow_effect_receipt(
        {
            "success": False,
            "error_code": "workflow_submission_failed",
        },
        capability=capability,
    )
    indeterminate = normalise_workflow_effect_receipt(
        {
            "success": False,
            "status": "timed_out",
            "error_code": "tool_timeout_outcome_unknown",
            "mutation_outcome": "unknown",
        },
        capability=capability,
    )
    durable_timeout = normalise_workflow_effect_receipt(
        {
            "success": False,
            "status": "timed_out",
            "error_code": "tool_timeout_after_durable_submission",
            "mutation_outcome": "partial",
            "instance_id": "instance-4",
            "created_new": False,
            "workflow_id": "#V#test_workflow",
        },
        capability=capability,
    )

    assert completed["effect_status"] == "succeeded"
    assert completed["changed"] is True
    assert partial["effect_status"] == "partial"
    assert partial["recovery_affordances"][0]["arguments"] == {
        "instance_id": "instance-2"
    }
    assert terminal_failure["effect_status"] == "failed"
    assert terminal_failure["changed"] is True
    assert terminal_failure["mutation_outcome"] == "partial"
    assert terminal_failure["recovery_affordances"][0]["arguments"] == {
        "instance_id": "instance-3"
    }
    assert no_start["effect_status"] == "failed"
    assert no_start["changed"] is False
    assert no_start["mutation_outcome"] == "not_started"
    assert indeterminate["effect_status"] == "indeterminate"
    assert indeterminate["changed"] is None
    assert indeterminate["mutation_outcome"] == "unknown"
    assert indeterminate["recovery_affordances"][-1]["action_type"] == (
        "inspect_operation_state_before_retry"
    )
    assert durable_timeout["effect_status"] == "partial"
    assert durable_timeout["changed"] is False
    assert durable_timeout["mutation_outcome"] == "partial"
    assert durable_timeout["recovery_affordances"][0]["arguments"] == {
        "instance_id": "instance-4"
    }
