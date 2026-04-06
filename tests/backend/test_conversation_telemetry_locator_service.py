from __future__ import annotations


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def _matches_query(self, doc, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches_query(doc, clause) for clause in value):
                    return False
                continue

            if isinstance(value, dict):
                if "$exists" in value:
                    exists = key in doc
                    if bool(value["$exists"]) != exists:
                        return False
                if "$eq" in value:
                    if doc.get(key) != value["$eq"]:
                        return False
                if "$in" in value:
                    if doc.get(key) not in value["$in"]:
                        return False
            else:
                if doc.get(key) != value:
                    return False
        return True

    def find_one(self, query, projection=None, **_kwargs):
        for doc in self._docs:
            if not self._matches_query(doc, query):
                continue
            if not isinstance(projection, dict):
                return doc
            projected = {}
            for key, spec in projection.items():
                if spec in (1, True):
                    projected[key] = doc.get(key)
                    continue
                projected[key] = doc.get(key)
            return projected
        return None


def test_build_conversation_locator_prefers_exact_namespace_and_preserves_org_scope(
    monkeypatch,
):
    from src.backend.services import chat_history_service
    from src.backend.services.conversation_telemetry_locator_service import (
        build_conversation_llm_telemetry_locator,
    )

    docs = [
        {
            "_id": "legacy",
            "user_id": "#V#u",
            "session_id": "s1",
            "session_name": "Legacy Session",
            "history": [],
        },
        {
            "_id": "exact",
            "user_id": "#V#u",
            "session_id": "s1",
            "namespace": "#V#u@org",
            "organisation_concept_id": "#V#org",
            "session_name": "Org Session",
            "history": [
                {
                    "role": "assistant",
                    "content": "Represented.",
                    "timestamp": "2026-04-06T02:16:03.692647Z",
                    "llm_debug_data": {
                        "request_id": "req-123",
                        "turn_id": "assistant-123",
                    },
                }
            ],
        },
    ]

    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: _FakeCollection(docs),
    )

    payload = build_conversation_llm_telemetry_locator(
        user_id="#V#u",
        session_id="s1",
        namespace="#V#u@org",
        organisation_concept_id="#V#org",
        include_legacy=True,
    )

    assert payload["session_name"] == "Org Session"
    assert payload["namespace_context"] == {
        "namespace": "#V#u@org",
        "user_id": "#V#u",
        "org_id": "#V#org",
    }
    assert payload["metadata"]["total_turns"] == 1
    assert payload["metadata"]["transcript_turn_count"] == 1
    assert payload["turns"][0]["request_id"] == "req-123"
    assert payload["turns"][0]["mcp_access"]["chat_history_get_debug_entry"][
        "arguments"
    ] == {
        "session_id": "s1",
        "history_index": 0,
        "namespace": "#V#u@org",
        "user_concept_id": "#V#u",
        "organisation_concept_id": "#V#org",
    }
    assert payload["mcp_access"]["conversation_telemetry_get_locator"]["arguments"] == {
        "session_id": "s1",
        "namespace": "#V#u@org",
        "user_concept_id": "#V#u",
        "organisation_concept_id": "#V#org",
    }
