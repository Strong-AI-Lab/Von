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


def test_introspection_projection_includes_parity_only_workflows_not_other_concepts(
    monkeypatch,
) -> None:
    public_workflow_id = "#V#public_workflow"
    restricted_workflow_id = "#V#restricted_workflow"
    restricted_vontology_only_id = "#V#restricted_vontology_only_workflow"
    checked_ids = []

    monkeypatch.setattr(mod, "should_enforce_access_control", lambda: True)

    def _filter_accessible(concept_ids):
        checked_ids.append(set(concept_ids))
        return [public_workflow_id]

    monkeypatch.setattr(
        mod,
        "filter_workflow_ids_for_current_actor",
        _filter_accessible,
    )

    projected = mod.project_workflow_introspection_payload_for_current_actor(
        {
            "definitions": [
                {"workflow_id": public_workflow_id},
                {"workflow_id": restricted_workflow_id},
            ],
            "total": 2,
            "parity_inventory": {
                "counts": {
                    "registry": 2,
                    "vontology_discovered": 2,
                    "overlap": 1,
                    "registry_only": 1,
                    "vontology_only": 1,
                },
                "registry_workflow_ids": [
                    public_workflow_id,
                    restricted_workflow_id,
                ],
                "vontology_discovered_workflow_ids": [
                    public_workflow_id,
                    restricted_vontology_only_id,
                ],
                "overlap_workflow_ids": [public_workflow_id],
                "registry_only_workflow_ids": [restricted_workflow_id],
                "vontology_only_workflow_ids": [restricted_vontology_only_id],
                "registry_sources": {
                    "counts": {"vontology": 3},
                    "source_by_workflow_id": {
                        public_workflow_id: "vontology",
                        restricted_vontology_only_id: "vontology",
                    }
                },
                "workflow_description_quality": {"counts": {"stub": 2}},
                "workflow_authority": {"missing_concept_count": 1},
                "workflow_purity": {"counters": {"workflow_count": 3}},
                "parity_policy": {"drift_detected": True},
                "diagnostics": {
                    "drift_detected": True,
                    "reason_codes": ["registry_only", "vontology_only"],
                },
                "graph_warnings_by_workflow_id": {
                    restricted_workflow_id: ["restricted metadata"],
                    restricted_vontology_only_id: ["vontology-only metadata"],
                },
                # These are related represented artefacts, not workflow IDs;
                # this projection must not make separate access decisions for them.
                "initial_state": "#V#global_workflow_step",
                "predicate": "#V#hasStep",
                "tool_id": "#V#global_workflow_tool",
            },
        },
        workflow_ids=[public_workflow_id, restricted_workflow_id],
    )

    assert checked_ids == [
        {
            public_workflow_id,
            restricted_workflow_id,
            restricted_vontology_only_id,
        }
    ]
    assert projected["definitions"] == [{"workflow_id": public_workflow_id}]
    assert projected["count"] == 1
    assert projected["total"] == 1
    assert projected["parity_inventory"]["initial_state"] == (
        "#V#global_workflow_step"
    )
    assert projected["parity_inventory"]["predicate"] == "#V#hasStep"
    assert projected["parity_inventory"]["tool_id"] == "#V#global_workflow_tool"
    assert projected["parity_inventory"]["counts"] == {
        "registry": 1,
        "vontology_discovered": 1,
        "overlap": 1,
        "registry_only": 0,
        "vontology_only": 0,
        "graph_complete": 0,
        "identity_only": 0,
    }
    assert projected["parity_inventory"]["registry_sources"]["counts"] == {
        "vontology": 1,
    }
    assert projected["parity_inventory"]["diagnostics"] == {
        "drift_detected": False,
        "severity": "ok",
        "reason_codes": [],
        "actor_scoped_projection": True,
    }
    for aggregate_key in (
        "workflow_description_quality",
        "workflow_authority",
        "workflow_purity",
        "parity_policy",
    ):
        assert aggregate_key not in projected["parity_inventory"]
    assert restricted_workflow_id not in str(projected)
    assert restricted_vontology_only_id not in str(projected)
    assert "restricted metadata" not in str(projected)
    assert "vontology-only metadata" not in str(projected)


def test_workflow_visibility_fails_closed_when_concept_authority_is_unavailable(
    monkeypatch,
) -> None:
    import src.backend.db.mongo_client as mongo_client

    monkeypatch.setattr(mod, "should_enforce_access_control", lambda: True)
    monkeypatch.setattr(mongo_client, "get_concepts_collection", lambda: None)
    monkeypatch.setattr(
        mod,
        "filter_accessible_concept_ids",
        lambda _concept_ids: (_ for _ in ()).throw(
            AssertionError("filter must not run without concept authority")
        ),
    )

    assert mod.filter_workflow_ids_for_current_actor(
        ["#V#warm_registry_workflow"]
    ) == []


def test_workflow_visibility_fails_closed_when_authority_query_raises(
    monkeypatch,
) -> None:
    import src.backend.db.mongo_client as mongo_client

    monkeypatch.setattr(mod, "should_enforce_access_control", lambda: True)
    monkeypatch.setattr(
        mongo_client,
        "get_concepts_collection",
        lambda: object(),
    )
    monkeypatch.setattr(
        mod,
        "filter_accessible_concept_ids",
        lambda _concept_ids: (_ for _ in ()).throw(
            RuntimeError("synthetic visibility query failure")
        ),
    )

    assert mod.filter_workflow_ids_for_current_actor(
        ["#V#warm_registry_workflow"]
    ) == []


def test_introspection_projection_redacts_warm_ids_during_authority_outage(
    monkeypatch,
) -> None:
    import src.backend.db.mongo_client as mongo_client

    workflow_id = "#V#warm_registry_workflow"
    monkeypatch.setattr(mod, "should_enforce_access_control", lambda: True)
    monkeypatch.setattr(mongo_client, "get_concepts_collection", lambda: None)

    projected = mod.project_workflow_introspection_payload_for_current_actor(
        {
            "definitions": [
                {
                    "workflow_id": workflow_id,
                    "description": "Warm process-global workflow metadata.",
                }
            ],
            "total": 1,
            "parity_inventory": {
                "registry_workflow_ids": [workflow_id],
                "counts": {"registry": 1},
                "summary_text": f"Loaded {workflow_id}",
            },
        },
        workflow_ids=[workflow_id],
    )

    assert projected["definitions"] == []
    assert projected["count"] == 0
    assert projected["total"] == 0
    assert projected["parity_inventory"]["counts"]["registry"] == 0
    assert projected["parity_inventory"]["visibility_projection"] == {
        "actor_scoped": True,
        "visible_workflow_count": 0,
    }
    assert workflow_id not in str(projected)
