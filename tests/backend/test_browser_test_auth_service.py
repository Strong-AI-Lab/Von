from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from flask import Flask

import src.backend.services.browser_test_auth_service as service


@dataclass
class _UpdateResult:
    matched_count: int = 1
    modified_count: int = 1


class _FakeConceptCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def find_one(self, query, projection=None):
        concept_id = query.get("concept_id")
        if concept_id:
            doc = self.docs.get(concept_id)
            if doc is None:
                return None
            return {
                "concept_id": doc["concept_id"],
                "concept_data": {
                    "read_by": list(doc.get("concept_data", {}).get("read_by", []))
                },
            }
        for doc in self.docs.values():
            metadata = ((doc.get("concept_data") or {}).get("metadata") or {})
            matches = True
            for key, value in query.items():
                if not isinstance(key, str) or not key.startswith("concept_data.metadata."):
                    continue
                metadata_key = key.split(".", 2)[2]
                if metadata.get(metadata_key) != value:
                    matches = False
                    break
            if matches:
                return {
                    "concept_id": doc["concept_id"],
                    "concept_data": {
                        "read_by": list(doc.get("concept_data", {}).get("read_by", []))
                    },
                }
        return None

    def update_one(self, query, update):
        concept_id = query["concept_id"]
        doc = self.docs[concept_id]
        for key, value in (update.get("$set") or {}).items():
            target = doc
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        return _UpdateResult()


class _FakeChatHistoryCollection:
    def __init__(self):
        self.docs: dict[tuple[str, str], dict] = {}

    def find_one(self, query, projection=None):
        return self.docs.get((query["user_id"], query["session_id"]))


def test_ensure_fixture_message_reuses_existing_message(monkeypatch):
    fake_coll = _FakeConceptCollection()
    create_calls: list[str] = []
    upsert_calls: list[str] = []
    relationship_calls: list[tuple[str, str, str]] = []

    def _fake_create_message(**kwargs):
        concept_id = f"#V#message_{len(create_calls) + 1}"
        create_calls.append(concept_id)
        fake_coll.docs[concept_id] = {
            "concept_id": concept_id,
            "concept_data": {
                "metadata": dict(kwargs["metadata"]),
                "read_by": [],
                "content_fallback": kwargs["content"],
            },
        }
        return {"concept_id": concept_id}

    monkeypatch.setattr(service, "get_concepts_collection", lambda: fake_coll)
    monkeypatch.setattr(service, "create_message", _fake_create_message)
    monkeypatch.setattr(
        service,
        "add_relationship",
        lambda *, source_id, predicate, target: relationship_calls.append(
            (source_id, predicate, target)
        ),
    )
    monkeypatch.setattr(
        service,
        "upsert_text_for_concept",
        lambda **kwargs: upsert_calls.append(kwargs["subject_concept_id"]),
    )

    spec = {
        "key": "workflow-review-request",
        "sender_id": "#V#browser_test_workflow_reviewer",
        "recipient_ids": ["#V#zhan_von_witbrock"],
        "subject": "Workflow preview needs a sanity check",
        "content": "Long browser-testing message content",
        "metadata": {
            "intent": "paper_recommendation",
            "delivery_channel": "paper_recommendation_message",
            "recommendation_assertion_ids": ["#V#assertion_1"],
        },
        "mark_unread_for": "#V#zhan_von_witbrock",
        "org_id": "#V#university_of_auckland_strong_ai_lab",
        "target_user_concept_id": "#V#zhan_von_witbrock",
    }

    first = service._ensure_fixture_message(spec=spec)
    fake_coll.docs[first["concept_id"]]["concept_data"]["read_by"] = ["#V#zhan_von_witbrock"]
    fake_coll.docs[first["concept_id"]]["concept_data"]["metadata"]["intent"] = "review_request"
    fake_coll.docs[first["concept_id"]]["concept_data"]["metadata"]["delivery_channel"] = (
        "interuser_message"
    )
    second = service._ensure_fixture_message(spec=spec)

    assert first["created"] is True
    assert second["created"] is False
    assert len(create_calls) == 1
    assert fake_coll.docs[first["concept_id"]]["concept_data"]["read_by"] == []
    assert (
        fake_coll.docs[first["concept_id"]]["concept_data"]["metadata"]["delivery_channel"]
        == "paper_recommendation_message"
    )
    assert (
        fake_coll.docs[first["concept_id"]]["concept_data"]["metadata"][
            "browser_test_target_user_concept_id"
        ]
        == "#V#zhan_von_witbrock"
    )
    assert upsert_calls == [first["concept_id"]]
    assert relationship_calls == [
        (
            "#V#assertion_1",
            service.PAPER_RECOMMENDATION_DELIVERED_VIA_MESSAGE_PREDICATE_ID,
            first["concept_id"],
        ),
        (
            "#V#assertion_1",
            service.PAPER_RECOMMENDATION_DELIVERED_VIA_MESSAGE_PREDICATE_ID,
            first["concept_id"],
        ),
    ]


