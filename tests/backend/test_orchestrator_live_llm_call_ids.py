from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


def test_live_llm_exchange_and_attempt_ids_are_stable() -> None:
    request_telemetry = {
        "prompt": {
            "text": "Select the workflow for this turn.",
            "char_count": 34,
        },
        "context_message_count": 2,
        "tool_count": 0,
    }

    exchange_id = InternalMCPChatOrchestrator._build_live_llm_exchange_id(
        stage="workflow_dispatch",
        workflow_stage_id="selected_workflow_execution",
        prepared_at_utc="2026-06-08T20:09:12.955000Z",
        request_telemetry=request_telemetry,
    )

    assert exchange_id == InternalMCPChatOrchestrator._build_live_llm_exchange_id(
        stage="workflow_dispatch",
        workflow_stage_id="selected_workflow_execution",
        prepared_at_utc="2026-06-08T20:09:12.955000Z",
        request_telemetry=request_telemetry,
    )
    assert exchange_id.startswith("llm-")
    assert InternalMCPChatOrchestrator._build_live_llm_call_id(
        exchange_id, 1
    ) == f"{exchange_id}:attempt:1"
    assert InternalMCPChatOrchestrator._build_live_llm_call_id(
        exchange_id, 2
    ) == f"{exchange_id}:attempt:2"


def test_live_llm_progress_payload_carries_exchange_identity() -> None:
    payload = InternalMCPChatOrchestrator._build_live_llm_progress_payload(
        request_telemetry={
            "prompt": {
                "text": "Summarise the evidence.",
                "char_count": 23,
            }
        },
        request_state="completed",
        llm_exchange_id="llm-abc123",
        call_id="llm-abc123:attempt:1",
        prepared_at_utc="2026-06-08T20:09:12.955000Z",
        sent_at_utc="2026-06-08T20:09:13.100000Z",
        first_output_at_utc="2026-06-08T20:09:16.557000Z",
        response="Done.",
    )

    assert payload["llm_exchange_id"] == "llm-abc123"
    assert payload["call_id"] == "llm-abc123:attempt:1"
    assert payload["llm_request_state"] == "completed"
    assert payload["llm_request"]["prompt"]["text"] == "Summarise the evidence."
    assert payload["llm_response_preview"]["text"] == "Done."
