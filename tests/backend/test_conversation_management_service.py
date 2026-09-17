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
        "get_chat_history_session_summaries_page",
        lambda *args, **kwargs: {
            "sessions": [
                {
                    "session_id": "owned-1",
                    "session_name": "Owned",
                    "last_message_at": "2026-08-17T10:00:00+00:00",
                    "namespace": "#V#user@org",
                    "organisation_concept_id": "#V#org",
                }
            ],
            "has_more": False,
            "next_position": None,
        },
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_sessions",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_page",
        lambda **kwargs: {
            "available": True,
            "invites": [
                {
                    "invite_id": "invite-1",
                    "session_id": "shared-1",
                    "conversation_owner_user_id": "#V#owner",
                    "inviter_user_id": "#V#owner",
                    "organisation_concept_id": "#V#org",
                }
            ],
            "has_more": False,
            "next_position": None,
        },
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
        "get_chat_history_session_summaries_for_owner_sessions",
        lambda *args, **kwargs: {
            ("#V#owner", "shared-1"): {
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
        "get_chat_history_session_summaries_page",
        lambda *args, **kwargs: {
            "sessions": [
                {
                    "session_id": "shared-1",
                    "session_name": "Local join stub",
                    "namespace": "#V#invitee@org",
                }
            ],
            "has_more": False,
            "next_position": None,
        },
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_sessions",
        lambda **kwargs: [
            {"session_id": "shared-1", "status": "accepted"}
        ],
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_page",
        lambda **kwargs: {
            "available": True,
            "invites": [
                {
                    "invite_id": "invite-1",
                    "session_id": "shared-1",
                    "conversation_owner_user_id": "#V#owner",
                    "inviter_user_id": "#V#owner",
                    "organisation_concept_id": "#V#org",
                }
            ],
            "has_more": False,
            "next_position": None,
        },
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
        "get_chat_history_session_summaries_for_owner_sessions",
        lambda *args, **kwargs: {
            ("#V#owner", "shared-1"): {
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
        "get_chat_history_session_summaries_page",
        lambda *args, **kwargs: {
            "sessions": [],
            "has_more": False,
            "next_position": None,
        },
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_sessions",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_page",
        lambda **kwargs: {
            "available": True,
            "invites": [
                {
                    "invite_id": "invite-1",
                    "session_id": "shared-1",
                    "conversation_owner_user_id": "#V#owner",
                    "organisation_concept_id": "#V#former_org",
                }
            ],
            "has_more": False,
            "next_position": None,
        },
    )
    monkeypatch.setattr(
        service, "get_user_memberships", lambda _actor: {"memberships": []}
    )
    summary_calls: list[str] = []
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_for_owner_sessions",
        lambda *args, **kwargs: summary_calls.append(args[0]),
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
    ordered_sessions = sorted(
        sessions, key=lambda row: row["session_id"], reverse=True
    )

    def _owned_page(*_args, **kwargs):
        position = kwargs.get("position")
        start = 0
        if position:
            start = next(
                index + 1
                for index, row in enumerate(ordered_sessions)
                if row["session_id"] == position["session_id"]
            )
        size = kwargs["page_size"]
        rows = ordered_sessions[start : start + size]
        has_more = start + size < len(ordered_sessions)
        return {
            "sessions": rows,
            "has_more": has_more,
            "next_position": (
                {"timestamp": rows[-1]["last_message_at"], "session_id": rows[-1]["session_id"]}
                if rows
                else None
            ),
        }

    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_page",
        _owned_page,
    )
    monkeypatch.setattr(
        service, "list_accepted_invites_for_user_sessions", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_page",
        lambda **_kwargs: {
            "available": True,
            "invites": [],
            "has_more": False,
            "next_position": None,
        },
    )
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


def test_list_final_coverage_preserves_prior_page_source_failure(monkeypatch):
    from src.backend.services import conversation_management_service as service

    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: _PreferenceCollection()
    )
    calls = {"owned": 0, "invite_lookup": 0}

    def _owned_page(*_args, **kwargs):
        calls["owned"] += 1
        if kwargs.get("position") is None:
            return {
                "sessions": [
                    {
                        "session_id": "owned-2",
                        "session_name": None,
                        "last_message_at": "2026-09-02T12:00:00+00:00",
                    }
                ],
                "has_more": True,
                "next_position": {
                    "timestamp": "2026-09-02T12:00:00+00:00",
                    "session_id": "owned-2",
                },
            }
        return {
            "sessions": [
                {
                    "session_id": "owned-1",
                    "session_name": None,
                    "last_message_at": "2026-09-01T12:00:00+00:00",
                }
            ],
            "has_more": False,
            "next_position": None,
        }

    def _accepted_for_sessions(**_kwargs):
        calls["invite_lookup"] += 1
        if calls["invite_lookup"] == 1:
            raise RuntimeError("invite store temporarily unavailable")
        return []

    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_page",
        _owned_page,
    )
    monkeypatch.setattr(
        service, "list_accepted_invites_for_user_sessions", _accepted_for_sessions
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_page",
        lambda **_kwargs: {
            "available": True,
            "invites": [],
            "has_more": False,
            "next_position": None,
        },
    )
    monkeypatch.setattr(
        service, "get_user_memberships", lambda _actor: {"memberships": []}
    )

    first = service.list_actor_conversations(
        actor_user_id="#V#alice", limit=1
    )
    final = service.list_actor_conversations(
        actor_user_id="#V#alice", limit=1, cursor=first["next_cursor"]
    )
    assert final["next_cursor"] is not None
    final = service.list_actor_conversations(
        actor_user_id="#V#alice", limit=1, cursor=final["next_cursor"]
    )

    assert calls == {"owned": 2, "invite_lookup": 2}
    assert final["next_cursor"] is None
    assert final["coverage_complete"] is False
    assert final["coverage"]["accepted_invites_available"] is False
    assert "prior_page_coverage_incomplete" in final["warnings"]


