from __future__ import annotations

from src.backend.services.conversation_turn_memory_context_service import (
    merge_conversation_situation_turn_projection,
)
from src.backend.services.turn_execution_record_service import (
    build_conversation_situation_turn_projection,
)


def test_design_proposal_projects_only_answer_material_verified_concept_ids() -> None:
    projection = build_conversation_situation_turn_projection(
        request_id="turn-design",
        terminal_status="completed",
        response_text=(
            "Use #V#trip, #V#tripcomponent, #V#flight_trip_component and "
            "#V#has_trip_component; do not create anything yet."
        ),
        tool_invocations=[
            {
                "tool": "search_concepts",
                "status": "ok",
                "payload": {
                    "name": "search_concepts",
                    "arguments": {"query": "trip component"},
                },
                "evidence": {
                    "schema_version": "turn_evidence_envelope.v1",
                    "status": "ok",
                    "projected_payload": {
                        "results": [
                            {"concept_id": "#V#trip"},
                            {"concept_id": "#V#tripcomponent"},
                            {"concept_id": "#V#flight_trip_component"},
                            {"concept_id": "#V#has_trip_component"},
                            {"concept_id": "#V#irrelevant_search_result"},
                        ],
                    },
                },
            }
        ],
    )

    assert projection is not None
    assert projection["verified_concept_ids"] == [
        "#V#trip",
        "#V#tripcomponent",
        "#V#flight_trip_component",
        "#V#has_trip_component",
    ]
    assert "effects" not in projection
    assert "#V#irrelevant_search_result" not in str(projection)

    merged = merge_conversation_situation_turn_projection(
        current_situation=None,
        model_situation=None,
        projection=projection,
    )
    assert merged is not None
    assert "turn request id: turn-design" in merged
    assert "verified concept ids: #V#trip" in merged
    assert "This block grants no authority" in merged


def test_fetch_concept_evidence_envelope_projects_answer_material_ids_and_relations() -> None:
    original_id = "#V#trip_gmail_19febb3feda7b024"
    derived_id = "#V#american_airlines_confirmation_trip_gmail_derived"
    projection = build_conversation_situation_turn_projection(
        request_id="turn-duplicate-review",
        terminal_status="completed",
        response_text=f"Keep [{original_id}](/concept/{original_id}); {derived_id} is derived.",
        tool_invocations=[
            {
                "tool": "fetch_concept",
                "method": "fetch_concept",
                "status": "ok",
                "effective_arguments": {"concept_id": original_id},
                "payload": {
                    "name": "fetch_concept",
                    "arguments": {"concept_id": original_id},
                },
                "evidence": {
                    "schema_version": "turn_evidence_envelope.v1",
                    "status": "ok",
                    "result_target_ids": [original_id],
                    "projected_payload": {
                        "_llm_view": "tool_evidence_projection.v1",
                        "concept_id": original_id,
                        "relations": {
                            "relations": [
                                {
                                    "source_concept_id": original_id,
                                    "predicate_id": "#V#has_trip_component",
                                    "target_concept_id": "#V#leg_01",
                                }
                            ]
                        },
                    },
                },
            },
            {
                "tool": "fetch_concept",
                "method": "fetch_concept",
                "status": "ok",
                "effective_arguments": {"concept_id": derived_id},
                "payload": {
                    "name": "fetch_concept",
                    "arguments": {"concept_id": derived_id},
                },
                "evidence": {
                    "schema_version": "turn_evidence_envelope.v1",
                    "status": "ok",
                    "result_target_ids": [derived_id],
                    "projected_payload": {
                        "_llm_view": "tool_evidence_projection.v1",
                        "concept_id": derived_id,
                    },
                },
            },
        ],
    )

    assert projection is not None
    assert projection["verified_concept_ids"] == [original_id, derived_id]
    assert projection["verified_relationships"] == [
        {
            "source_id": original_id,
            "predicate_id": "#V#has_trip_component",
            "target_id": "#V#leg_01",
        }
    ]


