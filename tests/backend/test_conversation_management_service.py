from __future__ import annotations

import types
from datetime import UTC, datetime


class _PreferenceCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query, projection=None):
        return [dict(doc) for doc in self.docs.values() if self._matches(doc, query)]

    def find_one(self, query, projection=None):
        return next(
            (dict(doc) for doc in self.docs.values() if self._matches(doc, query)),
            None,
        )

    def update_one(self, query, update, upsert=False):
        setting_name = query["setting_name"]
        existing = self.docs.get(setting_name)
        created = existing is None
        doc = existing or dict(query)
        for key, value in update.get("$set", {}).items():
            doc[key] = value
        self.docs[setting_name] = doc
        return types.SimpleNamespace(
            acknowledged=True,
            matched_count=0 if created else 1,
            modified_count=1,
            upserted_id=setting_name if created else None,
        )


def test_preference_effect_is_idempotent_and_has_canonical_read_back(monkeypatch):
    from src.backend.services import conversation_management_service as service

    collection = _PreferenceCollection()
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: collection
    )

    first = service.set_conversation_preference(
        actor_user_id="#V#user",
        session_id="session-1",
        hidden=True,
    )
    second = service.set_conversation_preference(
        actor_user_id="#V#user",
        session_id="session-1",
        hidden=True,
    )

    assert first["changed"] is True
    assert first["preference"]["hidden"] is True
    assert first["preference"]["preference_present"] is True
    assert second["changed"] is False
    assert second["preference"]["hidden"] is True


def test_actor_preferences_are_isolated_and_override_shared_display_name(monkeypatch):
    from src.backend.services import conversation_management_service as service

    collection = _PreferenceCollection()
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: collection
    )
    service.set_conversation_preference(
        actor_user_id="#V#alice",
        session_id="shared-1",
        pinned=True,
        session_name_override="Alice's project label",
        update_session_name_override=True,
    )

    alice_rows = service.apply_conversation_preferences(
        actor_user_id="#V#alice",
        conversations=[{"session_id": "shared-1", "session_name": "Owner name"}],
    )
    bob_rows = service.apply_conversation_preferences(
        actor_user_id="#V#bob",
        conversations=[{"session_id": "shared-1", "session_name": "Owner name"}],
    )

    assert alice_rows[0]["session_name"] == "Alice's project label"
    assert alice_rows[0]["pinned"] is True
    assert bob_rows[0]["session_name"] == "Owner name"
    assert bob_rows[0]["pinned"] is False
    assert bob_rows[0]["conversation_preference"]["preference_present"] is False


