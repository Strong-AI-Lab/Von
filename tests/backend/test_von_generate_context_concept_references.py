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
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _add_to_history,
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")
    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    return flask_app


def test_generate_attaches_prior_turn_concept_reference_metadata(app):
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
    assert by_id["#V#person"]["exists"] is True
    assert by_id["#V#person"]["kind"] == "type"
    assert by_id["#V#person"]["name"] == "Person"
