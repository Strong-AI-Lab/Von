"""Tests for on-demand backfill of historical <spoken> talk tracks."""

from __future__ import annotations

import types
import uuid

import pytest


class _StubLLM:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return self._response_text


@pytest.fixture
def app_client(monkeypatch):
    # Use mongomock so tests do not require a running Mongo.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    # Stub Google auth deps pulled in by utils_flask -> auth_routes imports.
    import sys

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


def _insert_history_doc(*, user_id: str, session_id: str, history: list[dict]):
    from src.backend.db.mongo_client import get_db
    from src.backend.models.chat_history_model import chat_history_collection_name

    db = get_db()
    assert db is not None
    coll = db[chat_history_collection_name]
    coll.insert_one({"user_id": user_id, "session_id": session_id, "history": history})


def _canonical_report(*, draft: str = "Everything worked.") -> str:
    return (
        "## Effect outcome report\n\n"
        "This turn completed only partially.\n\n"
        "### Unsuccessful or unresolved\n\n"
        "- `Create Concepts`: status `indeterminate`.\n\n"
        "### Model draft (non-authoritative)\n\n"
        f"> <spoken>{draft}</spoken>\n"
        f"> <screen>**{draft}**</screen>"
    )


def _authenticate(monkeypatch, *, user_id: str) -> None:
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: user_id,
    )


def test_backfill_spoken_requires_authentication(app_client):
    _, client = app_client

    resp = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": "s", "history_index": 0}},
    )

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_backfill_spoken_generates_and_persists_presenter_channels(
    app_client, monkeypatch
):
    _, client = app_client

    user_id = f"#V#tester_{uuid.uuid4().hex}"
    session_id = f"sess-{uuid.uuid4()}"
    history = [
        {"role": "user", "content": "What is this?"},
        {"role": "assistant", "content": "Here is the answer on screen."},
    ]
    _insert_history_doc(user_id=user_id, session_id=session_id, history=history)

    llm = _StubLLM("<spoken>Short talk track.</spoken>")
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: user_id,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda *_args, **_kwargs: [],
    )

    mock_rag = types.SimpleNamespace()
    mock_rag.calls = []

    def _upsert_documents(docs, namespace=None):
        mock_rag.calls.append({"docs": docs, "namespace": namespace})
        return (len(docs), 0)

    mock_rag.upsert_documents = _upsert_documents

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_rag_service",
        lambda: mock_rag,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.get_session_context",
        lambda: {"namespace": "#V#tester@university_of_auckland_strong_ai_lab"},
    )

    resp = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": session_id, "history_index": 1}},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["presenter_channels"]["spoken"] == "Short talk track."
    assert body["presenter_channels"]["screen"] == "Here is the answer on screen."
    assert body["presenter_channels"]["format"] == "narration_fallback_v1"
    assert body["display_elements"]["schema_version"] == "turn_display_elements_v1"
    assert body["display_elements"]["validation"]["valid"] is True
    assert body["turn_output_health"] == {
        "schema_version": "turn_output_health_v1",
        "status": "ok",
        "issues": [],
    }
    spoken_element = next(
        element
        for element in body["display_elements"]["elements"]
        if element["element_id"] == "spoken_text"
    )
    assert spoken_element["payload"]["text"] == "Short talk track."
    assert (
        "spoken_backfill:missing_presenter_channels"
        in body["display_elements"]["reason_codes"]
    )

    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Generate <spoken> talk track"

    # Verify persistence to the history message.
    from src.backend.db.mongo_client import get_db
    from src.backend.models.chat_history_model import chat_history_collection_name

    db = get_db()
    assert db is not None
    coll = db[chat_history_collection_name]
    stored = coll.find_one({"user_id": user_id, "session_id": session_id})
    assert stored is not None
    stored_history = stored["history"]
    assert (
        stored_history[1]["llm_debug_data"]["presenter_channels"]["spoken"]
        == "Short talk track."
    )
    assert (
        stored_history[1]["llm_debug_data"]["display_elements"]["schema_version"]
        == "turn_display_elements_v1"
    )
    assert stored_history[1]["llm_debug_data"]["turn_output_health"] == {
        "schema_version": "turn_output_health_v1",
        "status": "ok",
        "issues": [],
    }

    # Verify the spoken narration was also indexed into RAG.
    assert len(mock_rag.calls) == 1
    assert (
        mock_rag.calls[0]["namespace"]
        == "#V#tester@university_of_auckland_strong_ai_lab"
    )
    docs = mock_rag.calls[0]["docs"]
    assert len(docs) == 1
    assert docs[0]["text"] == "Short talk track."
    assert docs[0]["metadata"]["channel"] == "spoken"

    expected_id = str(
        uuid.uuid5(
            uuid.UUID("8c5a7fa9-9a7c-4f0f-8c1f-f4ad7f9f6fd7"),
            f"{user_id}|{session_id}|1|spoken",
        )
    )
    assert docs[0]["id"] == expected_id