def test_list_actor_conversations_combines_shared_and_filters_hidden(monkeypatch):
    from src.backend.services import conversation_management_service as service

    collection = _PreferenceCollection()
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: collection
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_result",
        lambda *args, **kwargs: {
            "sessions": [
                {
                    "session_id": "owned-1",
                    "session_name": "Owned",
                    "last_message_at": "2026-08-17T10:00:00+00:00",
                    "namespace": "#V#user@org",
                    "organisation_concept_id": "#V#org",
                }
            ]
        },
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user",
        lambda **kwargs: [
            {
                "invite_id": "invite-1",
                "session_id": "shared-1",
                "conversation_owner_user_id": "#V#owner",
                "inviter_user_id": "#V#owner",
                "organisation_concept_id": "#V#org",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "get_user_memberships",
        lambda _actor: {"memberships": [{"organisation_concept_id": "#V#org"}]},
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "resolve_chat_history_namespace",
        lambda user_id: f"{user_id}@org",
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_by_ids",
        lambda *args, **kwargs: {
            "shared-1": {
                "session_id": "shared-1",
                "session_name": "Shared",
                "last_message_at": "2026-08-17T11:00:00+00:00",
                "namespace": "#V#owner@org",
                "organisation_concept_id": "#V#org",
            }
        },
    )
    service.set_conversation_preference(
        actor_user_id="#V#user", session_id="owned-1", hidden=True
    )

    visible = service.list_actor_conversations(
        actor_user_id="#V#user",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        include_hidden=False,
    )
    all_rows = service.list_actor_conversations(
        actor_user_id="#V#user",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        include_hidden=True,
    )

    assert [row["session_id"] for row in visible["conversations"]] == ["shared-1"]
    assert all_rows["hidden_count"] == 1
    assert {row["session_id"] for row in all_rows["conversations"]} == {
        "owned-1",
        "shared-1",
    }
    shared = next(
        row for row in all_rows["conversations"] if row["session_id"] == "shared-1"
    )
    assert shared["shared_with_me"] is True
    assert (
        shared["conversation_ref"]["conversation_ref"]["user_concept_id"] == "#V#owner"
    )
    assert (
        shared["conversation_ref"]["conversation_ref"]["organisation_concept_id"]
        == "#V#org"
    )
    assert shared["conversation_reference"]["schema_version"] == (
        "conversation_reference.v1"
    )
    assert shared["conversation_reference"]["binding_kind"] == "explicit_session_id"
    assert shared["access_mode"] == "invitee"
    assert "trash" not in shared["available_actions"]
    owned = next(
        row for row in all_rows["conversations"] if row["session_id"] == "owned-1"
    )
    assert "trash" in owned["available_actions"]


def test_list_prefers_shared_owner_summary_over_local_join_stub(monkeypatch):
    from src.backend.services import conversation_management_service as service

    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: _PreferenceCollection()
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_result",
        lambda *args, **kwargs: {
            "sessions": [
                {
                    "session_id": "shared-1",
                    "session_name": "Local join stub",
                    "namespace": "#V#invitee@org",
                }
            ]
        },
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user",
        lambda **kwargs: [
            {
                "session_id": "shared-1",
                "conversation_owner_user_id": "#V#owner",
                "inviter_user_id": "#V#owner",
                "organisation_concept_id": "#V#org",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "get_user_memberships",
        lambda _actor: {"memberships": [{"organisation_concept_id": "#V#org"}]},
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "resolve_chat_history_namespace",
        lambda user_id: f"{user_id}@org",
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_by_ids",
        lambda *args, **kwargs: {
            "shared-1": {
                "session_id": "shared-1",
                "session_name": "Owner summary",
                "namespace": "#V#owner@org",
            }
        },
    )

    result = service.list_actor_conversations(
        actor_user_id="#V#invitee",
        namespace="#V#invitee@org",
        organisation_concept_id="#V#org",
    )

    assert len(result["conversations"]) == 1
    row = result["conversations"][0]
    assert row["session_name"] == "Owner summary"
    assert row["shared_with_me"] is True
    assert row["conversation_ref"]["conversation_ref"]["user_concept_id"] == "#V#owner"


def test_list_excludes_shared_conversation_when_org_membership_is_absent(monkeypatch):
    from src.backend.services import conversation_management_service as service

    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: _PreferenceCollection()
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_result",
        lambda *args, **kwargs: {"sessions": []},
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user",
        lambda **kwargs: [
            {
                "session_id": "shared-1",
                "conversation_owner_user_id": "#V#owner",
                "organisation_concept_id": "#V#former_org",
            }
        ],
    )
    monkeypatch.setattr(
        service, "get_user_memberships", lambda _actor: {"memberships": []}
    )
    summary_calls: list[str] = []
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_by_ids",
        lambda *args, **kwargs: summary_calls.append(args[1]),
    )

    result = service.list_actor_conversations(
        actor_user_id="#V#invitee",
        namespace="#V#invitee@org",
        include_hidden=True,
    )

    assert result["conversations"] == []
    assert summary_calls == []
    assert "shared_conversation_membership_required" in result["warnings"]


def test_preference_projection_serialises_datetime(monkeypatch):
    from src.backend.services import conversation_management_service as service

    collection = _PreferenceCollection()
    setting_name = service._preference_setting_name("#V#user", "session-1")
    collection.docs[setting_name] = {
        "setting_name": setting_name,
        "actor_user_id": "#V#user",
        "session_id": "session-1",
        "preference_kind": service.PREFERENCE_SCHEMA_VERSION,
        "value": {"hidden": False, "pinned": True},
        "updated_at": datetime(2026, 8, 17, tzinfo=UTC),
    }
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: collection
    )

    preference = service.get_conversation_preferences(
        actor_user_id="#V#user", session_ids=["session-1"]
    )["session-1"]

    assert preference["updated_at"] == "2026-08-17T00:00:00+00:00"


def test_list_cursor_pages_more_than_250_without_duplicates(monkeypatch):
    from src.backend.services import conversation_management_service as service

    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: _PreferenceCollection()
    )
    sessions = [
        {
            "session_id": f"session-{index:03d}",
            "session_name": f"Conversation {index}",
            "last_message_at": f"2026-08-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
            "namespace": "#V#alice@org",
            "trashed": False,
        }
        for index in range(275)
    ]
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_result",
        lambda *args, **kwargs: {"sessions": sessions},
    )
    monkeypatch.setattr(service, "list_accepted_invites_for_user", lambda **_kwargs: [])
    monkeypatch.setattr(
        service, "get_user_memberships", lambda _actor: {"memberships": []}
    )

    seen: list[str] = []
    cursor = None
    for _ in range(3):
        page = service.list_actor_conversations(
            actor_user_id="#V#alice",
            namespace="#V#alice@org",
            limit=100,
            cursor=cursor,
        )
        seen.extend(row["session_id"] for row in page["conversations"])
        cursor = page["next_cursor"]

    assert len(seen) == 275
    assert len(set(seen)) == 275
    assert cursor is None
