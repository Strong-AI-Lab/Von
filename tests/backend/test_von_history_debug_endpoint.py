from flask import Flask


def test_history_exposes_conversation_situation_without_message_segments(
    monkeypatch,
):
    from src.backend.server.routes import von_routes

    situation = {
        "text": "The user and Von are planning the first implementation slice.",
        "revision": 3,
        "source": "adaptive_turn",
        "updated_by": "#V#test_user",
        "updated_at": "2026-07-29T09:00:00+00:00",
    }

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#test_user", None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
            "conversation_situation": situation,
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")

    response = app.test_client().get(
        "/von/history",
        query_string={"session_id": "session-empty"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "history": [],
        "segments_returned": 0,
        "total_segments": 0,
        "has_more_history": False,
        "conversation_situation": situation,
        "conversation_observations": [],
        "conversation_observation_state": {
            "schema_version": "conversation_observation_state.v1",
            "retained_count": 0,
            "total_count": 0,
            "omitted_count": 0,
            "retention_limit": 12,
        },
    }


def test_history_projects_terminal_concept_q_and_a_carrier_state(monkeypatch):
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#test_user", None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
            "mode": "concept_q_and_a",
            "origin_kind": "concept_q_and_a",
            "focal_concept_ids": ["#V#primary_labs"],
            "concept_q_and_a": {
                "schema_version": "concept_q_and_a_session.v1",
                "concept": {
                    "concept_id": "#V#primary_labs",
                    "name": "Primary Labs",
                },
                "lifecycle": {"status": "cancelled", "revision": 1},
            },
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history",
        query_string={"session_id": "qa-terminal"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["session"]["mode"] == "concept_q_and_a"
    assert body["session"]["focal_concept_ids"] == ["#V#primary_labs"]
    assert body["qa_session"]["lifecycle"]["status"] == "cancelled"


def test_history_projects_retryable_initial_concept_q_and_a_question(monkeypatch):
    from src.backend.server.routes import von_routes

    initial_question = {
        "status": "retryable_failure",
        "retryable": True,
        "attempt_count": 1,
        "failure": {
            "error_code": "initial_question_generation_failed",
            "message": "The initial question could not be generated.",
        },
    }
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: ("#V#test_user", None),
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_state",
        lambda **kwargs: {
            "session_id": kwargs["session_id"],
            "history": [],
            "mode": "concept_q_and_a",
            "origin_kind": "concept_q_and_a",
            "focal_concept_ids": ["#V#primary_labs"],
            "concept_q_and_a": {
                "schema_version": "concept_q_and_a_session.v1",
                "concept": {
                    "concept_id": "#V#primary_labs",
                    "name": "Primary Labs",
                },
                "lifecycle": {"status": "active", "revision": 0},
                "initial_question": initial_question,
            },
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().get(
        "/von/history",
        query_string={"session_id": "qa-retryable"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["session"]["initial_question"] == initial_question
    assert body["qa_session"]["initial_question"] == initial_question


def test_history_endpoint_returns_compact_debug_refs_without_hydration(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    calls: dict[str, object] = {}
    compact_debug_ref = {
        "schema_version": "debug_payload_blob_ref.v1",
        "field_path": "messages",
        "summary": {"type": "array", "item_count": 1},
        "blob_ref": {"backend": "local", "key": "debug/turns/test/messages.json.gz"},
    }

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.resolve_chat_history_namespace",
        lambda _user_id: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )

    situation = {
        "text": "We are checking compact history retrieval.",
        "revision": 2,
        "source": "adaptive_turn",
        "updated_by": "#V#test_user",
        "updated_at": "2026-06-03T00:01:00+00:00",
    }
    observations = [
        {
            "observation_id": "late-effect:req-compact",
            "kind": "durable_effect_terminal",
            "summary": "The represented effect completed after its originating turn.",
        }
    ]

    def _session_state(*, user_id, session_id, namespace, **_kwargs):
        calls["state"] = {
            "user_id": user_id,
            "session_id": session_id,
            "namespace": namespace,
        }
        return {
            "session_id": session_id,
            "conversation_situation": situation,
            "conversation_observations": observations,
            "history": [
                {
                    "role": "assistant",
                    "content": "compact answer",
                    "timestamp": "2026-06-03T00:00:00Z",
                    "history_location": {
                        "session_id": "session-compact",
                        "history_index": 0,
                    },
                    "llm_debug_data": {
                        "request_id": "req-compact",
                        "messages": compact_debug_ref,
                    },
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_session_state",
        _session_state,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_debug_entry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("normal history must not hydrate debug entries")
        ),
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    response = app.test_client().get(
        "/von/history",
        query_string={"session_id": "session-compact", "segments": 1},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["segments_returned"] == 1
    history = body["history"]
    assert history[0]["llm_debug_data"]["messages"] == compact_debug_ref
    assert "large raw payload" not in str(body)
    assert body["conversation_situation"] == situation
    assert body["conversation_observations"] == observations
    assert calls["state"] == {
        "user_id": "#V#test_user",
        "session_id": "session-compact",
        "namespace": "#V#test_user",
    }


def test_history_endpoint_projects_reference_manifest_for_legacy_turn(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    evidence_id = "ev_legacy_history_evidence"
    instance_id = "bd571204-d0ac-43e6-8764-11d757c37d35"
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {
            "namespace": "#V#test_user@org",
            "organisation_id": "#V#org",
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_session_state",
        lambda **_kwargs: {
            "history": [
                {
                    "role": "assistant",
                    "content": f"instance `{instance_id}`; evidence `{evidence_id}`",
                    "llm_debug_data": {
                        "request_id": "request-legacy-history",
                        "aux_llm_calls": [
                            {
                                "type": "adaptive_turn_evidence_index",
                                "evidence": [
                                    {
                                        "evidence_id": evidence_id,
                                        "preview": "bounded legacy preview",
                                    }
                                ],
                            },
                            {
                                "type": "adaptive_turn_effect_outcome_report",
                                "schema_version": "adaptive_turn_effect_outcome_report.v1",
                                "facts": [
                                    {
                                        "instance_id": instance_id,
                                        "workflow_id": "#V#legacy_workflow",
                                        "effect_status": "failed",
                                        "evidence_id": evidence_id,
                                    }
                                ],
                            },
                        ],
                    },
                }
            ]
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    response = app.test_client().get(
        "/von/history",
        query_string={"session_id": "session-legacy", "segments": 1},
    )

    assert response.status_code == 200
    debug = response.get_json()["history"][0]["llm_debug_data"]
    manifest = debug["reference_manifest"]
    references = {item["reference_id"]: item for item in manifest["references"]}
    assert references[evidence_id]["reference_type"] == "turn_evidence"
    assert references[evidence_id]["evidence"]["preview"] == "bounded legacy preview"
    assert references[instance_id]["reference_type"] == "workflow_instance"


def test_history_endpoint_projects_bounded_latest_llm_credential_summary(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user@org"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_session_state",
        lambda **_kwargs: {
            "history": [
                {
                    "role": "assistant",
                    "content": "Used the backup credential.",
                    "timestamp": "2026-09-03T15:38:44Z",
                    "turn_id": "a-request-backup",
                    "history_location": {
                        "session_id": "session-backup",
                        "history_index": 3,
                    },
                }
            ]
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_debug_entry",
        lambda **_kwargs: {
            "model": "gpt-5.6-luna",
            "messages": [{"role": "user", "content": "private prompt"}],
            "llm_interaction": {
                "requested_model": "gpt-5.6-luna",
                "calls": [
                    {
                        "type": "adaptive_turn_model_call",
                        "model": "gpt-5.6-luna",
                        "provider": "openai",
                        "transport": {
                            "credential_source": "backup",
                            "credential_failover_used": True,
                            "primary_credential_failure_kind": "quota_exhausted",
                            "api_key": "sk-must-not-leak",
                        },
                        "response": "private provider response",
                    }
                ],
            },
        },
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    response = app.test_client().get(
        "/von/history",
        query_string={
            "session_id": "session-backup",
            "segments": 1,
            "include_debug": 0,
        },
    )

    assert response.status_code == 200
    message = response.get_json()["history"][0]
    assert "llm_debug_data" not in message
    assert message["llm_execution_summary"] == {
        "schema_version": "history_llm_execution_summary.v1",
        "model": "gpt-5.6-luna",
        "llm_interaction": {
            "requested_model": "gpt-5.6-luna",
            "calls": [
                {
                    "type": "adaptive_turn_model_call",
                    "model": "gpt-5.6-luna",
                    "provider": "openai",
                    "transport": {
                        "credential_source": "backup",
                        "credential_failover_used": True,
                        "primary_credential_failure_kind": "quota_exhausted",
                    },
                }
            ],
        },
    }
    response_text = response.get_data(as_text=True)
    assert "sk-must-not-leak" not in response_text
    assert "private prompt" not in response_text
    assert "private provider response" not in response_text


def test_add_chat_history_message_persists_bounded_llm_execution_summary(monkeypatch):
    from src.backend.server.routes import von_routes

    stored: dict[str, object] = {}

    def _store(_user_id, _session_id, message, **kwargs):
        stored["message"] = message
        stored["debug"] = kwargs.get("llm_debug_data")

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "add_message_to_history",
        _store,
    )

    debug = {
        "model": "gpt-5.6-luna",
        "llm_interaction": {
            "requested_model": "gpt-5.6-luna",
            "calls": [
                {
                    "model": "gpt-5.6-luna",
                    "provider": "openai",
                    "transport": {
                        "credential_source": "primary",
                        "credential_failover_used": False,
                        "api_key": "sk-never-project",
                    },
                }
            ],
        },
    }
    von_routes._add_chat_history_message(
        user_id="#V#test_user",
        session_id="session-primary",
        message={"role": "assistant", "content": "Done"},
        llm_debug_data=debug,
    )

    message = stored["message"]
    assert isinstance(message, dict)
    assert message["llm_execution_summary"]["llm_interaction"]["calls"][0][
        "transport"
    ] == {
        "credential_source": "primary",
        "credential_failover_used": False,
    }
    assert "sk-never-project" not in repr(message["llm_execution_summary"])
    assert stored["debug"] is debug


def test_history_debug_returns_stored_turn_execution_diagnostics(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    debug_payload = {
        "model": "test-model",
        "turn_execution_diagnostics": {
            "generated_at_utc": "2026-02-18T00:00:00Z",
            "request_id": "req-history-1",
            "elapsed_ms": 987,
            "prompt_preview": "Summarise today",
            "latest_progress": {"status": "completed", "stage": "completed"},
            "progress_events": [{"status": "completed", "stage": "completed"}],
            "phase_history": [{"phase": "tool_execute"}],
            "tool_history": [{"tool": "search_knowledge_base", "success": True}],
            "workflow_discovery": {"matches": [{"concept_id": "#V#workflow_demo"}]},
        },
    }

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.resolve_chat_history_namespace",
        lambda _user_id: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: True,
    )

    def _get_debug_entry(*, user_id, session_id, history_index, namespace):
        calls["user_id"] = user_id
        calls["session_id"] = session_id
        calls["history_index"] = history_index
        calls["namespace"] = namespace
        return debug_payload

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_debug_entry",
        _get_debug_entry,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/debug",
        query_string={"session_id": "session-1", "history_index": 3},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("success") is True
    assert body.get("history_location") == {
        "session_id": "session-1",
        "history_index": 3,
    }

    returned = body.get("llm_debug_data")
    assert isinstance(returned, dict)
    assert returned == debug_payload
    diagnostics = returned.get("turn_execution_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("request_id") == "req-history-1"
    assert diagnostics.get("prompt_preview") == "Summarise today"

    assert calls == {
        "user_id": "#V#test_user",
        "session_id": "session-1",
        "history_index": 3,
        "namespace": "#V#test_user",
    }


def test_history_debug_uses_org_hint_to_upgrade_bare_namespace(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {"namespace": "#V#test_user"},
    )

    def _has_chat_history_session(user_id, session_id, namespace=None, **_kwargs):
        calls["checked_namespace"] = namespace
        return namespace == "#V#test_user@org"

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.has_chat_history_session",
        _has_chat_history_session,
    )

    def _get_debug_entry(*, user_id, session_id, history_index, namespace):
        calls["user_id"] = user_id
        calls["session_id"] = session_id
        calls["history_index"] = history_index
        calls["namespace"] = namespace
        return {"request_id": "req-org-1"}

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_debug_entry",
        _get_debug_entry,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")

    client = app.test_client()
    response = client.get(
        "/von/history/debug",
        query_string={
            "session_id": "session-1",
            "history_index": 3,
            "organisation_concept_id": "#V#org",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert calls["checked_namespace"] == "#V#test_user@org"
    assert calls["namespace"] == "#V#test_user@org"
