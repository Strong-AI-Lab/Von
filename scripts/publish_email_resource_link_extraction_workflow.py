"""One-shot authoring script for #V#email_resource_link_extraction (JVNAUTOSCI-2118).

Phase 1 vertical slice: a generic two-step workflow that

    1. invokes the source-tool MCP action (gmail_get_message), and
    2. invokes the generic ``extract_signals_from_tool_result`` durable action,
       passing the source tool's concept id so the runtime resolves the
       authored signal-extraction hint from Vontology.

The workflow body is deliberately free of integration-specific Python: the
"emailness" of this slice lives entirely in the source tool concept and its
authored output_item_signal_extraction_hint text relation.

Run once, then verify via mcp_vontology_fetch_concept / get_text_relations.
"""

from __future__ import annotations

import json

from src.backend.workflows import (
    workflow_concept_authority_service as workflow_authority_service,
)
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)


WORKFLOW_ID = "#V#email_resource_link_extraction"
WORKFLOW_NAME = "Email resource link extraction"
WORKFLOW_DESCRIPTION = (
    "Fetches an email message via the source-tool MCP action, then runs the "
    "generic extract_signals_from_tool_result durable action to surface "
    "structured resource references (arXiv ids, DOIs, URLs, attachments, and "
    "an intent_context summary). All semantic behaviour comes from the source "
    "tool concept's authored output_item_signal_extraction_hint, so the same "
    "workflow shape generalises to other tools by swapping the source tool "
    "concept and authoring its hint."
)

SOURCE_TOOL_CONCEPT_ID = "#V#gmail_get_message_tool"
SOURCE_TOOL_ACTION_ID = "gmail_get_message"  # MCP tool name, resolved via fallback bridge


def _build_authoring_spec() -> dict:
    return {
        "workflow_id": WORKFLOW_ID,
        "workflow_name": WORKFLOW_NAME,
        "workflow_description": WORKFLOW_DESCRIPTION,
        "parent_type_id": "#V#durable_workflow",
        "initial_state_key": "fetch_payload",
        "steps": [
            {
                "state_id": "fetch_payload",
                "state_key": "fetch_payload",
                "action_id": SOURCE_TOOL_ACTION_ID,
                "execution_mode": "deterministic",
                "context_input_mappings": [
                    {
                        "tool_param": "message_id",
                        "context_key": "message_id",
                        "required": True,
                    },
                ],
                "writes_context_keys": ["tool_payload"],
                "tool_output_context_mappings": [
                    {
                        "tool_output_field": "result",
                        "context_key": "tool_payload",
                    },
                ],
                "next_state_key": "extract_signals",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "extract_signals",
                "state_key": "extract_signals",
                "action_id": "extract_signals_from_tool_result",
                "execution_mode": "deterministic",
                "static_input_bindings": [
                    {
                        "tool_param": "source_tool_concept",
                        "value": SOURCE_TOOL_CONCEPT_ID,
                    },
                ],
                "context_input_mappings": [
                    {
                        "tool_param": "tool_payload",
                        "context_key": "tool_payload",
                        "required": True,
                    },
                ],
                "writes_context_keys": ["signals"],
                "tool_output_context_mappings": [
                    {
                        "tool_output_field": "signals",
                        "context_key": "signals",
                    },
                ],
                "next_state_key": "done",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "done",
                "state_key": "done",
                "terminal": True,
            },
            {
                "state_id": "failed",
                "state_key": "failed",
                "terminal": True,
            },
        ],
    }


def main() -> None:
    spec = _build_authoring_spec()
    definition = build_workflow_definition_from_authoring_spec(spec)
    report = workflow_authority_service.publish_workflow_definition_from_definition(
        definition=definition,
        create_missing=True,
        purpose=WORKFLOW_DESCRIPTION,
    )

    errors_by_id = report.get("errors_by_workflow_id") or {}
    validation_failures = report.get("validation_failures_by_workflow_id") or {}
    if errors_by_id.get(WORKFLOW_ID) or validation_failures.get(WORKFLOW_ID):
        print("PUBLICATION FAILED")
        print(json.dumps(report, indent=2, default=str))
        raise SystemExit(1)

    # Mark the lifecycle as published so the workflow is routable.
    workflow_authority_service.upsert_workflow_publication_lifecycle(
        workflow_id=WORKFLOW_ID,
        phase="published",
        published=True,
        validation_passed=True,
        postconditions_verified=True,
    )

    print("PUBLICATION OK")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
