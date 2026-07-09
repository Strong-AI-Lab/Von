from __future__ import annotations

from scripts.repair_jvnautosci_2272_zhan_gmail_arxiv_ingestion_workflow import (
    ALLOW_PARTIAL_SUCCESS_POLICY,
    CHILD_EXTRACT_RESOURCES_STEP_ID,
    CHILD_INGEST_ARXIV_STEP_ID,
    CHILD_MARK_DONE_STEP_ID,
    PARENT_LIST_MESSAGES_STEP_ID,
    PARENT_PROCESS_MESSAGES_STEP_ID,
    build_email_message_launch_input_contract,
    build_zhan_launch_input_contract,
    rewrite_email_message_workflow_spec,
    rewrite_gmail_completion_hint_payload,
    rewrite_zhan_parent_workflow_spec,
)
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)
from src.backend.workflows.workflow_launch_input_contracts import (
    normalise_workflow_launch_input_contract,
    resolve_workflow_launch_inputs,
)


def _binding_value(step: dict, tool_param: str):
    for item in step.get("static_input_bindings") or []:
        if item.get("tool_param") == tool_param:
            return item.get("value")
    raise AssertionError(f"missing binding: {tool_param}")


def _mapping_for(step: dict, context_key: str) -> dict:
    for item in step.get("tool_output_context_mappings") or []:
        if item.get("context_key") == context_key:
            return item
    raise AssertionError(f"missing mapping: {context_key}")


def test_parent_rewrite_collects_partial_message_outcomes() -> None:
    spec = {
        "workflow_id": "#V#zhan_gmail_arxiv_ingestion_workflow",
        "initial_state_key": PARENT_LIST_MESSAGES_STEP_ID,
        "steps": [
            {
                "state_id": PARENT_LIST_MESSAGES_STEP_ID,
                "action_id": "workflow_mcp.invoke_tool",
                "static_input_bindings": [
                    {"tool_param": "tool_name", "value": "gmail_list_messages"}
                ],
                "next_state_key": PARENT_PROCESS_MESSAGES_STEP_ID,
            },
            {
                "state_id": PARENT_PROCESS_MESSAGES_STEP_ID,
                "action_id": "workflow_control.for_each",
                "static_input_bindings": [
                    {"tool_param": "success_policy", "value": "all_must_succeed"}
                ],
                "writes_context_keys": [
                    "message_iteration_results",
                    "message_success_count",
                    "message_error_count",
                ],
                "next_state_key": "done",
            },
            {"state_id": "done", "terminal": True},
        ],
    }

    rewritten, changes = rewrite_zhan_parent_workflow_spec(spec)
    process_step = rewritten["steps"][1]
    list_step = rewritten["steps"][0]

    assert changes["message_batch_allows_partial"] is True
    assert (
        _binding_value(process_step, "success_policy") == ALLOW_PARTIAL_SUCCESS_POLICY
    )
    assert "message_partial_success" in process_step["writes_context_keys"]
    assert "message_item_count" in process_step["writes_context_keys"]
    assert _mapping_for(process_step, "message_partial_success")[
        "tool_output_field"
    ] == ("for_each_partial_success")
    assert _mapping_for(process_step, "message_item_count")["tool_output_field"] == (
        "for_each_item_count"
    )
    assert list_step["metadata"]["retry_policy"]["max_attempts"] == 2

    definition = build_workflow_definition_from_authoring_spec(rewritten)
    action = definition.states[PARENT_PROCESS_MESSAGES_STEP_ID].actions[0]
    assert action.inputs["success_policy"] == ALLOW_PARTIAL_SUCCESS_POLICY


def test_child_rewrite_collects_arxiv_partial_outcomes_before_marker_step() -> None:
    spec = {
        "workflow_id": "#V#email_arxiv_ingestion_from_message_workflow",
        "initial_state_key": CHILD_EXTRACT_RESOURCES_STEP_ID,
        "steps": [
            {
                "state_id": CHILD_EXTRACT_RESOURCES_STEP_ID,
                "subworkflow_id": "#V#email_resource_link_extraction",
                "next_state_key": CHILD_INGEST_ARXIV_STEP_ID,
            },
            {
                "state_id": CHILD_INGEST_ARXIV_STEP_ID,
                "action_id": "workflow_control.for_each",
                "static_input_bindings": [
                    {"tool_param": "success_policy", "value": "all_must_succeed"}
                ],
                "writes_context_keys": [
                    "arxiv_iteration_results",
                    "arxiv_success_count",
                    "arxiv_error_count",
                ],
                "conditional_transitions": [
                    {
                        "to_state": "resolve_done_hint",
                        "reason": "all_arxiv_ingestions_succeeded",
                        "condition_spec": {
                            "kind": "context_compare",
                            "key": "arxiv_error_count",
                            "operator": "eq",
                            "value": 0,
                        },
                    }
                ],
            },
            {"state_id": "resolve_done_hint", "next_state_key": "mark_done"},
            {
                "state_id": CHILD_MARK_DONE_STEP_ID,
                "action_id": "workflow_mcp.invoke_tool",
                "static_input_bindings": [
                    {"tool_param": "tool_name", "value": "gmail_modify_labels"}
                ],
                "next_state_key": "done",
            },
            {"state_id": "done", "terminal": True},
        ],
    }

    rewritten, changes = rewrite_email_message_workflow_spec(spec)
    ingest_step = rewritten["steps"][1]
    extract_step = rewritten["steps"][0]
    mark_done_step = rewritten["steps"][3]

    assert changes["arxiv_batch_allows_partial"] is True
    assert _binding_value(ingest_step, "success_policy") == ALLOW_PARTIAL_SUCCESS_POLICY
    assert "arxiv_partial_success" in ingest_step["writes_context_keys"]
    assert "arxiv_item_count" in ingest_step["writes_context_keys"]
    assert _mapping_for(ingest_step, "arxiv_partial_success")["tool_output_field"] == (
        "for_each_partial_success"
    )
    assert _mapping_for(ingest_step, "arxiv_item_count")["tool_output_field"] == (
        "for_each_item_count"
    )
    assert (
        ingest_step["conditional_transitions"][0]["to_state"] == CHILD_MARK_DONE_STEP_ID
    )
    assert extract_step["metadata"]["retry_policy"]["max_attempts"] == 2
    assert mark_done_step["metadata"]["retry_policy"]["max_attempts"] == 2
    assert _binding_value(mark_done_step, "tool_name") == (
        "record_source_processing_marker"
    )
    marker_args = _binding_value(mark_done_step, "tool_arguments")
    assert marker_args["source_system"] == "gmail"
    assert marker_args["source_item_id"] == {"$context_key": "message_id"}
    assert marker_args["represented_outputs"] == {
        "$context_key": "arxiv_iteration_results"
    }
    assert "message_processing_marker" in mark_done_step["writes_context_keys"]

    definition = build_workflow_definition_from_authoring_spec(rewritten)
    action = definition.states[CHILD_INGEST_ARXIV_STEP_ID].actions[0]
    assert action.inputs["success_policy"] == ALLOW_PARTIAL_SUCCESS_POLICY


