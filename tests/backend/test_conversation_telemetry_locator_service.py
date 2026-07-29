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
    from src.backend.services.conversation_scope_binding_service import (
        verify_conversation_scope_binding,
        verify_history_location_binding,
    )
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
            "conversation_situation": {
                "text": "Inspectable carrier text must not enter the locator.",
                "revision": 7,
                "source": "adaptive_turn",
                "updated_by": "#V#u",
                "updated_at": "2026-07-29T10:00:00Z",
            },
            "conversation_observations": [
                {
                    "observation_id": "effect-1",
                    "kind": "late_terminal_effect",
                }
            ],
            "conversation_observation_total": 4,
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
    assert payload["conversation_situation_state"] == {
        "available": True,
        "revision": 7,
        "updated_at": "2026-07-29T10:00:00+00:00",
    }
    assert payload["conversation_observation_state"] == {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": 1,
        "total_count": 4,
        "omitted_count": 3,
        "retention_limit": 12,
    }
    assert "conversation_situation" not in payload
    assert "conversation_observations" not in payload
    debug_args = payload["turns"][0]["mcp_access"]["chat_history_get_debug_entry"][
        "arguments"
    ]
    assert debug_args["namespace"] == "#V#u@org"
    assert debug_args["user_concept_id"] == "#V#u"
    assert debug_args["organisation_concept_id"] == "#V#org"
    verified_history_ref = verify_history_location_binding(
        debug_args["history_location_ref"]
    )
    assert verified_history_ref["success"] is True
    assert verified_history_ref["chat_session_id"] == "s1"
    assert verified_history_ref["history_index"] == 0

    locator_args = payload["mcp_access"]["conversation_telemetry_get_locator"][
        "arguments"
    ]
    assert locator_args["namespace"] == "#V#u@org"
    assert locator_args["user_concept_id"] == "#V#u"
    assert locator_args["organisation_concept_id"] == "#V#org"
    verified_conversation_ref = verify_conversation_scope_binding(
        locator_args["conversation_ref"]
    )
    assert verified_conversation_ref["success"] is True
    assert verified_conversation_ref["chat_session_id"] == "s1"
    segments_descriptor = payload["mcp_access"]["chat_history_get_segments"]
    assert "conversation carrier" in segments_descriptor["purpose"]


def test_build_conversation_locator_keeps_carrier_freshness_when_history_is_empty(
    monkeypatch,
):
    from src.backend.services import chat_history_service
    from src.backend.services.conversation_telemetry_locator_service import (
        build_conversation_llm_telemetry_locator,
    )

    calls = []
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_telemetry_locator_projection",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or {
                "session_id": "empty-session",
                "namespace": "#V#u@org",
                "organisation_concept_id": "#V#org",
                "history": [],
                "conversation_situation_state": {
                    "available": True,
                    "revision": 2,
                    "updated_at": "2026-07-29T11:00:00+00:00",
                },
                "conversation_observation_state": {
                    "schema_version": "conversation_observation_state.v1",
                    "retained_count": 0,
                    "total_count": 1,
                    "omitted_count": 1,
                    "retention_limit": 12,
                },
            }
        ),
    )

    payload = build_conversation_llm_telemetry_locator(
        user_id="#V#u",
        session_id="empty-session",
        namespace="#V#u@org",
        organisation_concept_id="#V#org",
    )

    assert len(calls) == 1
    assert payload["turns"] == []
    assert payload["metadata"]["transcript_turn_count"] == 0
    assert payload["conversation_situation_state"]["revision"] == 2
    assert payload["conversation_observation_state"]["total_count"] == 1