def test_ensure_fixture_message_creates_new_message_for_different_target_user(monkeypatch):
    fake_coll = _FakeConceptCollection()
    create_calls: list[str] = []

    fake_coll.docs["#V#message_existing"] = {
        "concept_id": "#V#message_existing",
        "concept_data": {
            "metadata": {
                "browser_test_fixture_id": "browser_user_view.v1",
                "browser_test_message_key": "workflow-review-request",
                "browser_test_target_user_concept_id": "#V#zhan_von_witbrock",
                "browser_test_participant_signature": (
                    "#V#browser_test_workflow_reviewer|#V#zhan_von_witbrock"
                ),
            },
            "read_by": [],
            "content_fallback": "Old content",
        },
    }

    def _fake_create_message(**kwargs):
        concept_id = f"#V#message_{len(create_calls) + 1}"
        create_calls.append(concept_id)
        fake_coll.docs[concept_id] = {
            "concept_id": concept_id,
            "concept_data": {
                "metadata": dict(kwargs["metadata"]),
                "read_by": [],
                "content_fallback": kwargs["content"],
            },
        }
        return {"concept_id": concept_id}

    monkeypatch.setattr(service, "get_concepts_collection", lambda: fake_coll)
    monkeypatch.setattr(service, "create_message", _fake_create_message)
    monkeypatch.setattr(service, "upsert_text_for_concept", lambda **kwargs: None)
    monkeypatch.setattr(service, "add_relationship", lambda **kwargs: None)

    spec = {
        "key": "workflow-review-request",
        "sender_id": "#V#browser_test_workflow_reviewer",
        "recipient_ids": ["#V#codex_browser_fixture"],
        "subject": "Workflow preview needs a sanity check",
        "content": "Long browser-testing message content",
        "metadata": {"intent": "review_request", "delivery_channel": "interuser_message"},
        "mark_unread_for": "#V#codex_browser_fixture",
        "org_id": "#V#university_of_auckland_strong_ai_lab",
        "target_user_concept_id": "#V#codex_browser_fixture",
    }

    result = service._ensure_fixture_message(spec=spec)

    assert result["created"] is True
    assert create_calls == ["#V#message_1"]
    assert fake_coll.docs["#V#message_existing"]["concept_data"]["metadata"][
        "browser_test_target_user_concept_id"
    ] == "#V#zhan_von_witbrock"


