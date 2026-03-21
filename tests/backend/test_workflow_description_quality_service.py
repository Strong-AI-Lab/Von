from __future__ import annotations

from src.backend.workflows.workflow_description_quality_service import (
    assess_workflow_description_quality,
    summarise_workflow_description_quality,
)


def test_assess_workflow_description_quality_marks_retrieval_ready_text() -> None:
    quality = assess_workflow_description_quality(
        description=(
            "Narrative generation workflow for conversation turns that need contextual "
            "framing or persona-consistent storytelling. Domain: chat orchestration. "
            "Input types: user prompt, conversation history. Output types: narrated "
            "response text. Prerequisite capabilities: prompt resolution, LLM generation. "
            "Cost class: lightweight. Maturity: high. Estimated success likelihood: high."
        ),
        description_source="text_relation:#V#hasDescription",
    )

    assert quality["quality_label"] == "retrieval_ready"
    assert quality["signals"]["domain"] is True
    assert quality["signals"]["input_types"] is True
    assert quality["signals"]["output_types"] is True
    assert "under_specified_for_routing" not in quality["issue_codes"]


def test_assess_workflow_description_quality_flags_minimal_fallback_text() -> None:
    quality = assess_workflow_description_quality(
        description="Background duplicate-entity detection workflow.",
        description_source="registration.purpose",
    )

    assert quality["quality_label"] in {"stub", "minimal"}
    assert "fallback_source" in quality["issue_codes"]
    assert "under_specified_for_routing" in quality["issue_codes"]


def test_summarise_workflow_description_quality_aggregates_counts() -> None:
    summary = summarise_workflow_description_quality(
        [
            {
                "workflow_id": "#V#rich_workflow",
                "description": (
                    "Tool-calling workflow for external APIs. Domain: integration. "
                    "Input types: user request. Output types: tool results. "
                    "Prerequisite capabilities: MCP access. Cost class: medium. "
                    "Maturity: high. Estimated success likelihood: high."
                ),
                "description_source": "text_relation:#V#hasDescription",
            },
            {
                "workflow_id": "#V#thin_workflow",
                "description": "Simple workflow.",
                "description_source": "registration.purpose",
            },
        ]
    )

    counts = summary["counts"]
    assert counts["total"] == 2
    assert counts["retrieval_ready"] == 1
    assert counts["under_specified"] == 1
    assert "#V#thin_workflow" in summary["under_specified_workflow_ids"]
