import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from unittest.mock import MagicMock, patch

from src.backend.services import chat_history_service


def test_add_message_to_history_uses_composite_namespace_for_rag_upsert():
    mock_coll = MagicMock()
    mock_rag = MagicMock()
    mock_rag.upsert_documents.return_value = (1, 0)

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=mock_coll,
        ),
        patch(
            "src.backend.services.chat_history_service.get_session_context",
            return_value={
                "org_id": "university_of_auckland_strong_ai_lab",
                "role_in_org": "member",
                "namespace": None,
            },
        ),
        patch(
            "src.backend.services.chat_history_service.get_rag_service",
            return_value=mock_rag,
        ),
    ):
        chat_history_service.add_message_to_history(
            user_id="#V#michael_witbrock",
            session_id="session-123",
            message={"role": "user", "content": "hello"},
        )

    # Should index under the derived composite namespace (not the hard-coded "chat_history")
    _, kwargs = mock_rag.upsert_documents.call_args
    assert (
        kwargs.get("namespace")
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )

    # Also persist the namespace on the chat_history document
    update_doc = mock_coll.update_one.call_args_list[0][0][1]
    assert (
        update_doc["$set"]["namespace"]
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )


def test_add_message_to_history_indexes_spoken_presenter_channel_to_rag():
    mock_coll = MagicMock()
    mock_rag = MagicMock()
    mock_rag.upsert_documents.return_value = (1, 0)

    with (
        patch(
            "src.backend.services.chat_history_service.get_chat_history_collection_service",
            return_value=mock_coll,
        ),
        patch(
            "src.backend.services.chat_history_service.get_session_context",
            return_value={
                "org_id": "university_of_auckland_strong_ai_lab",
                "role_in_org": "member",
                "namespace": "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
            },
        ),
        patch(
            "src.backend.services.chat_history_service.get_rag_service",
            return_value=mock_rag,
        ),
    ):
        chat_history_service.add_message_to_history(
            user_id="#V#michael_witbrock",
            session_id="session-123",
            message={"role": "assistant", "content": "Screen answer."},
            llm_debug_data={
                "presenter_channels": {
                    "screen": "Screen answer.",
                    "spoken": "Spoken answer.",
                    "format": "presenter_protocol_v1",
                }
            },
        )

    # First call indexes the screen content. Second call indexes spoken.
    assert mock_rag.upsert_documents.call_count == 2

    screen_call = mock_rag.upsert_documents.call_args_list[0]
    spoken_call = mock_rag.upsert_documents.call_args_list[1]

    screen_docs = screen_call.args[0]
    assert screen_docs[0]["text"] == "Screen answer."
    assert screen_docs[0]["metadata"].get("channel") is None

    spoken_docs = spoken_call.args[0]
    assert spoken_docs[0]["text"] == "Spoken answer."
    assert spoken_docs[0]["metadata"]["channel"] == "spoken"
