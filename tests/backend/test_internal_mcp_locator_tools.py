from __future__ import annotations

from types import SimpleNamespace


def _emitted_von_conversation_ref(
    *,
    session_id: str = "ff9be41d-28f8-4864-ab0b-8c201e3152d0",
    user_concept_id: str = "#V#michael_witbrock",
    namespace: str = "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    organisation_concept_id: str = "university_of_auckland_strong_ai_lab",
) -> dict:
    return {
        "kind": "von_conversation_ref",
        "conversation_ref": {
            "session_id": session_id,
            "user_concept_id": user_concept_id,
            "namespace": namespace,
            "organisation_concept_id": organisation_concept_id,
            "include_legacy": False,
        },
        "chat_history_lookup": {
            "user_id": user_concept_id,
            "session_id": session_id,
            "namespace": namespace,
            "include_legacy": False,
        },
        "display": {
            "session_name": "gem 6",
            "current_user_concept_id": user_concept_id,
        },
    }


def test_chat_history_get_segments_returns_provenanced_payload(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        cat,
        "_resolve_chat_history_read_target",
        lambda _kwargs: {
            "success": True,
            "session_id": "chat-1",
            "chat_session_id": "chat-1",
            "read_user_id": "#V#user",
            "requested_user_id": "#V#user",
            "read_namespace": "#V#user@org",
            "access_mode": "owner",
            "identifier_binding": {"mode": "raw_parameters"},
        },
    )

    situation = {
        "text": "The shared situation is inspectable.",
        "revision": 3,
        "source": "adaptive_turn",
        "updated_by": "#V#user",
        "updated_at": "2026-07-29T10:00:00+00:00",
    }
    observations = [
        {
            "schema_version": "conversation_observation.v1",
            "observation_id": "effect-1",
            "kind": "late_terminal_effect",
        }
    ]
    observation_state = {
        "schema_version": "conversation_observation_state.v1",
        "retained_count": 1,
        "total_count": 2,
        "omitted_count": 1,
        "retention_limit": 12,
    }

    def get_segments(*_args, **kwargs):
        assert kwargs["include_conversation_state"] is True
        return (
            [
                {
                    "segment_index": 0,
                    "history": [{"role": "assistant", "content": "Done"}],
                }
            ],
            {
                "history_truncated": False,
                "conversation_situation": situation,
                "conversation_observations": observations,
                "conversation_observation_state": observation_state,
            },
        )

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        get_segments,
    )

    result = cat._chat_history_get_segments(
        session_id="chat-1", namespace="#V#user@org"
    )

    assert result["success"] is True
    assert result["session_id"] == "chat-1"
    assert result["chat_session_id"] == "chat-1"
    assert result["segment_count"] == 1
    assert result["history_truncated"] is False
    assert result["conversation_situation"] == situation
    assert result["conversation_observations"] == observations
    assert result["conversation_observation_state"] == observation_state
    assert result["identifier_binding"]["mode"] == "raw_parameters"
    assert result["provenance"]["item_kind"] == "chat_history_segments"


def test_chat_history_get_debug_entry_returns_history_location(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        cat,
        "_resolve_chat_history_read_target",
        lambda _kwargs: {
            "success": True,
            "session_id": "chat-1",
            "chat_session_id": "chat-1",
            "read_user_id": "#V#user",
            "requested_user_id": "#V#user",
            "read_namespace": "#V#user@org",
            "access_mode": "owner",
            "identifier_binding": {"mode": "raw_parameters"},
        },
    )

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_debug_entry",
        lambda **kwargs: {"request_id": "req-debug-1", "model": "gpt-test"},
    )

    result = cat._chat_history_get_debug_entry(
        session_id="chat-1",
        history_index=4,
        namespace="#V#user@org",
    )

    assert result["success"] is True
    assert result["history_location"] == {
        "session_id": "chat-1",
        "history_index": 4,
    }
    assert result["chat_session_id"] == "chat-1"
    assert result["llm_debug_data"]["request_id"] == "req-debug-1"
    assert result["identifier_binding"]["mode"] == "raw_parameters"
    assert result["provenance"]["item_kind"] == "chat_history_debug_entry"


