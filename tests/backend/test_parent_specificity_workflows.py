"""Tests for parent-specificity dossier and rumination workflows."""

from __future__ import annotations


def test_dossier_workflow_definition_structure() -> None:
    from src.backend.workflows.durable.parent_specificity_concept_dossier_workflow import (
        PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
        build_parent_specificity_concept_dossier_workflow,
    )

    workflow = build_parent_specificity_concept_dossier_workflow()
    assert workflow.workflow_id == PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID
    assert workflow.initial_state == "collect"
    assert set(workflow.states.keys()) == {"collect", "complete", "failed"}
    assert workflow.states["complete"].terminal is True
    assert workflow.states["failed"].terminal is True


def test_collect_dossier_gathers_multilingual_texts_and_relations(monkeypatch) -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import (
        parent_specificity_concept_dossier_workflow as mod,
    )

    monkeypatch.setattr(
        mod,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "computed_kind": "type",
            "relationships": {
                "is_a_type_of": ["#V#researcher"],
                "#V#has_affiliation": ["#V#organisation"],
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "get_texts_for_concept",
        lambda concept_id, limit=120: [
            {"predicate": "hasName", "lang": "en-NZ", "text": "Graph theorist"},
            {"predicate": "hasDescription", "lang": "en-NZ", "text": "Studies graph structure."},
            {"predicate": "hasDescription", "lang": "fr", "text": "Étudie la structure des graphes."},
        ],
    )
    monkeypatch.setattr(
        mod,
        "find_relations_with_argument",
        lambda *_args, **_kwargs: {
            "hits": [
                {
                    "predicate_concept_id": "#V#has_affiliation",
                    "argument_indexes": [1],
                    "target_value": "#V#organisation",
                },
                {
                    "predicate_concept_id": "#V#is_about",
                    "argument_indexes": [2],
                    "target_value": "#V#graph_theory",
                },
            ]
        },
    )
    monkeypatch.setattr(
        mod,
        "get_concept_display_name_with_names_fallback",
        lambda _concept: "Graph theorist",
    )

    request = WorkflowActionRequest(
        action_id="parent_specificity.collect_dossier",
        inputs={"concept_id": "#V#graph_theorist"},
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )
    result = mod._handle_collect_dossier(request)

    assert result.ok
    dossier = result.outputs["concept_dossier"]
    assert dossier["display_name"] == "Graph theorist"
    assert sorted(dossier["relationship_summary"]["non_hierarchy_predicates"]) == [
        "#V#has_affiliation"
    ]
    assert len(dossier["multilingual_descriptions"]) == 2
    assert result.outputs["concept_dossier_summary"]["languages_seen"] == ["en-NZ", "fr"]


def test_parent_specificity_rumination_workflow_definition_structure() -> None:
    from src.backend.workflows.durable.parent_specificity_rumination_workflow import (
        build_parent_specificity_rumination_workflow,
    )
    from src.backend.workflows.parent_specificity_workflow_contracts import (
        PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID,
        PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS,
        PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS,
    )
    from src.backend.workflows.subworkflow_contracts import (
        WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
    )

    workflow = build_parent_specificity_rumination_workflow()
    assert workflow.initial_state == "assess"
    assert set(workflow.states.keys()) == {
        "assess",
        "prepare_candidate",
        "gather_dossier",
        "analyse_candidate",
        "apply_candidate",
        "complete",
        "failed",
    }
    assert workflow.states["complete"].terminal is True
    gather_dossier = workflow.states["gather_dossier"]
    assert gather_dossier.actions[0].inputs["failure_mode"] == (
        WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE
    )
    assert gather_dossier.actions[0].inputs["concept_id"]["$mapping_concept_id"] == (
        PARENT_SPECIFICITY_DOSSIER_CONTEXT_INPUT_MAPPING_CONCEPT_ID
    )
    assert gather_dossier.metadata["writes_context_keys"] == list(
        PARENT_SPECIFICITY_DOSSIER_WRITES_CONTEXT_KEYS
    )
    assert gather_dossier.metadata["tool_output_context_mappings"] == [
        {
            "tool_output_field": item.tool_output_field,
            "context_key": item.context_key,
            "mapping_concept_id": item.concept_id,
        }
        for item in PARENT_SPECIFICITY_DOSSIER_TOOL_OUTPUT_MAPPINGS
    ]


def test_apply_candidate_adds_existing_parent_when_confident(monkeypatch) -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import (
        parent_specificity_rumination_workflow as mod,
    )

    add_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        mod,
        "add_relationship",
        lambda source_id, predicate, target_id: add_calls.append(
            (source_id, predicate, target_id)
        )
        or {"success": True, "forward_modified": True},
    )
    monkeypatch.setattr(mod, "_record_parent_specificity_audit", lambda **_kwargs: None)

    request = WorkflowActionRequest(
        action_id="parent_specificity.apply_candidate",
        inputs={},
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "current_candidate_id": "#V#alice",
            "analysis_status": "actionable",
            "analysis_actionable": True,
            "analysis_result": {
                "decision": "add_existing_parent",
                "confidence": 0.97,
                "subject_kind": "instance",
                "recommended_parent_id": "#V#professor",
            },
            "analysis_records": [],
            "max_mutations_per_run": 4,
        },
    )
    result = mod._handle_apply_candidate(request)

    assert result.ok
    assert add_calls == [("#V#alice", "is_an_instance_of", "#V#professor")]
    assert result.outputs["applied_count"] == 1
    assert result.outputs["latest_parent_specificity_action"]["status"] == "applied"


def test_apply_candidate_creates_intervening_type(monkeypatch) -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable import (
        parent_specificity_rumination_workflow as mod,
    )

    monkeypatch.setattr(
        mod,
        "_ensure_intervening_type",
        lambda **_kwargs: {
            "success": True,
            "intervening_type_id": "#V#theoretical_graph_scientist",
            "created": True,
        },
    )
    add_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        mod,
        "add_relationship",
        lambda source_id, predicate, target_id: add_calls.append(
            (source_id, predicate, target_id)
        )
        or {"success": True},
    )
    monkeypatch.setattr(mod, "_record_parent_specificity_audit", lambda **_kwargs: None)

    request = WorkflowActionRequest(
        action_id="parent_specificity.apply_candidate",
        inputs={},
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "current_candidate_id": "#V#alice",
            "analysis_status": "actionable",
            "analysis_actionable": True,
            "analysis_result": {
                "decision": "create_intervening_type",
                "confidence": 0.96,
                "subject_kind": "instance",
                "new_type": {
                    "name": "Theoretical graph scientist",
                    "concept_id": "#V#theoretical_graph_scientist",
                    "parent_ids": ["#V#researcher"],
                },
            },
            "analysis_records": [],
            "max_mutations_per_run": 4,
        },
    )
    result = mod._handle_apply_candidate(request)

    assert result.ok
    assert add_calls == [
        ("#V#alice", "is_an_instance_of", "#V#theoretical_graph_scientist")
    ]
    assert result.outputs["created_intervening_type_count"] == 1
