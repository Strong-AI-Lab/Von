import json

import pytest
from flask import Flask

from src.backend.services import execution_cost_display_service as service


def test_predicate_resolves_without_database_registration(monkeypatch):
    from src.backend.services import text_relation_predicate_validation_service as validation
    from src.backend.vontology.code_concepts_registry import build_virtual_concept_doc

    monkeypatch.setattr(validation.concept_service, "get_concept_by_concept_id", build_virtual_concept_doc)
    resolved = validation.resolve_text_relation_predicate_for_write(service.PREDICATE)
    assert resolved.storage_predicate == service.PREDICATE
    assert "#V#binary_text_predicate" in resolved.predicate_doc["relationships"]["is_an_instance_of"]


def test_scope_precedence_and_fallback(monkeypatch):
    values = {}
    monkeypatch.setattr(service, "get_texts_for_concept", lambda subject, **_: values.get(subject, []))
    assert service.resolve_preferences("user", "org") == {**service.DEFAULTS, "source": "default"}
    org = {"currency": "USD", "display_above": "0.02", "alert_above": "0.20"}
    user = {"currency": "USD", "display_above": "0.03", "alert_above": "0.30"}
    values["org"] = [{"text": json.dumps(org)}]
    assert service.resolve_preferences("user", "org") == {**org, "source": "organisation"}
    values["user"] = [{"text": json.dumps(user)}]
    assert service.resolve_preferences("user", "org") == {**user, "source": "user"}
    for invalid in ({}, {"currency": "USD"}, {**user, "alert_above": 0.3}):
        values["user"] = [{"text": json.dumps(invalid)}]
        assert service.resolve_preferences("user", "org")["source"] == "organisation"
    values["user"] *= 2
    assert service.resolve_preferences("user", None)["source"] == "default"


@pytest.mark.parametrize("value", [None, [], {**service.DEFAULTS, "currency": "NZD"},
    {**service.DEFAULTS, "display_above": "NaN"}, {**service.DEFAULTS, "alert_above": "-1"},
    {**service.DEFAULTS, "display_above": "0.11"}, {**service.DEFAULTS, "alert_above": 0.1}])
def test_invalid_preferences(value):
    with pytest.raises(ValueError):
        service.validate_preferences(value)


def test_preferences_endpoint_scope_write_and_readback(monkeypatch):
    from src.backend.server.routes import settings_routes as routes
    from src.backend.services import ontology_mutation_command_service as governance
    from src.backend.services import text_value_service

    stored = {}
    monkeypatch.setattr(routes, "get_effective_user_concept_id", lambda: "#V#viewer")
    monkeypatch.setattr(routes, "get_effective_context", lambda *_: {"organisation_id": "#V#org"})
    monkeypatch.setattr(service, "get_texts_for_concept", lambda subject, **_: stored.get(subject, []))
    mutations = []

    def write(**kwargs):
        mutations.append(kwargs)
        stored[kwargs["subject_concept_id"]] = [{"text": kwargs["text"]}]
        return {"success": True}

    monkeypatch.setattr(text_value_service, "upsert_singleton_text_relation", write)
    monkeypatch.setattr(governance, "execute_governed_ontology_method", lambda **kw: kw["mutate"]())
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(routes.settings_bp, url_prefix="/api/settings")
    client = app.test_client()
    url = "/api/settings/execution_cost_display"
    assert client.get(url).json["source"] == "default"
    assert client.put(url, json=service.DEFAULTS).json == {**service.DEFAULTS, "source": "user"}
    assert mutations[0]["subject_concept_id"] == "#V#viewer"
    assert mutations[0]["predicate"] == service.PREDICATE
    assert client.get(url + "?user_id=other&organisation_id=other").json["source"] == "user"
    assert client.put(url, json={**service.DEFAULTS, "user_id": "other"}).status_code == 400
    assert client.put(url, json={}).json["source"] == "default"
    monkeypatch.setattr(routes, "get_effective_user_concept_id", lambda: None)
    assert client.get(url).status_code == 401
    assert client.put(url, json=service.DEFAULTS).status_code == 401