def test_conversation_telemetry_get_locator_wraps_builder(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        cat,
        "_resolve_chat_history_read_target",
        lambda _kwargs: {
            "success": True,
            "session_id": "chat-1",
            "chat_session_id": "chat-1",
            "read_user_id": "#V#user",
            "requested_user_id": "#V#user",
            "read_namespace": "#V#user@org",
            "access_mode": "owner",
            "requested_namespace": "#V#user@org",
            "identifier_binding": {"mode": "raw_parameters"},
        },
    )

    monkeypatch.setattr(
        "src.backend.services.conversation_telemetry_locator_service.build_conversation_llm_telemetry_locator",
        lambda **kwargs: {
            "schema_version": "conversation_llm_telemetry_locator.v1",
            "generated_at_utc": "2026-04-05T03:18:49Z",
            "session_id": "chat-1",
            "session_name": "Locator Test",
            "namespace_context": {
                "namespace": "#V#user@org",
                "user_id": "#V#user",
                "org_id": "#V#org",
            },
            "metadata": {"total_turns": 1},
            "mcp_access": {
                "conversation_telemetry_get_locator": {
                    "tool_name": "conversation_telemetry_get_locator",
                    "arguments": {"session_id": "chat-1", "namespace": "#V#user@org"},
                }
            },
            "turns": [],
        },
    )

    result = cat._conversation_telemetry_get_locator(
        session_id="chat-1",
        namespace="#V#user@org",
    )

    assert result["session_id"] == "chat-1"
    assert result["history_owner_user_id"] == "#V#user"
    assert result["requested_user_id"] == "#V#user"
    assert result["access_mode"] == "owner"
    assert result["identifier_binding"]["mode"] == "raw_parameters"
    assert result["provenance"]["item_kind"] == "conversation_telemetry_locator"


def test_chat_history_get_segments_accepts_bound_conversation_ref(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat
    from src.backend.services.conversation_scope_binding_service import (
        build_conversation_scope_binding,
    )

    conversation_ref = build_conversation_scope_binding(
        chat_session_id="chat-bound-1",
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#owner",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#owner" and session_id == "chat-bound-1"
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        lambda *args, **kwargs: ([{"segment_index": 0, "history": []}], {}),
    )

    result = cat._chat_history_get_segments(
        conversation_ref=conversation_ref,
        namespace="#V#owner@org",
        user_concept_id="#V#owner",
        organisation_concept_id="#V#org",
    )

    assert result["success"] is True
    assert result["identifier_binding"]["mode"] == "server_bound_reference"
    assert result["identifier_binding"]["validation_status"] == "verified"


def test_chat_history_get_segments_accepts_emitted_von_conversation_ref_wrapper(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    conversation_ref = _emitted_von_conversation_ref()
    namespace = "#V#michael_witbrock@university_of_auckland_strong_ai_lab"

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#michael_witbrock",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#michael_witbrock"
            and session_id == "ff9be41d-28f8-4864-ab0b-8c201e3152d0"
            and namespace == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        ),
    )

    def get_segments(*args, **kwargs):
        assert kwargs["namespace"] == namespace
        assert kwargs["include_legacy"] is False
        return ([{"segment_index": 0, "history": []}], {})

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        get_segments,
    )

    result = cat._chat_history_get_segments(conversation_ref=conversation_ref)

    assert result["success"] is True
    assert result["session_id"] == "ff9be41d-28f8-4864-ab0b-8c201e3152d0"
    assert result["history_owner_user_id"] == "#V#michael_witbrock"
    assert result["requested_user_id"] == "#V#michael_witbrock"
    assert result["namespace"] == namespace
    assert result["identifier_binding"]["mode"] == "public_conversation_ref"
    assert result["identifier_binding"]["validation_status"] == "normalised"
    assert result["identifier_binding"]["signature_verified"] is False
    assert result["identifier_binding"]["input_shape"] == (
        "emitted_von_conversation_ref_wrapper"
    )


def test_chat_history_get_segments_accepts_flattened_von_conversation_ref(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    conversation_ref = {
        "kind": "von_conversation_ref",
        "session_id": "chat-flat-1",
        "user_concept_id": "#V#owner",
        "namespace": "#V#owner@org",
        "organisation_concept_id": "#V#org",
        "include_legacy": False,
    }

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#owner",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#owner"
            and session_id == "chat-flat-1"
            and namespace == "#V#owner@org"
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        lambda *args, **kwargs: ([{"segment_index": 0, "history": []}], {}),
    )

    result = cat._chat_history_get_segments(conversation_ref=conversation_ref)

    assert result["success"] is True
    assert result["session_id"] == "chat-flat-1"
    assert result["identifier_binding"]["mode"] == "public_conversation_ref"
    assert result["identifier_binding"]["input_shape"] == (
        "flattened_von_conversation_ref"
    )


