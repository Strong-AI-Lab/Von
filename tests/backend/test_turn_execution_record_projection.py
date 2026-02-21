from unittest.mock import MagicMock, patch

from src.backend.services import chat_history_service


def _session_context() -> dict[str, str]:
    return {
        "org_id": "university_of_auckland_strong_ai_lab",
        "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
        "role_in_org": "member",
        "namespace": "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    }


def test_add_message_to_history_upserts_turn_execution_projection() -> None:
    mock_coll = MagicMock()

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=mock_coll,
        ),
        patch(
            "src.backend.services.chat_history_service.get_session_context",
            return_value=_session_context(),
        ),
        patch("src.backend.services.chat_history_service.get_rag_service", None),
        patch(
            "src.backend.services.chat_history_service.upsert_turn_execution_record_projection",
            return_value={"updated": True, "request_id": "req-turn-1"},
        ) as mock_upsert,
        patch(
            "src.backend.services.chat_history_service.build_turn_execution_record",
        ) as mock_build,
    ):
        chat_history_service.add_message_to_history(
            user_id="#V#michael_witbrock",
            session_id="session-123",
            message={"role": "assistant", "content": "Done."},
            llm_debug_data={
                "turn_execution_record": {
                    "request_id": "req-turn-1",
                    "completion_gate": {"decision": "failed"},
                }
            },
        )

    mock_upsert.assert_called_once()
    kwargs = mock_upsert.call_args.kwargs
    assert kwargs["record"]["request_id"] == "req-turn-1"
    assert kwargs["user_id"] == "#V#michael_witbrock"
    assert kwargs["session_id"] == "session-123"
    assert kwargs["namespace"] == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    assert kwargs["org_id"] == "#V#university_of_auckland_strong_ai_lab"
    mock_build.assert_not_called()


def test_add_message_to_history_synthesises_turn_execution_projection_without_record() -> None:
    mock_coll = MagicMock()

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=mock_coll,
        ),
        patch(
            "src.backend.services.chat_history_service.get_session_context",
            return_value=_session_context(),
        ),
        patch("src.backend.services.chat_history_service.get_rag_service", None),
        patch(
            "src.backend.services.chat_history_service.upsert_turn_execution_record_projection",
            return_value={"updated": True, "request_id": "req-turn-2"},
        ) as mock_upsert,
        patch(
            "src.backend.services.chat_history_service.build_turn_execution_record",
            return_value={
                "request_id": "req-turn-2",
                "workflow_selection": {
                    "selected_workflow_id": "#V#chat_assistant_workflow"
                },
                "completion_gate": {"decision": "partial"},
            },
        ) as mock_build,
    ):
        chat_history_service.add_message_to_history(
            user_id="#V#michael_witbrock",
            session_id="session-123",
            message={"role": "assistant", "content": "Done."},
            llm_debug_data={
                "request_id": "req-turn-2",
                "actor_concept_id": "#V#github_copilot_instance",
                "messages": [{"role": "user", "content": "Please update predicates"}],
            },
        )

    mock_build.assert_called_once()
    kwargs = mock_build.call_args.kwargs
    assert kwargs["request_id"] == "req-turn-2"
    assert kwargs["actor_concept_id"] == "#V#github_copilot_instance"
    assert kwargs["prompt_text"] == "Please update predicates"
    assert kwargs["response_text"] == "Done."

    mock_upsert.assert_called_once()
    upsert_kwargs = mock_upsert.call_args.kwargs
    assert upsert_kwargs["record"]["request_id"] == "req-turn-2"

    update_calls = [call for call in mock_coll.update_one.call_args_list]
    assert update_calls, "Expected add_message_to_history to write chat history entry"
    write_payload = update_calls[0].args[1]
    stored_message = write_payload["$push"]["history"]
    stored_debug = stored_message.get("llm_debug_data")
    assert isinstance(stored_debug, dict)
    assert isinstance(stored_debug.get("turn_execution_record"), dict)
    assert stored_debug["turn_execution_record"]["request_id"] == "req-turn-2"


def test_add_message_to_history_synthesis_infers_workflow_selection() -> None:
    mock_coll = MagicMock()

    captured: dict = {}

    def _capture_upsert(**kwargs):
        captured.update(kwargs.get("record") or {})
        return {"updated": True, "request_id": kwargs.get("record", {}).get("request_id")}

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=mock_coll,
        ),
        patch(
            "src.backend.services.chat_history_service.get_session_context",
            return_value=_session_context(),
        ),
        patch("src.backend.services.chat_history_service.get_rag_service", None),
        patch(
            "src.backend.services.chat_history_service.upsert_turn_execution_record_projection",
            side_effect=_capture_upsert,
        ) as mock_upsert,
    ):
        chat_history_service.add_message_to_history(
            user_id="#V#michael_witbrock",
            session_id="session-456",
            message={"role": "assistant", "content": "I inspected the concepts."},
            llm_debug_data={
                "request_id": "req-turn-3",
                "messages": [{"role": "user", "content": "Check these concepts"}],
                "tool_invocations": [
                    {
                        "name": "search_concepts",
                        "status": "success",
                        "arguments": {"query": "concept"},
                    }
                ],
            },
        )

    mock_upsert.assert_called_once()
    workflow_selection = captured.get("workflow_selection")
    assert isinstance(workflow_selection, dict)
    assert workflow_selection.get("selected_workflow_id") == "#V#tool_calling_workflow"
