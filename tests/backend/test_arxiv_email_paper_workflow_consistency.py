from __future__ import annotations

from scripts.repair_arxiv_email_paper_workflow_consistency import (
    READ_FILE_COPY_TOOL_CONCEPT_ID,
    load_read_file_copy_signal_hint_seed,
    rewrite_arxiv_resource_ingestion_spec,
    rewrite_arxiv_wrapper_file_copy_output_spec,
)
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)


def _sample_arxiv_resource_authoring_spec() -> dict:
    return {
        "workflow_id": "#V#arxiv_resource_ingestion_from_email_reference_workflow",
        "initial_state_key": (
            "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_workflow_"
            "represent_arxiv_paper"
        ),
        "steps": [
            {
                "state_id": (
                    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
                    "workflow_represent_arxiv_paper"
                ),
                "subworkflow_id": "#V#arxiv_paper_representation_workflow",
                "tool_output_context_mappings": [
                    {
                        "context_key": "paper_concept_id",
                        "tool_output_field": "result.paper_concept_id",
                        "mapping_concept_id": (
                            "#V#workflow_mapping_tool_field_sample_paper_concept"
                        ),
                    }
                ],
                "writes_context_keys": ["paper_concept_id"],
                "next_state_key": (
                    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
                    "workflow_read_paper"
                ),
            },
            {
                "state_id": (
                    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
                    "workflow_read_paper"
                ),
                "action_id": "workflow_mcp.invoke_tool",
                "static_input_bindings": [
                    {"tool_param": "tool_name", "value": "read_paper"},
                    {
                        "tool_param": "tool_arguments",
                        "value": {"arxiv_id": {"$context_key": "arxiv_id"}},
                    },
                ],
                "tool_output_context_mappings": [
                    {
                        "context_key": "paper_payload",
                        "tool_output_field": "result",
                        "mapping_concept_id": (
                            "#V#workflow_mapping_tool_field_sample_paper_payload"
                        ),
                    }
                ],
                "writes_context_keys": ["paper_payload"],
                "next_state_key": (
                    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
                    "workflow_extract_paper_signals"
                ),
            },
            {
                "state_id": (
                    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
                    "workflow_extract_paper_signals"
                ),
                "action_id": "extract_signals_from_tool_result",
                "static_input_bindings": [
                    {
                        "tool_param": "source_tool_concept",
                        "value": "#V#read_paper_tool",
                    }
                ],
                "context_input_mappings": [
                    {
                        "tool_param": "tool_payload",
                        "context_key": "paper_payload",
                        "required": True,
                    }
                ],
                "next_state_key": (
                    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
                    "workflow_done"
                ),
            },
            {
                "state_id": (
                    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
                    "workflow_done"
                ),
                "terminal": True,
            },
        ],
    }


def _binding_value(step: dict, tool_param: str):
    for item in step.get("static_input_bindings") or []:
        if item.get("tool_param") == tool_param:
            return item.get("value")
    raise AssertionError(f"missing binding: {tool_param}")


def test_rewrite_arxiv_resource_workflow_reads_wrapper_file_copy() -> None:
    spec, found = rewrite_arxiv_resource_ingestion_spec(
        _sample_arxiv_resource_authoring_spec()
    )

    assert found == {
        "representation_step": True,
        "read_step": True,
        "extract_step": True,
    }
    represent_step, read_step, extract_step, _done = spec["steps"]

    assert "file_copy_concept_id" in represent_step["writes_context_keys"]
    assert any(
        item["context_key"] == "file_copy_concept_id"
        and item["tool_output_field"] == "result.file_copy_concept_id"
        for item in represent_step["tool_output_context_mappings"]
    )

    assert _binding_value(read_step, "tool_name") == "read_file_copy"
    assert _binding_value(read_step, "tool_arguments") == {
        "concept_id": {"$context_key": "file_copy_concept_id"},
        "as_text": True,
        "allow_large": True,
        "max_bytes": 20_000_000,
    }
    assert _binding_value(extract_step, "source_tool_concept") == (
        READ_FILE_COPY_TOOL_CONCEPT_ID
    )

    definition = build_workflow_definition_from_authoring_spec(spec)
    read_state = definition.states[
        (
            "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_"
            "workflow_read_paper"
        )
    ]
    action = read_state.actions[0]
    assert action.action_id == "workflow_mcp.invoke_tool"
    assert action.inputs["tool_name"] == "read_file_copy"
    assert action.inputs["tool_arguments"]["concept_id"] == {
        "$context_key": "file_copy_concept_id"
    }


def test_read_file_copy_signal_hint_seed_is_scholarly_and_file_copy_based() -> None:
    hint = load_read_file_copy_signal_hint_seed()

    assert "read_file_copy tool output" in hint
    assert "payload.text" in hint
    assert "#V#claim" in hint
    assert "read_paper tool output" not in hint


def test_rewrite_arxiv_wrapper_declares_file_copy_output_contract() -> None:
    spec = {
        "workflow_id": "#V#arxiv_paper_representation_workflow",
        "initial_state_key": "download_or_finalise",
        "steps": [
            {
                "state_id": "decide_acquisition_mode",
                "action_id": "arxiv.decide_acquisition_mode",
                "tool_output_context_mappings": [
                    {
                        "context_key": "file_copy_concept_id",
                        "tool_output_field": "file_copy_concept_id",
                        "mapping_concept_id": (
                            "#V#workflow_mapping_tool_field_decide_file_copy"
                        ),
                    }
                ],
                "writes_context_keys": ["file_copy_concept_id"],
                "next_state_key": "download_or_finalise",
            },
            {
                "state_id": "download_or_finalise",
                "action_id": "download_paper",
                "tool_output_context_mappings": [
                    {
                        "context_key": "file_copy_concept_id",
                        "tool_output_field": "result.computer_file_copy_concept_id",
                        "mapping_concept_id": (
                            "#V#workflow_mapping_tool_field_sample_file_copy"
                        ),
                    }
                ],
                "next_state_key": "done",
            },
            {"state_id": "done", "terminal": True},
        ],
    }

    rewritten, updated_count = rewrite_arxiv_wrapper_file_copy_output_spec(spec)

    assert updated_count == 2
    assert "writes_context_keys" not in rewritten["steps"][0]
    assert rewritten["steps"][1]["writes_context_keys"] == ["file_copy_concept_id"]

    definition = build_workflow_definition_from_authoring_spec(rewritten)
    assert (
        "file_copy_concept_id"
        not in definition.states["decide_acquisition_mode"].metadata.get(
            "writes_context_keys", []
        )
    )
    assert "file_copy_concept_id" in (
        definition.states["download_or_finalise"].metadata["writes_context_keys"]
    )