def test_completion_hint_uses_represented_marker_without_gmail_mutation() -> None:
    hint = {
        "schema_version": "tool_output_followup_hint.v1",
        "entries": [
            {
                "action_kind": "terminal_completion",
                "represented_effects": [
                    {
                        "effect_kind": "add_gmail_label",
                        "gmail_label_id": "Label_7",
                        "gmail_label_name": "vontology/ingested",
                    }
                ],
                "tool_arguments": {
                    "add_labels": ["vontology/ingested"],
                    "allow_mutation": True,
                },
                "upstream_filter": {
                    "query_fragment": "-label:vontology/ingested",
                },
            }
        ],
    }

    rewritten, changed = rewrite_gmail_completion_hint_payload(hint)
    entry = rewritten["entries"][0]

    assert changed is True
    assert entry["action"]["tool_name"] == "record_source_processing_marker"
    assert entry["tool_arguments"] == {
        "source_system": "gmail",
        "source_profile_context_key": "gmail_profile",
        "source_item_id_context_key": "message_id",
        "represented_outputs_context_key": "arxiv_iteration_results",
        "processing_status": "processed",
    }
    assert entry["represented_effects"] == [
        {
            "effect_kind": "record_source_processing_marker",
            "marker_type_concept_id": "#V#source_processing_marker",
            "evidence_predicate_concept_id": "#V#hasSourceProcessingEvidenceJson",
        }
    ]
    assert entry["upstream_filter"]["query_fragment"] == ""


def test_launch_contracts_are_valid_and_mapping_backed() -> None:
    for contract in (
        build_zhan_launch_input_contract(),
        build_email_message_launch_input_contract(),
    ):
        normalised, error = normalise_workflow_launch_input_contract(contract)
        assert error is None
        assert normalised is not None
        assert normalised["input_mappings"]


def test_zhan_launch_contract_uses_prior_arxiv_evidence_as_narrow_query() -> None:
    resolution = resolve_workflow_launch_inputs(
        workflow_id="#V#zhan_gmail_arxiv_ingestion_workflow",
        contract=build_zhan_launch_input_contract(),
        inputs={"arxiv_id": "2606.30544"},
        contract_source="repair_script_test",
    )

    assert resolution.resolved_inputs["base_gmail_query"] == "2606.30544"
    assert resolution.resolved_inputs["arxiv_id"] == "2606.30544"
    assert resolution.diagnostics["status"] == "resolved"


def test_serialise_authoring_spec_does_not_reauthor_loader_transition_metadata() -> (
    None
):
    definition = build_workflow_definition_from_authoring_spec(
        {
            "workflow_id": "#V#sample_transition_roundtrip_workflow",
            "initial_state_key": "start",
            "steps": [
                {
                    "state_id": "start",
                    "metadata": {
                        "transition_condition_specs": [
                            {
                                "to_state": "failed",
                                "reason": "on_failure",
                                "condition_spec": {
                                    "kind": "context_flag",
                                    "key": "last_action_failed",
                                    "expected": True,
                                },
                            },
                            {
                                "to_state": "done",
                                "reason": "next_step",
                                "condition_spec": {"kind": "always"},
                            },
                        ],
                    },
                    "on_failure_state_key": "failed",
                    "next_state_key": "done",
                },
                {"state_id": "failed", "terminal": True},
                {"state_id": "done", "terminal": True},
            ],
        }
    )

    spec = serialise_workflow_definition_to_authoring_spec(definition)
    start_row = spec["steps"][0]

    assert start_row["on_failure_state_key"] == "failed"
    assert start_row["next_state_key"] == "done"
    assert "transition_condition_specs" not in start_row.get("metadata", {})