def test_list_exhausts_owned_and_shared_streams_with_equal_timestamps_and_stub(
    monkeypatch,
):
    from src.backend.services import conversation_management_service as service

    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: _PreferenceCollection()
    )
    equal_timestamp = "2026-09-02T12:00:00+00:00"
    owned = [
        {
            "session_id": f"owned-{index:03d}",
            "session_name": None,
            "last_message_at": equal_timestamp,
            "namespace": "#V#alice@org",
        }
        for index in range(125)
    ]
    owned.append(
        {
            "session_id": "shared-000",
            "session_name": "local join stub",
            "last_message_at": equal_timestamp,
            "namespace": "#V#alice@org",
        }
    )
    owned.sort(key=lambda row: row["session_id"], reverse=True)
    invites = [
        {
            "invite_id": f"invite-{index:03d}",
            "session_id": f"shared-{index:03d}",
            "conversation_owner_user_id": "#V#owner",
            "inviter_user_id": "#V#owner",
            "organisation_concept_id": "#V#org",
            "updated_at": equal_timestamp,
        }
        for index in range(151)
    ]
    invites.sort(key=lambda row: row["invite_id"], reverse=True)

    def _page(rows, *, page_size, position, id_field):
        start = 0
        if position:
            start = next(
                index + 1
                for index, row in enumerate(rows)
                if row[id_field] == position[id_field]
            )
        page_rows = rows[start : start + page_size]
        has_more = start + page_size < len(rows)
        return page_rows, has_more

    def _owned_page(*_args, **kwargs):
        rows, has_more = _page(
            owned,
            page_size=kwargs["page_size"],
            position=kwargs.get("position"),
            id_field="session_id",
        )
        return {
            "sessions": rows,
            "has_more": has_more,
            "next_position": (
                {"timestamp": equal_timestamp, "session_id": rows[-1]["session_id"]}
                if rows
                else None
            ),
        }

    def _invite_page(**kwargs):
        rows, has_more = _page(
            invites,
            page_size=kwargs["page_size"],
            position=kwargs.get("position"),
            id_field="invite_id",
        )
        return {
            "available": True,
            "invites": rows,
            "has_more": has_more,
            "next_position": (
                {"timestamp": equal_timestamp, "invite_id": rows[-1]["invite_id"]}
                if rows
                else None
            ),
        }

    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_page",
        _owned_page,
    )
    monkeypatch.setattr(
        service,
        "list_accepted_invites_for_user_sessions",
        lambda **kwargs: (
            [{"session_id": "shared-000"}]
            if "shared-000" in kwargs["session_ids"]
            else []
        ),
    )
    monkeypatch.setattr(service, "list_accepted_invites_for_user_page", _invite_page)
    monkeypatch.setattr(
        service,
        "get_user_memberships",
        lambda _actor: {"memberships": [{"organisation_concept_id": "#V#org"}]},
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "resolve_chat_history_namespace",
        lambda _owner: "#V#owner@org",
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_session_summaries_for_owner_sessions",
        lambda pairs, **_kwargs: {
            (owner, session): {
                "session_id": session,
                "session_name": None,
                "last_message_at": equal_timestamp,
                "namespace": "#V#owner@org",
            }
            for owner, session in pairs
        },
    )

    seen = []
    cursor = None
    coverage_complete = False
    for _ in range(10):
        page = service.list_actor_conversations(
            actor_user_id="#V#alice",
            namespace="#V#alice@org",
            organisation_concept_id="#V#org",
            limit=100,
            cursor=cursor,
            include_hidden=True,
            name_present=False,
        )
        seen.extend(row["session_id"] for row in page["conversations"])
        cursor = page["next_cursor"]
        coverage_complete = page["coverage_complete"]
        if cursor is None:
            break

    assert len(seen) == 276
    assert len(set(seen)) == 276
    assert seen.count("shared-000") == 1
    assert coverage_complete is True
    assert cursor is None