def test_ensure_fixture_chat_session_skips_duplicate_history_entries(monkeypatch):
    fake_coll = _FakeChatHistoryCollection()
    added_entries: list[str] = []
    skip_rag_indexing_flags: list[bool | None] = []

    def _fake_create_chat_session(
        *,
        user_id,
        session_id,
        session_name=None,
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
    ):
        key = (user_id, session_id)
        fake_coll.docs.setdefault(
            key,
            {
                "user_id": user_id,
                "session_id": session_id,
                "session_name": session_name,
                "history": [],
                "namespace": namespace,
                "organisation_concept_id": organisation_concept_id,
                "role_in_org": role_in_org,
            },
        )
        return {"session_id": session_id, "session_name": session_name, "namespace": namespace}

    def _fake_add_message_to_history(
        *,
        user_id,
        session_id,
        message,
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
        llm_debug_data=None,
        broadcast_to_shared=None,
        exclude_user_from_broadcast=None,
        skip_rag_indexing=None,
    ):
        fake_coll.docs[(user_id, session_id)]["history"].append(dict(message))
        added_entries.append(message["fixture_entry_id"])
        skip_rag_indexing_flags.append(skip_rag_indexing)

    monkeypatch.setattr(
        service.chat_history_service,
        "create_chat_session",
        _fake_create_chat_session,
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_collection_service",
        lambda: fake_coll,
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "add_message_to_history",
        _fake_add_message_to_history,
    )

    spec = {
        "session_id": "browser-fixture-user-view-state",
        "session_name": "Authenticated user-view fixture sanity check",
        "history": (
            {
                "fixture_entry_id": "entry-1",
                "role": "user",
                "content": "First browser-test chat entry",
                "timestamp": datetime.now(timezone.utc),
            },
            {
                "fixture_entry_id": "entry-2",
                "role": "assistant",
                "content": "Second browser-test chat entry",
                "timestamp": datetime.now(timezone.utc),
            },
        ),
    }

    first = service._ensure_fixture_chat_session(
        user_concept_id="#V#zhan_von_witbrock",
        namespace="#V#zhan_von_witbrock@university_of_auckland_strong_ai_lab",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        role_in_org="member",
        spec=spec,
    )
    second = service._ensure_fixture_chat_session(
        user_concept_id="#V#zhan_von_witbrock",
        namespace="#V#zhan_von_witbrock@university_of_auckland_strong_ai_lab",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        role_in_org="member",
        spec=spec,
    )

    assert first["created_entries"] == 2
    assert second["created_entries"] == 0
    assert added_entries == ["entry-1", "entry-2"]
    assert skip_rag_indexing_flags == [True, True]


def test_login_browser_test_user_handles_existing_counterpart_name_variants(
    monkeypatch,
):
    app = Flask(__name__)
    app.secret_key = "browser-test-secret"

    config = service.BrowserTestAuthConfig(
        enabled=True,
        pseudouser_name="Zhan von Witbrock",
        pseudouser_email="zhanvonwitbrock@gmail.com",
        pseudouser_concept_id="#V#zhan_von_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
    )

    message_specs_seen: list[dict] = []

    def _fake_ensure_person_concept(*, concept_id, name, email):
        alias_by_concept = {
            "#V#zhan_von_witbrock": "Zhan von Witbrock",
            "#V#browser_test_workflow_reviewer": "Workflow Reviewer Existing Alias",
            "#V#browser_test_paper_scout": "Paper Scout Existing Alias",
        }
        return {
            "concept_id": concept_id,
            "direct_concept_name": alias_by_concept[concept_id],
        }

    monkeypatch.setattr(service, "get_browser_test_auth_config", lambda: config)
    monkeypatch.setattr(
        service,
        "_ensure_org_exists",
        lambda organisation_concept_id: {
            "concept_id": organisation_concept_id,
            "direct_concept_name": "University Of Auckland Strong AI Lab",
        },
    )
    monkeypatch.setattr(service, "_ensure_person_concept", _fake_ensure_person_concept)
    monkeypatch.setattr(service, "_ensure_org_membership", lambda **kwargs: None)
    monkeypatch.setattr(
        service,
        "derive_namespace",
        lambda user_slug, org_slug: f"#V#{user_slug}@{org_slug}",
    )
    monkeypatch.setattr(service, "set_window_organisation", lambda **kwargs: None)
    monkeypatch.setattr(service, "set_window_chat_session", lambda **kwargs: None)
    monkeypatch.setattr(
        service,
        "_ensure_fixture_message",
        lambda *, spec: (
            message_specs_seen.append(dict(spec))
            or {"created": False, "concept_id": f"#V#message_{len(message_specs_seen)}"}
        ),
    )
    monkeypatch.setattr(
        service,
        "_paper_recommendation_fixture_spec",
        lambda *, pseudouser_concept_id, organisation_concept_id: {
            "key": "paper-recommendation",
            "sender_id": "#V#von_system",
            "recipient_ids": [pseudouser_concept_id],
            "subject": "New paper recommendation from Von",
            "content": "Recommendation fixture content",
            "metadata": {
                "delivery_channel": "paper_recommendation_message",
                "recommendation_assertion_ids": ["#V#assertion_browser_fixture"],
            },
            "mark_unread_for": pseudouser_concept_id,
            "org_id": organisation_concept_id,
            "target_user_concept_id": pseudouser_concept_id,
        },
    )
    monkeypatch.setattr(
        service,
        "_ensure_fixture_chat_session",
        lambda **kwargs: {
            "session_id": "browser-fixture-user-view-state",
            "created_entries": 0,
        },
    )

    with app.test_request_context("/von/api/auth/browser-test-login"):
        result = service.login_browser_test_user(window_session_id="ws_fixture")

    assert len(message_specs_seen) == 4
    sender_ids = {item["sender_id"] for item in message_specs_seen}
    assert "#V#browser_test_workflow_reviewer" in sender_ids
    assert "#V#browser_test_paper_scout" in sender_ids
    recommendation_specs = [
        item
        for item in message_specs_seen
        if item.get("metadata", {}).get("delivery_channel")
        == "paper_recommendation_message"
    ]
    assert len(recommendation_specs) == 1
    assert recommendation_specs[0]["target_user_concept_id"] == "#V#zhan_von_witbrock"
    assert result["fixture"]["counterparts"][0]["name"] == "Workflow Reviewer Existing Alias"
    assert result["fixture"]["counterparts"][1]["name"] == "Paper Scout Existing Alias"


