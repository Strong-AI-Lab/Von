import pytest
from flask import Flask

from src.backend.services.adaptive_turn_service import AdaptiveTurnResult

_VERSION_INFO = {
    "schema_version": "runtime_code_version.v1",
    "version": "test-version+gabcd1234",
    "source": "test",
    "legacy_app_version": "legacy-test-version",
    "git_commit": "abcd1234abcd1234abcd1234abcd1234abcd1234",
    "git_short_commit": "abcd1234",
    "git_branch": "test-branch",
    "git_dirty": False,
}


class _StubLLM:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return "ok"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_generate_stored_context")

    from src.backend.server.routes import von_routes
    from src.backend.server.routes.von_routes import von_bp

    llm = _StubLLM()

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )

    def _execute_adaptive_turn(**kwargs):
        llm.calls.append(
            {
                "prompt": kwargs["prompt"],
                "context": list(kwargs["context"]),
                "model": kwargs["model"],
            }
        )
        return AdaptiveTurnResult(
            response_text="ok",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        )

    monkeypatch.setattr(von_routes, "execute_adaptive_turn", _execute_adaptive_turn)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_generate_requested_model",
        lambda *_args, **_kwargs: ("test-model", None, {}),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_runtime_code_version_info",
        lambda: dict(_VERSION_INFO),
    )
    monkeypatch.setattr(von_routes, "get_show_tool_use_during_thinking", lambda: False)
    monkeypatch.setattr(von_routes, "get_buttonify_model_enabled", lambda: False)
    monkeypatch.setattr(
        von_routes,
        "get_display_elements_screen_fence_compat_enabled",
        lambda **_kwargs: True,
    )

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )

    # Avoid touching the database in this test.
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "ok"},
        ],
    )
    monkeypatch.setattr(
        von_routes,
        "_ensure_generate_conversation_session",
        lambda **_kwargs: ("test-session", None, False),
    )
    monkeypatch.setattr(
        von_routes,
        "_resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(von_routes, "_add_chat_history_message", lambda **_kwargs: None)
    monkeypatch.setattr(
        von_routes,
        "build_context_concept_reference_metadata",
        lambda *_args, **_kwargs: {
            "source": "sent_context_user_assistant",
            "metadata_version": 1,
            "message_roles": ["user", "assistant"],
            "messages_scanned": 0,
            "concept_count": 0,
            "concept_count_capped": False,
            "max_concepts": None,
            "include_direct_supertypes": False,
            "max_direct_supertypes": 0,
            "concepts": [],
        },
    )
    monkeypatch.setattr(
        von_routes.PromptTemplateService,
        "resolve_prompt_text",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.services.mail_profile_resource_vontology_service.resolve_authorised_gmail_profile_for_user",
        lambda **_kwargs: {
            "success": False,
            "reason_code": "default_mail_profile_not_represented",
            "profile_id": None,
        },
    )

    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    return flask_app


def test_generate_debug_stored_context_uses_persisted_history_for_authenticated_user(
    app,
):
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "Hello"})
    assert resp.status_code == 200

    body = resp.get_json()
    llm_debug = body["llm_debug"]

    # Previously this was empty because it always reported app.config["CONTEXT"],
    # which is only used for unauthenticated sessions.
    stored = llm_debug["context_stats"]["stored_context"]
    assert stored["total_messages"] >= 2
    assert stored["by_role"]["user"] >= 1
    assert stored["by_role"]["assistant"] >= 1

    internal_mcp = llm_debug["internal_mcp"]
    assert internal_mcp["gateway_present"] is False
    assert internal_mcp["gateway_enabled"] is False
    assert internal_mcp["orchestrator_present"] is False

    diagnostics = llm_debug.get("turn_execution_diagnostics")
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("schema_version") == "turn_execution_diagnostics.v1"
    assert diagnostics.get("request_id") == body.get("request_id")
    assert diagnostics.get("prompt_preview") == "Hello"
    assert llm_debug.get("code_version") == _VERSION_INFO["version"]
    assert llm_debug.get("code_version_details") == _VERSION_INFO
    assert diagnostics.get("code_version") == _VERSION_INFO["version"]
    assert diagnostics.get("code_version_details") == _VERSION_INFO
    assert isinstance(diagnostics.get("progress_events"), list)
    assert isinstance(diagnostics.get("phase_history"), list)
    assert isinstance(diagnostics.get("tool_history"), list)
    assert "workflow_stage_model" not in diagnostics
    assert "workflow_stage_path" not in diagnostics

    turn_execution_record = llm_debug.get("turn_execution_record")
    assert isinstance(turn_execution_record, dict)
    assert (
        turn_execution_record.get("schema_version")
        == "turn_execution_record.observational.v1"
    )
    assert turn_execution_record.get("record_kind") == "observational"
    assert "completion_gate" not in turn_execution_record
    assert "execution_correctness" not in turn_execution_record
    assert (
        llm_debug["llm_interaction"]["ordinary_turn_engine"]
        == "direct_adaptive_turn"
    )


def test_generate_scopes_chat_history_reads_to_authenticated_namespace(
    app,
    monkeypatch,
) -> None:
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *_args, **_kwargs: {
            "organisation_id": "#V#test_org",
            "chat_session_id": "test-session",
            "role": "member",
        },
    )
    history_reads = []

    def _get_history(user_id, session_id, **kwargs):
        history_reads.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                **kwargs,
            }
        )
        return [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "ok"},
        ]

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history",
        _get_history,
    )

    response = app.test_client().post(
        "/von/generate",
        json={"prompt": "Use my prior context."},
        headers={"X-Von-Window-Session": "window-context"},
    )

    assert response.status_code == 200
    assert history_reads
    assert all(call["user_id"] == "#V#test_user" for call in history_reads)
    assert all(
        call.get("namespace") == "#V#test_user@test_org"
        for call in history_reads
    )


def test_generate_marks_live_progress_as_observational(app, monkeypatch) -> None:
    from src.backend.server.routes import von_routes

    emitted_updates = []
    monkeypatch.setattr(
        von_routes,
        "get_show_tool_use_during_thinking",
        lambda: True,
    )
    monkeypatch.setattr(
        von_routes,
        "_set_tool_progress",
        lambda _scope_key, _request_id, update: emitted_updates.append(dict(update)),
    )
    monkeypatch.setattr(
        von_routes,
        "_start_tool_progress_heartbeat",
        lambda _scope_keys, _request_id: (None, None),
    )

    response = app.test_client().post("/von/generate", json={"prompt": "Hello"})

    assert response.status_code == 200
    assert emitted_updates
    assert all(
        update.get("record_kind") == "observational" for update in emitted_updates
    )
    assert all("workflow_stage_path" not in update for update in emitted_updates)


def test_generate_debug_turn_execution_record_attributes_coding_agent_actor(
    app, monkeypatch
):
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#github_copilot_instance",
    )

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})
    assert resp.status_code == 200

    body = resp.get_json()
    llm_debug = body["llm_debug"]
    turn_execution_record = llm_debug.get("turn_execution_record")
    assert isinstance(turn_execution_record, dict)
    actor = turn_execution_record.get("actor")
    assert isinstance(actor, dict)
    assert actor.get("actor_concept_id") == "#V#github_copilot_instance"
    assert llm_debug.get("actor_concept_id") == "#V#github_copilot_instance"
