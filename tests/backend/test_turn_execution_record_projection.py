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


def test_add_message_to_history_skips_turn_execution_projection_without_record() -> None:
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
            return_value={"updated": False, "reason": "missing_request_id"},
        ) as mock_upsert,
    ):
        chat_history_service.add_message_to_history(
            user_id="#V#michael_witbrock",
            session_id="session-123",
            message={"role": "assistant", "content": "Done."},
            llm_debug_data={"request_id": "req-turn-2"},
        )

    mock_upsert.assert_not_called()