def test_chat_history_get_segments_rejects_mismatched_emitted_ref_fields() -> None:
    from src.backend.integrations.internal_mcp import catalogue as cat

    cases = [
        ({"conversation_session_id": "different-session"}, "session_id"),
        ({"user_concept_id": "#V#different_user"}, "user_concept_id"),
        ({"namespace": "#V#different_user@different_org"}, "namespace"),
        ({"organisation_concept_id": "#V#different_org"}, "organisation_concept_id"),
    ]

    for explicit_payload, expected_field in cases:
        result = cat._chat_history_get_segments(
            conversation_ref=_emitted_von_conversation_ref(),
            **explicit_payload,
        )

        assert result["success"] is False
        assert result["error_code"] == "INVALID_CONTEXT_BINDING"
        assert result["error_details"]["identifier_binding"]["mode"] == (
            "public_conversation_ref"
        )
        assert result["error_details"]["identifier_binding"]["validation_status"] == (
            "field_mismatch"
        )
        assert result["error_details"]["field_conflicts"][0]["field"] == (
            expected_field
        )


def test_chat_history_get_segments_accepts_conversation_session_id(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#owner",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#owner" and session_id == "chat-session-alias"
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_segments",
        lambda *args, **kwargs: ([{"segment_index": 0, "history": []}], {}),
    )

    result = cat._chat_history_get_segments(
        conversation_session_id="chat-session-alias",
        namespace="#V#owner@org",
        user_concept_id="#V#owner",
        organisation_concept_id="#V#org",
    )

    assert result["success"] is True
    assert result["session_id"] == "chat-session-alias"
    assert result["identifier_binding"]["mode"] == "raw_parameters"
    assert (
        result["identifier_binding"]["chat_session_id_source"]
        == "payload.conversation_session_id"
    )