def test_describe_browser_test_mode_surfaces_disabled_setup_hint_and_default_identity(
    monkeypatch,
):
    for key in (
        "VON_BROWSER_TEST_AUTH_ENABLED",
        "VON_BROWSER_TEST_PSEUDOUSER_NAME",
        "VON_BROWSER_TEST_PSEUDOUSER_EMAIL",
        "VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID",
        "VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(
        service,
        "browser_test_auth_allowed_for_request",
        lambda: (False, "Browser-test auth is disabled"),
    )

    payload = service.describe_browser_test_mode()

    assert payload["configured"] is False
    assert payload["available"] is False
    assert payload["status"] == "disabled"
    assert payload["status_label"] == "Disabled"
    assert payload["identity_source"] == "default"
    assert payload["uses_default_identity"] is True
    assert payload["email"] == "zhanvonwitbrock@gmail.com"
    assert "VON_BROWSER_TEST_AUTH_ENABLED=1" in payload["setup_hint"]
    assert payload["requires_restart"] is True


def test_describe_browser_test_mode_surfaces_configured_identity_and_availability(
    monkeypatch,
):
    monkeypatch.setenv("VON_BROWSER_TEST_AUTH_ENABLED", "1")
    monkeypatch.setenv("VON_BROWSER_TEST_PSEUDOUSER_NAME", "Codex Browser Test")
    monkeypatch.setenv(
        "VON_BROWSER_TEST_PSEUDOUSER_EMAIL",
        "codex-browser-fixture@strongailab.invalid",
    )
    monkeypatch.setenv(
        "VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID",
        "#V#codex_browser_fixture",
    )
    monkeypatch.setenv(
        "VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID",
        "#V#university_of_auckland_strong_ai_lab",
    )
    monkeypatch.setattr(
        service,
        "browser_test_auth_allowed_for_request",
        lambda: (True, None),
    )

    payload = service.describe_browser_test_mode()

    assert payload["configured"] is True
    assert payload["available"] is True
    assert payload["status"] == "available"
    assert payload["status_label"] == "Available"
    assert payload["identity_source"] == "env_configured"
    assert payload["uses_default_identity"] is False
    assert payload["display_name"] == "Codex Browser Test"
    assert payload["email"] == "codex-browser-fixture@strongailab.invalid"
    assert payload["identity_label"] == "Codex Browser Test <codex-browser-fixture@strongailab.invalid>"
    assert "Browser Test Login" in payload["setup_hint"]
    assert payload["enabled_env_present"] is True
