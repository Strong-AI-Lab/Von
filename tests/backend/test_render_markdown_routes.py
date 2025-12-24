"""Tests for /von/api/render_markdown.

These tests ensure markdown is rendered to HTML and that the output is sanitised.
"""

from __future__ import annotations

import sys
import types

from bs4 import BeautifulSoup
import pytest


@pytest.fixture
def app_client(monkeypatch):
    """Provide a Flask test client with side effects stubbed out."""

    # Stub Google auth dependencies pulled in by utils_flask -> auth_routes imports.
    fake_flow_module = types.ModuleType("google_auth_oauthlib.flow")

    class _DummyFlow:
        def __init__(self, *args, **kwargs):
            self.credentials = types.SimpleNamespace(id_token="dummy-token")
            self.redirect_uri = kwargs.get("redirect_uri")
            self.client_config = {"web": {"redirect_uris": [self.redirect_uri]}}

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            return cls(**kwargs)

        def authorization_url(self, *args, **kwargs):
            return "https://auth.example", "state-token"

        def fetch_token(self, *args, **kwargs):
            return None

    fake_flow_module.Flow = _DummyFlow  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

    fake_id_token_module = types.ModuleType("google.oauth2.id_token")
    fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {  # type: ignore[attr-defined]
        "sub": "dummy-user"
    }

    fake_credentials_module = types.ModuleType("google.oauth2.credentials")

    class _DummyCredentials:
        def __init__(self, id_token: str = "dummy-token"):
            self.id_token = id_token

    fake_credentials_module.Credentials = _DummyCredentials  # type: ignore[attr-defined]

    fake_service_account_module = types.ModuleType("google.oauth2.service_account")

    class _DummyServiceAccountCredentials:
        def __init__(self, *args, **kwargs):
            self.project_id = kwargs.get("project_id")

    fake_service_account_module.Credentials = _DummyServiceAccountCredentials  # type: ignore[attr-defined]

    fake_oauth2_package = types.ModuleType("google.oauth2")
    fake_oauth2_package.id_token = fake_id_token_module  # type: ignore[attr-defined]
    fake_oauth2_package.credentials = fake_credentials_module  # type: ignore[attr-defined]
    fake_oauth2_package.service_account = fake_service_account_module  # type: ignore[attr-defined]

    sys.modules["google.oauth2"] = fake_oauth2_package
    sys.modules["google.oauth2.id_token"] = fake_id_token_module
    sys.modules["google.oauth2.credentials"] = fake_credentials_module
    sys.modules["google.oauth2.service_account"] = fake_service_account_module

    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )

    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield app, client


def test_render_markdown_renders_html(app_client):
    _, client = app_client

    markdown = "# Title\n\n- Item\n\n```python\nprint('x')\n```"
    resp = client.post("/von/api/render_markdown", json={"text": markdown})

    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data.get("html"), str)

    html = data["html"]
    assert "<h1" in html
    assert "Title" in html
    assert "<ul" in html
    assert "<pre" in html
    assert "<code" in html


def test_render_markdown_sanitises_scripts_and_js_links(app_client):
    _, client = app_client

    markdown = """
# XSS

<script>window.hacked=1</script>

[bad](javascript:alert(1))

<img src=x onerror=alert(1)>
""".strip()

    resp = client.post("/von/api/render_markdown", json={"text": markdown})

    assert resp.status_code == 200
    html = resp.get_json()["html"].lower()

    assert "<script" not in html
    assert "javascript:" not in html
    assert "<img" not in html


def test_render_markdown_validates_payload(app_client):
    _, client = app_client

    resp = client.post("/von/api/render_markdown", json={"text": 123})
    assert resp.status_code == 400


def test_render_markdown_renders_nested_lists(app_client):
    _, client = app_client

    markdown = "- read:\n  - a\n  - b\n  - c\n"
    resp = client.post("/von/api/render_markdown", json={"text": markdown})

    assert resp.status_code == 200
    html = resp.get_json()["html"]

    soup = BeautifulSoup(html, "html.parser")
    top_ul = soup.find("ul")
    assert top_ul is not None

    top_lis = top_ul.find_all("li", recursive=False)
    assert len(top_lis) == 1
    assert "read:" in top_lis[0].get_text(" ", strip=True)

    nested_ul = top_lis[0].find("ul")
    assert nested_ul is not None

    nested_items = [
        li.get_text(" ", strip=True) for li in nested_ul.find_all("li", recursive=False)
    ]
    assert nested_items == ["a", "b", "c"]


def test_render_markdown_renders_list_after_paragraph_without_blank_line(app_client):
    _, client = app_client

    markdown = """5) How to represent this in the ontology (pragmatic approach)
- Create the new types once:
  - #V#disambiguation_input_profile (type)
  - #V#disambiguation_result (type)
"""

    resp = client.post("/von/api/render_markdown", json={"text": markdown})
    assert resp.status_code == 200

    html = resp.get_json()["html"]
    soup = BeautifulSoup(html, "html.parser")

    uls = soup.find_all("ul")
    assert uls, "Expected at least one <ul> to be rendered"

    # Ensure the first list item is present.
    list_text = soup.get_text(" ", strip=True)
    assert "Create the new types once:" in list_text


def test_render_markdown_does_not_parse_vontology_ids_as_headings(app_client):
    _, client = app_client

    # Python-Markdown accepts headings without requiring a space after '#'. A list
    # item like "- #V#foo" can therefore be misparsed as a heading (<h1>) inside
    # a <li>.
    markdown = """- Types (class-level concepts)
  - #V#disambiguation_workflow (type) — already exists; can be the generic workflow backbone.
  - #V#disambiguation_input_profile (type) — represents the input knowledge state used to drive a disambiguation instance.

> #V#disambiguation_result (type) — quoted example should also not become a heading.
"""

    resp = client.post("/von/api/render_markdown", json={"text": markdown})
    assert resp.status_code == 200

    html = resp.get_json()["html"]
    soup = BeautifulSoup(html, "html.parser")

    assert soup.find("h1") is None

    text = soup.get_text(" ", strip=True)
    assert "#V#disambiguation_workflow" in text
    assert "#V#disambiguation_input_profile" in text
    assert "#V#disambiguation_result" in text

    # Ensure the normaliser does not leak markdown escape backslashes into HTML.
    assert "\\#V#" not in html