def test_chat_history_get_segments_rejects_mismatched_session_id_and_bound_ref(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat
    from src.backend.services.conversation_scope_binding_service import (
        build_conversation_scope_binding,
    )

    conversation_ref = build_conversation_scope_binding(
        chat_session_id="chat-bound-2",
        history_owner_user_id="#V#owner",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )

    result = cat._chat_history_get_segments(
        conversation_ref=conversation_ref,
        session_id="req-wrong-2",
        namespace="#V#owner@org",
        user_concept_id="#V#owner",
        organisation_concept_id="#V#org",
    )

    assert result["success"] is False
    assert result["error_code"] == "INVALID_CONTEXT_BINDING"
    assert result["error_details"]["identifier_binding"]["validation_status"] == (
        "verified"
    )


def test_chat_history_get_debug_entry_accepts_history_location_ref(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat
    from src.backend.services.conversation_scope_binding_service import (
        build_history_location_binding,
    )

    history_location_ref = build_history_location_binding(
        chat_session_id="chat-debug-1",
        history_index=3,
        history_owner_user_id="#V#owner",
        read_namespace="#V#owner@org",
        organisation_concept_id="#V#org",
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda user_id=None, org_id=None, namespace=None: (user_id, org_id),
    )
    monkeypatch.setattr(
        "src.backend.services.shared_conversation_service.resolve_conversation_owner",
        lambda session_id: "#V#owner",
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda user_id, session_id, namespace=None, include_legacy=True: (
            user_id == "#V#owner" and session_id == "chat-debug-1"
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_chat_history_debug_entry",
        lambda **kwargs: {
            "request_id": "req-debug-bound-1",
            "history_index": kwargs["history_index"],
        },
    )

    result = cat._chat_history_get_debug_entry(
        history_location_ref=history_location_ref,
        namespace="#V#owner@org",
        user_concept_id="#V#owner",
        organisation_concept_id="#V#org",
    )

    assert result["success"] is True
    assert result["history_location"] == {
        "session_id": "chat-debug-1",
        "history_index": 3,
    }
    assert result["identifier_binding"]["mode"] == "server_bound_reference"
    assert result["identifier_binding"]["history_index_source"] == (
        "history_location_ref"
    )


def test_turn_execution_get_live_progress_returns_provenanced_payload(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.services.turn_execution_live_progress_service.get_turn_execution_live_progress_payload",
        lambda **kwargs: {
            "success": True,
            "request_id": kwargs["request_id"],
            "resolved_scope_key": "user:#V#user",
            "status": "thinking",
            "mcp_access": {
                "turn_execution_get_live_progress": {
                    "tool_name": "turn_execution_get_live_progress",
                    "arguments": {"request_id": kwargs["request_id"]},
                }
            },
        },
    )

    result = cat._turn_execution_get_live_progress(
        request_id="req-live-1",
        namespace="#V#user@org",
        user_concept_id="#V#user",
    )

    assert result["success"] is True
    assert result["request_id"] == "req-live-1"
    assert result["resolved_scope_key"] == "user:#V#user"
    assert result["provenance"]["item_kind"] == "turn_live_progress"


def test_workflow_list_definitions_filters_by_workflow_id(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    class _StubRegistry:
        def all_workflow_ids(self):
            return ["#V#wf_alpha", "#V#wf_beta"]

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        lambda defer_parity_work=True: _StubRegistry(),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_or_build_workflow_registry_inventory_snapshot",
        lambda registry, allow_sync_build=False: {"inventory": "ok"},
    )
    listing_kwargs = {}

    def _build_listing_entry(registry, workflow_id, **kwargs):
        listing_kwargs.update(kwargs)
        return {
            "workflow_id": workflow_id,
            "name": workflow_id,
        }

    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.build_workflow_listing_entry",
        _build_listing_entry,
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_baseline_telemetry.get_workflow_baseline_telemetry_snapshot",
        lambda: {"baseline": True},
    )
    monkeypatch.setattr(
        cat,
        "build_workflow_surface_capability_matrix",
        lambda internal_method_names=None: {"capabilities": []},
    )
    monkeypatch.setattr(
        cat,
        "build_default_catalogue",
        lambda: SimpleNamespace(list_methods=lambda: ["workflow_list_definitions"]),
    )

    result = cat._workflow_list_definitions(workflow_id="#V#wf_beta", limit=10)

    assert result["success"] is True
    assert result["workflow_id_filter"] == "#V#wf_beta"
    assert result["count"] == 1
    assert result["definitions"] == [{"workflow_id": "#V#wf_beta", "name": "#V#wf_beta"}]
    assert listing_kwargs["resolve_vontology_metadata"] is True
    assert listing_kwargs["metadata_mode"] == "auto"
    assert listing_kwargs["metadata_reason_code"] == (
        "exact_workflow_id_authoritative_metadata"
    )
    assert result["metadata_resolution_summary"]["requested_mode"] == "auto"
    assert result["metadata_resolution_summary"]["resolved_vontology_metadata"] is True


def test_workflow_list_definitions_authoritative_metadata_requires_bounded_limit(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    class _StubRegistry:
        def all_workflow_ids(self):
            return ["#V#wf_alpha", "#V#wf_beta"]

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        lambda defer_parity_work=True: _StubRegistry(),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_or_build_workflow_registry_inventory_snapshot",
        lambda registry, allow_sync_build=False: {"inventory": "ok"},
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_baseline_telemetry.get_workflow_baseline_telemetry_snapshot",
        lambda: {"baseline": True},
    )
    monkeypatch.setattr(
        cat,
        "build_workflow_surface_capability_matrix",
        lambda internal_method_names=None: {"capabilities": []},
    )
    monkeypatch.setattr(
        cat,
        "build_default_catalogue",
        lambda: SimpleNamespace(list_methods=lambda: ["workflow_list_definitions"]),
    )

    listing_calls = []

    def _build_listing_entry(registry, workflow_id, **kwargs):
        listing_calls.append((workflow_id, dict(kwargs)))
        return {"workflow_id": workflow_id}

    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.build_workflow_listing_entry",
        _build_listing_entry,
    )

    bounded = cat._workflow_list_definitions(
        metadata_mode="authoritative",
        limit=2,
    )

    assert bounded["success"] is True
    assert bounded["metadata_resolution_summary"]["requested_mode"] == "authoritative"
    assert bounded["metadata_resolution_summary"]["resolved_vontology_metadata"] is True
    assert {call[1]["resolve_vontology_metadata"] for call in listing_calls} == {True}

    listing_calls.clear()
    broad = cat._workflow_list_definitions(
        metadata_mode="authoritative",
        limit=50,
    )

    assert broad["success"] is True
    assert broad["metadata_resolution_summary"]["requested_mode"] == "authoritative"
    assert broad["metadata_resolution_summary"]["resolved_vontology_metadata"] is False
    assert broad["metadata_resolution_summary"]["reason_code"] == (
        "authoritative_metadata_limit_exceeded"
    )
    assert {call[1]["resolve_vontology_metadata"] for call in listing_calls} == {False}


def test_workflow_list_use_episodes_returns_filtered_payload(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.services.workflow_episode_service.list_workflow_use_episodes",
        lambda **kwargs: [
            {
                "episode_id": "ep-1",
                "workflow_id": kwargs.get("workflow_id"),
                "session_id": kwargs.get("session_id"),
            }
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_episode_service.count_workflow_use_episodes",
        lambda **kwargs: 1,
    )

    result = cat._workflow_list_use_episodes(
        workflow_id="#V#wf_beta",
        namespace="#V#user@org",
        session_id="chat-ep-1",
        turn_id="turn-ep-1",
        limit=5,
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["total"] == 1
    assert result["filters"] == {
        "workflow_id": "#V#wf_beta",
        "namespace": "#V#user@org",
        "session_id": "chat-ep-1",
        "turn_id": "turn-ep-1",
        "limit": 5,
    }
    assert result["provenance"]["item_kind"] == "workflow_use_episode_list"