def test_consent_turn_merges_model_sidecar_with_verified_effect_readback() -> None:
    trip_id = "#V#trip_gmail_19febb3feda7b024"
    leg_ids = [f"#V#flight_leg_{index}" for index in range(1, 4)]
    invocations = [
        {
            "tool": "gmail_get_message",
            "status": "ok",
            "effective_arguments": {
                "profile": "work",
                "message_id": "19febb3feda7b024",
            },
            "effective_payload": {
                "success": True,
                "profile": "work",
                "message_id": "19febb3feda7b024",
                "body": "private mail must never enter the situation",
            },
        },
        {
            "tool": "create_concepts",
            "status": "ok",
            "effect_id": "effect-create-trip",
            "effect_status": "succeeded",
            "changed": True,
            "effective_arguments": {
                "source_message_id": "19febb3feda7b024",
            },
            "effective_payload": {
                "success": True,
                "effect_status": "succeeded",
                "changed": True,
                "created_concept_ids": [trip_id],
            },
        },
        *[
            {
                "tool": "add_relationship",
                "status": "ok",
                "effect_id": f"effect-link-{index}",
                "effect_status": "succeeded",
                "changed": True,
                "effective_arguments": {
                    "source_id": trip_id,
                    "predicate": "#V#has_trip_component",
                    "target": leg_id,
                },
                "effective_payload": {
                    "success": True,
                    "effect_status": "succeeded",
                    "changed": True,
                },
            }
            for index, leg_id in enumerate(leg_ids, start=1)
        ],
        {
            "tool": "fetch_concept",
            "status": "ok",
            "effective_arguments": {"concept_id": trip_id},
            "effective_payload": {
                "success": True,
                "concept_id": trip_id,
                "relations": [
                    {
                        "source_id": trip_id,
                        "predicate": "#V#has_trip_component",
                        "target_id": leg_id,
                    }
                    for leg_id in leg_ids
                ],
            },
        },
    ]
    response_text = "Persisted " + ", ".join([trip_id, *leg_ids])
    projection = build_conversation_situation_turn_projection(
        request_id="turn-consent",
        terminal_status="completed",
        response_text=response_text,
        tool_invocations=invocations,
    )

    assert projection is not None
    assert projection["source_ids"] == [
        {
            "kind": "gmail_message",
            "id": "19febb3feda7b024",
            "profile": "work",
        }
    ]
    assert projection["verified_concept_ids"] == [trip_id]
    assert len(projection["verified_relationships"]) == 3
    assert projection["effects"][0] == {
        "tool": "create_concepts",
        "status": "succeeded",
        "effect_id": "effect-create-trip",
        "changed": True,
        "mode": "created",
    }

    prior = merge_conversation_situation_turn_projection(
        current_situation="The trip design is awaiting consent.",
        model_situation=None,
        projection={
            "request_id": "turn-design",
            "terminal_status": "completed",
            "provenance": "canonical_turn_tool_records",
            "verified_concept_ids": ["#V#trip"],
        },
    )
    merged = merge_conversation_situation_turn_projection(
        current_situation=prior,
        model_situation="The neutral trip now exists; purpose remains open.",
        projection=projection,
    )
    assert merged is not None
    assert merged.startswith("The neutral trip now exists; purpose remains open.")
    assert merged.count("turn request id: turn-design") == 1
    assert merged.count("turn request id: turn-consent") == 1
    assert "private mail must never enter the situation" not in merged


def test_mixed_partial_turn_preserves_stable_receipt_and_workflow_status_only() -> None:
    projection = build_conversation_situation_turn_projection(
        request_id="turn-partial",
        terminal_status="effect_partially_completed",
        response_text="The durable trip operation is only partly complete.",
        tool_invocations=[
            {
                "tool": "workflow_execute",
                "status": "partial",
                "effect_id": "effect-trip-workflow",
                "effect_status": "partial",
                "changed": True,
                "workflow_id": "#V#trip_workflow",
                "instance_id": "workflow-instance-123",
                "effective_payload": {
                    "success": False,
                    "effect_status": "partial",
                    "changed": True,
                    "workflow_id": "#V#trip_workflow",
                    "instance_id": "workflow-instance-123",
                    "status": "running",
                },
            }
        ],
    )

    assert projection is not None
    assert projection["terminal_status"] == "effect_partially_completed"
    assert projection["effects"] == [
        {
            "tool": "workflow_execute",
            "status": "partial",
            "effect_id": "effect-trip-workflow",
            "changed": True,
            "mode": "partial",
        }
    ]
    assert projection["workflow_instances"] == [
        {
            "instance_id": "workflow-instance-123",
            "status": "running",
            "workflow_id": "#V#trip_workflow",
            "mode": "partial",
        }
    ]
    assert "verified_concept_ids" not in projection


def test_simple_chat_without_tool_records_does_not_create_runtime_situation() -> None:
    projection = build_conversation_situation_turn_projection(
        request_id="turn-simple-chat",
        terminal_status="completed",
        response_text="Hello there.",
        tool_invocations=[],
    )
    assert projection is None
    assert (
        merge_conversation_situation_turn_projection(
            current_situation=None,
            model_situation=None,
            projection=projection,
        )
        is None
    )
