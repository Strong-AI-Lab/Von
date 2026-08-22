from __future__ import annotations

from typing import Any, Mapping

from src.backend.server.routes.generate_route_support import (
    _persist_generate_turn_messages,
)
from src.backend.services.blob_store import BlobRef


class _FakeBlobStore:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> BlobRef:
        self.writes.append(
            {
                "key": key,
                "data": data,
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
        )
        return BlobRef(
            backend="local",
            key=key,
            uri=f"local://{key}",
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata or {}),
        )

    def get_bytes(self, key: str) -> bytes:
        for write in self.writes:
            if write["key"] == key:
                return write["data"]
        raise KeyError(key)

    def exists(self, key: str) -> bool:
        return any(write["key"] == key for write in self.writes)

    def delete(self, key: str) -> None:
        self.writes = [write for write in self.writes if write["key"] != key]

    def list(self, prefix: str = "") -> list[str]:
        return [
            write["key"] for write in self.writes if write["key"].startswith(prefix)
        ]


def test_persist_generate_turn_messages_offloads_tool_message_content(
    monkeypatch,
) -> None:
    store = _FakeBlobStore()
    captured_messages: list[dict[str, Any]] = []

    monkeypatch.setenv("VON_BLOB_SPILLWAY_ENABLED", "0")
    monkeypatch.setenv("VON_DEBUG_TOOL_MESSAGE_BLOB_THRESHOLD_BYTES", "512")
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: store,
    )

    def _capture_message(**kwargs):
        captured_messages.append(dict(kwargs["message"]))

    updated_context = _persist_generate_turn_messages(
        history_user_id="#V#michael_witbrock",
        user_message_persisted_early=True,
        prompt_text="prompt",
        user_concept_id="#V#michael_witbrock",
        session_id="session-tool-offload",
        tool_messages=[
            {
                "role": "tool",
                "tool": "gmail_get_message",
                "content": "x" * 2000,
            }
        ],
        response_text="answer",
        llm_debug_info={"request_id": "req-tool-offload"},
        user_namespace="#V#michael@org",
        org_concept_id="#V#org",
        role_in_org=None,
        current_context=[],
        truncate_large_tool_results_fn=lambda messages, **_kwargs: messages,
        add_chat_history_message_fn=_capture_message,
        limit_context_size_fn=lambda messages, **_kwargs: list(messages),
    )

    assert store.writes
    assert len(captured_messages) == 2
    stored_tool_message = captured_messages[0]
    assert stored_tool_message["role"] == "tool"
    assert (
        stored_tool_message["content"]["schema_version"] == "debug_payload_blob_ref.v1"
    )
    assert stored_tool_message["content"]["field_path"] == "content"
    assert captured_messages[1] == {"role": "assistant", "content": "answer"}
    assert updated_context == []


def test_persist_generate_assistant_opening_does_not_write_a_synthetic_user_message():
    captured_messages: list[dict[str, Any]] = []

    _persist_generate_turn_messages(
        history_user_id="#V#michael_witbrock",
        user_message_persisted_early=False,
        prompt_text="",
        user_concept_id="#V#michael_witbrock",
        session_id="assistant-opening",
        tool_messages=[],
        response_text="I can help examine this concept.",
        llm_debug_info={},
        user_namespace="#V#michael@org",
        org_concept_id="#V#org",
        role_in_org=None,
        current_context=[],
        truncate_large_tool_results_fn=lambda messages, **_kwargs: messages,
        add_chat_history_message_fn=lambda **kwargs: captured_messages.append(
            dict(kwargs["message"])
        ),
        limit_context_size_fn=lambda messages, **_kwargs: list(messages),
        persist_user_message=False,
        assistant_message_metadata={
            "turn_kind": "assistant_opening",
            "initiation_id": "opening-1",
        },
    )

    assert captured_messages == [
        {
            "role": "assistant",
            "content": "I can help examine this concept.",
            "turn_kind": "assistant_opening",
            "initiation_id": "opening-1",
        }
    ]
