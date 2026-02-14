from __future__ import annotations

from typing import Any, Dict

from src.backend.services import workflow_mapping_migration_service as migration_service


def _workflow_graph_for_step(
    *,
    step_id: str,
    action_id: str,
    input_mapping_ids: list[str] | None = None,
    output_mapping_ids: list[str] | None = None,
) -> Dict[str, Any]:
    return {
        "workflow_id": "#V#test_workflow",
        "initial_step": step_id,
        "steps": [
            {
                "step_id": step_id,
                "invokes_action": action_id,
                "context_input_mappings": input_mapping_ids or [],
                "tool_output_context_mappings": output_mapping_ids or [],
            }
        ],
    }


def test_migrate_workflow_mapping_specs_dry_run_builds_input_spec(monkeypatch):
    mapping_id = "#V#workflow_mapping_target_type_id_to_concept_id_param"
    graph = _workflow_graph_for_step(
        step_id="#V#step_identify",
        action_id="fetch_concept",
        input_mapping_ids=[mapping_id],
    )

    monkeypatch.setattr(
        migration_service,
        "discover_workflow_ids",
        lambda: ["#V#test_workflow"],
    )
    monkeypatch.setattr(
        migration_service,
        "build_workflow_process_graph",
        lambda _workflow_id: (graph, []),
    )
    monkeypatch.setattr(
        migration_service,
        "_fetch_concepts_by_id",
        lambda _mapping_ids: {mapping_id: {"concept_id": mapping_id, "relationships": {}}},
    )

    writes: list[Dict[str, Any]] = []

    def _fake_update_concept(*, concept_id: str, update_data: Dict[str, Any]):
        writes.append({"concept_id": concept_id, "update_data": update_data})
        return {"concept_id": concept_id}

    monkeypatch.setattr(migration_service, "update_concept", _fake_update_concept)

    report = migration_service.migrate_workflow_mapping_specs(dry_run=True)

    assert report["success"] is True
    assert report["stats"]["migrated"] == 1
    assert report["stats"]["updated"] == 0
    assert writes == []
    detail = next(item for item in report["details"] if item["status"] == "would_migrate")
    assert detail["target_spec"] == {
        "schema_version": 1,
        "mapping_type": "context_key_to_tool_param",
        "workflow_step_id": "#V#step_identify",
        "tool_id": "fetch_concept",
        "context_key_concept_id": "#V#workflow_context_key_target_type_id",
        "tool_param_name": "concept_id",
    }


def test_migrate_workflow_mapping_specs_recovers_from_invalid_structured_spec(monkeypatch):
    mapping_id = "#V#workflow_mapping_target_type_id_to_concept_id_param"
    graph = _workflow_graph_for_step(
        step_id="#V#step_identify",
        action_id="fetch_concept",
        input_mapping_ids=[mapping_id],
    )
    mapping_doc = {
        "concept_id": mapping_id,
        "concept_data": {
            "preserved_fields": {
                "workflow_mapping_spec": {
                    "schema_version": 1,
                    "mapping_type": "context_key_to_tool_param",
                    "workflow_step_id": "#V#step_identify",
                    "tool_id": "wrong_tool",
                    "context_key_concept_id": "#V#workflow_context_key_target_type_id",
                    "tool_param_name": "concept_id",
                },
                "description": (
                    "Bind context key 'target_type_id' to the 'concept_id' parameter."
                ),
            }
        },
        "relationships": {},
    }

    monkeypatch.setattr(
        migration_service,
        "discover_workflow_ids",
        lambda: ["#V#test_workflow"],
    )
    monkeypatch.setattr(
        migration_service,
        "build_workflow_process_graph",
        lambda _workflow_id: (graph, []),
    )
    monkeypatch.setattr(
        migration_service,
        "_fetch_concepts_by_id",
        lambda _mapping_ids: {mapping_id: mapping_doc},
    )

    writes: list[Dict[str, Any]] = []

    def _fake_update_concept(*, concept_id: str, update_data: Dict[str, Any]):
        writes.append({"concept_id": concept_id, "update_data": update_data})
        return {"concept_id": concept_id}

    monkeypatch.setattr(migration_service, "update_concept", _fake_update_concept)

    report = migration_service.migrate_workflow_mapping_specs(dry_run=False)

    assert report["success"] is True
    assert report["stats"]["migrated"] == 1
    assert report["stats"]["updated"] == 1
    assert len(writes) == 1
    migrated_spec = writes[0]["update_data"][
        "concept_data.preserved_fields.workflow_mapping_spec"
    ]
    assert migrated_spec["tool_id"] == "fetch_concept"
    migrated_detail = next(item for item in report["details"] if item["status"] == "migrated")
    assert migrated_detail["parse_source"] == "legacy_recovery"


def test_migrate_workflow_mapping_specs_skips_already_structured(monkeypatch):
    mapping_id = "#V#workflow_mapping_target_type_id_to_concept_id_param"
    graph = _workflow_graph_for_step(
        step_id="#V#step_identify",
        action_id="fetch_concept",
        input_mapping_ids=[mapping_id],
    )
    mapping_doc = {
        "concept_id": mapping_id,
        "concept_data": {
            "preserved_fields": {
                "workflow_mapping_spec": {
                    "schema_version": 1,
                    "mapping_type": "context_key_to_tool_param",
                    "workflow_step_id": "#V#step_identify",
                    "tool_id": "fetch_concept",
                    "context_key_concept_id": "#V#workflow_context_key_target_type_id",
                    "tool_param_name": "concept_id",
                }
            }
        },
        "relationships": {},
    }

    monkeypatch.setattr(
        migration_service,
        "discover_workflow_ids",
        lambda: ["#V#test_workflow"],
    )
    monkeypatch.setattr(
        migration_service,
        "build_workflow_process_graph",
        lambda _workflow_id: (graph, []),
    )
    monkeypatch.setattr(
        migration_service,
        "_fetch_concepts_by_id",
        lambda _mapping_ids: {mapping_id: mapping_doc},
    )
    monkeypatch.setattr(
        migration_service,
        "update_concept",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not update")),
    )

    report = migration_service.migrate_workflow_mapping_specs(dry_run=True)

    assert report["success"] is True
    assert report["stats"]["already_structured"] == 1
    assert report["stats"]["migrated"] == 0


def test_migrate_workflow_mapping_specs_skips_conflicting_contexts(monkeypatch):
    mapping_id = "#V#workflow_mapping_target_type_id_to_concept_id_param"
    graph = {
        "workflow_id": "#V#test_workflow",
        "initial_step": "#V#step_a",
        "steps": [
            {
                "step_id": "#V#step_a",
                "invokes_action": "fetch_concept",
                "context_input_mappings": [mapping_id],
                "tool_output_context_mappings": [],
            },
            {
                "step_id": "#V#step_b",
                "invokes_action": "search_concepts",
                "context_input_mappings": [mapping_id],
                "tool_output_context_mappings": [],
            },
        ],
    }

    monkeypatch.setattr(
        migration_service,
        "discover_workflow_ids",
        lambda: ["#V#test_workflow"],
    )
    monkeypatch.setattr(
        migration_service,
        "build_workflow_process_graph",
        lambda _workflow_id: (graph, []),
    )
    monkeypatch.setattr(
        migration_service,
        "_fetch_concepts_by_id",
        lambda _mapping_ids: {mapping_id: {"concept_id": mapping_id}},
    )

    report = migration_service.migrate_workflow_mapping_specs(dry_run=True)

    assert report["success"] is True
    assert report["stats"]["skipped_conflicting_context"] == 1
    assert report["stats"]["migrated"] == 0