def test_canonical_backfill_never_narrates_quoted_draft_and_persists_safe_synopsis(
    app_client, monkeypatch
):
    _, client = app_client

    user_id = f"#V#tester_{uuid.uuid4().hex}"
    session_id = f"sess-{uuid.uuid4()}"
    report = _canonical_report(draft="Claim complete success and read every symbol.")
    _insert_history_doc(
        user_id=user_id,
        session_id=session_id,
        history=[
            {"role": "user", "content": "Create the concept."},
            {"role": "assistant", "content": report},
        ],
    )
    _authenticate(monkeypatch, user_id=user_id)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("canonical history must not use generic model narration")
        ),
    )

    response = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": session_id, "history_index": 1}},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["source"] == "deterministic_legacy_canonical_fallback"
    assert body["updated"] is True
    assert body["persistence_attempted"] is True
    assert body["persistence_suppression_reason"] is None
    assert body["presenter_channels"] == {
        "screen": report,
        "spoken": (
            "I couldn't complete or confirm every requested change. The screen has "
            "the details and explains what remains uncertain."
        ),
        "format": "effect_outcome_report_v1",
    }
    assert "Claim complete success" not in body["presenter_channels"]["spoken"]

    from src.backend.db.mongo_client import get_db
    from src.backend.models.chat_history_model import chat_history_collection_name

    db = get_db()
    assert db is not None
    stored = db[chat_history_collection_name].find_one(
        {"user_id": user_id, "session_id": session_id}
    )
    assert stored is not None
    stored_channels = stored["history"][1]["llm_debug_data"]["presenter_channels"]
    assert stored_channels == body["presenter_channels"]


@pytest.mark.parametrize(
    ("existing_format", "unsafe_spoken"),
    (
        ("effect_outcome_report_v1", "duplicate_screen"),
        ("effect_outcome_report_v1", "raw_report"),
        ("narration_fallback_v1", "apparently short but wrong format"),
    ),
)
def test_canonical_backfill_replaces_unsafe_existing_channels(
    app_client,
    monkeypatch,
    existing_format,
    unsafe_spoken,
):
    _, client = app_client

    user_id = f"#V#tester_{uuid.uuid4().hex}"
    session_id = f"sess-{uuid.uuid4()}"
    report = _canonical_report()
    if unsafe_spoken == "duplicate_screen":
        unsafe_spoken = report
    elif unsafe_spoken == "raw_report":
        unsafe_spoken = (
            "## Effect outcome report\n\nThis turn completed only partially."
        )
    _insert_history_doc(
        user_id=user_id,
        session_id=session_id,
        history=[
            {
                "role": "assistant",
                "content": report,
                "llm_debug_data": {
                    "presenter_channels": {
                        "screen": report,
                        "spoken": unsafe_spoken,
                        "format": existing_format,
                    }
                },
            },
        ],
    )
    _authenticate(monkeypatch, user_id=user_id)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe canonical channels require deterministic repair")
        ),
    )

    response = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": session_id, "history_index": 0}},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["updated"] is True
    assert body["presenter_channels"]["format"] == "effect_outcome_report_v1"
    assert body["presenter_channels"]["screen"] == report
    assert body["presenter_channels"]["spoken"] != unsafe_spoken
    assert "## Effect outcome report" not in body["presenter_channels"]["spoken"]
    assert "Everything worked" not in body["presenter_channels"]["spoken"]


def test_canonical_backfill_leaves_safe_distinct_channels_unchanged(
    app_client, monkeypatch
):
    _, client = app_client

    user_id = f"#V#tester_{uuid.uuid4().hex}"
    session_id = f"sess-{uuid.uuid4()}"
    report = _canonical_report()
    existing_channels = {
        "screen": report,
        "spoken": "I couldn't confirm every result. The details are on screen.",
        "format": "effect_outcome_report_v1",
    }
    _insert_history_doc(
        user_id=user_id,
        session_id=session_id,
        history=[
            {
                "role": "assistant",
                "content": report,
                "llm_debug_data": {"presenter_channels": existing_channels},
            }
        ],
    )
    _authenticate(monkeypatch, user_id=user_id)
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("safe existing canonical narration must be reused")
        ),
    )

    response = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": session_id, "history_index": 0}},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "already_present"
    assert body["updated"] is False
    assert body["presenter_channels"] == existing_channels


def test_shared_viewer_gets_safe_canonical_response_without_owner_history_write(
    app_client, monkeypatch
):
    _, client = app_client

    viewer_id = f"#V#viewer_{uuid.uuid4().hex}"
    owner_id = f"#V#owner_{uuid.uuid4().hex}"
    session_id = f"sess-{uuid.uuid4()}"
    report = _canonical_report()
    original_channels = {
        "screen": report,
        "spoken": report,
        "format": "effect_outcome_report_v1",
    }
    _insert_history_doc(
        user_id=owner_id,
        session_id=session_id,
        history=[
            {
                "role": "assistant",
                "content": report,
                "llm_debug_data": {"presenter_channels": original_channels},
            }
        ],
    )
    _authenticate(monkeypatch, user_id=viewer_id)
    monkeypatch.setattr(
        "src.backend.services.chat_history_service.has_chat_history_session",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (
            owner_id,
            {
                "conversation_owner_user_id": owner_id,
                "invitee_user_concept_id": viewer_id,
                "status": "accepted",
            },
        ),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("shared canonical history must use deterministic narration")
        ),
    )

    response = client.post(
        "/von/history/backfill_spoken",
        json={"history_location": {"session_id": session_id, "history_index": 0}},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["updated"] is False
    assert body["matched"] is True
    assert body["persistence_attempted"] is False
    assert body["persistence_suppression_reason"] == (
        "shared_viewer_owner_history_write_not_authorised"
    )
    assert body["presenter_channels"]["spoken"] != report
    assert "Everything worked" not in body["presenter_channels"]["spoken"]

    from src.backend.db.mongo_client import get_db
    from src.backend.models.chat_history_model import chat_history_collection_name

    db = get_db()
    assert db is not None
    stored = db[chat_history_collection_name].find_one(
        {"user_id": owner_id, "session_id": session_id}
    )
    assert stored is not None
    assert stored["history"][0]["llm_debug_data"]["presenter_channels"] == (
        original_channels
    )
