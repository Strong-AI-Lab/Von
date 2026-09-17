from types import SimpleNamespace
from flask import Flask, jsonify
import pytest
from src.backend.server.routes import settings_routes as routes
from src.backend.languagemodels import llm_interface


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(routes.settings_bp, url_prefix='/api/settings')
    monkeypatch.setattr(routes, 'get_effective_user_concept_id', lambda: '#V#actor')
    monkeypatch.setattr(routes, 'get_effective_organisation_concept_id', lambda: '#V#org')
    monkeypatch.setattr(routes, 'OpenAIClient', lambda **kw: SimpleNamespace(
        list_models=lambda: ['gpt-6-astra', 'gpt-6-astra-preview', 'other-model']
    ))
    return app.test_client()


def test_picker_uses_execution_normalisation_and_trusted_scope(client, monkeypatch):
    def pool(**scope):
        assert scope == dict(user_concept_id='#V#actor', org_concept_id='#V#org')
        return [dict(provider=' OpenAI ', model='openai:gpt-6-astra'),
                dict(provider='gemini', model='other-model')]
    monkeypatch.setattr(routes, 'resolve_enabled_llm_settings', pool)
    monkeypatch.setattr(llm_interface, 'resolve_enabled_llm_settings', pool)
    response = client.get('/api/settings/models/enabled/openai?user_concept_id=other')
    assert response.status_code == 200
    assert response.get_json() == ['gpt-6-astra']
    assert 'no-store' in response.headers['Cache-Control']
    assert llm_interface.assert_model_execution_allowed(
        provider='openai', model=response.get_json()[0],
        user_concept_id='#V#actor', org_concept_id='#V#org'
    )['allowed']
    # Settings discovery still exposes models which can be enabled later.
    assert client.get('/api/settings/models/openai').get_json() == [
        'gpt-6-astra', 'gpt-6-astra-preview', 'other-model'
    ]


def test_missing_scope_does_not_discover_or_read_pool(client, monkeypatch):
    monkeypatch.setattr(routes, 'get_effective_user_concept_id', lambda: None)
    monkeypatch.setattr(routes, 'get_effective_organisation_concept_id', lambda: None)
    monkeypatch.setattr(routes, 'resolve_enabled_llm_settings', lambda **kw: pytest.fail('no actor'))
    assert client.get('/api/settings/models/enabled/openai').get_json() == []


def test_unavailable_scope_is_not_reported_as_empty_pool(client, monkeypatch):
    def unavailable(**kw):
        raise RuntimeError('offline')
    monkeypatch.setattr(routes, 'resolve_enabled_llm_settings', unavailable)
    assert client.get('/api/settings/models/enabled/openai').status_code == 503


def test_ollama_retains_local_execution_exception(client, monkeypatch):
    monkeypatch.setattr(routes, 'resolve_enabled_llm_settings', lambda **kw: pytest.fail('local'))
    monkeypatch.setattr(routes, 'get_ollama_models', lambda: (jsonify(['local-model']), 200))
    assert client.get('/api/settings/models/enabled/ollama').get_json() == ['local-model']
