import pytest
from flask import Flask


class _StubLLM:
    def generate(self, prompt, context, model):
        return "ok"


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: _StubLLM(),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda *_args, **_kwargs: {
            "chat_session_id": "session-1",
            "organisation_id": "#V#test_org",
            "namespace": "#V#test_user@test_org",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )

    def _stub_node_content(concept_id: str, *, reconstruct_md: bool = True):
        if concept_id == "#V#person":
            return {
                "concept_id": concept_id,
                "display_name": "Person",
                "kind": "type",
                "is_a_type_of": [],
            }
        return {"error": "not found"}

    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_vontology_node_content",
        _stub_node_content,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_texts_for_concept",
        lambda **_kwargs: [
            {"text": "Person", "lang": "en-NZ", "context": {"name_type": "NL"}}
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.ConceptsRepository.find",
        lambda *_args, **_kwargs: [],
    )

    history_store: dict[tuple[str, str], list[dict]] = {}

    def _get_history(user_id, session_id, *_, **__):
        key = (str(user_id), str(session_id))
        return [dict(item) for item in history_store.get(key, [])]

    def _add_to_history(user_id, session_id, message, llm_debug_data=None, **_kwargs):
        key = (str(user_id), str(session_id))
        bucket = history_store.setdefault(key, [])
        row = dict(message)
        if llm_debug_data is not None:
            row["llm_debug_data"] = dict(llm_debug_data)
        bucket.append(row)

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        _get_history,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history_session_state",
        lambda user_id, session_id, **_kwargs: {
            "session_id": session_id,
            "history": _get_history(user_id, session_id),
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _add_to_history,
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")
    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None
    flask_app.config["_TEST_HISTORY_STORE"] = history_store

    return flask_app


def test_generate_defers_prior_turn_concept_reference_resolution(app):
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#test_user"
        sess["session_id"] = "session-1"
        sess["namespace"] = "#V#test_user@test_org"

    first = client.post("/von/generate", json={"prompt": "Please open #V#person"})
    assert first.status_code == 200
    first_refs = first.get_json()["llm_debug"]["context_concept_references"]
    assert first_refs["concept_count"] == 0

    second = client.post("/von/generate", json={"prompt": "Continue please"})
    assert second.status_code == 200
    second_refs = second.get_json()["llm_debug"]["context_concept_references"]
    by_id = {entry["concept_id"]: entry for entry in second_refs["concepts"]}

    assert "#V#person" in by_id
    assert second_refs["resolution_status"] == "deferred"
    assert by_id["#V#person"]["exists"] is None
    assert by_id["#V#person"]["kind"] is None
    assert by_id["#V#person"]["name"] is None
    assert by_id["#V#person"]["resolution_status"] == "deferred"


def test_generate_projects_long_history_before_model_use(app):
    history_store = app.config["_TEST_HISTORY_STORE"]
    history_store[("#V#test_user", "session-1")] = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"Earlier message {index}",
        }
        for index in range(30)
    ]
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user_concept_id"] = "#V#test_user"
        sess["session_id"] = "session-1"
        sess["namespace"] = "#V#test_user@test_org"

    response = client.post("/von/generate", json={"prompt": "Continue please"})

    assert response.status_code == 200
    debug = response.get_json()["llm_debug"]
    projection = debug["namespace_report"][
        "conversation_history_model_projection"
    ]
    assert projection == {
        "schema_version": "conversation_history_model_projection.v1",
        "source_message_count": 30,
        "projected_message_count": 20,
        "older_context_carrier": "recent_transcript_window_only",
    }
    assert debug["context_stats"]["sent_to_llm"]["total_messages"] <= 22
