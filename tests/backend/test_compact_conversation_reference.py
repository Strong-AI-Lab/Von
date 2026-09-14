"""Exercise compact references through browser and canonical agent read paths."""

import json

import pytest
from flask import Flask

from src.backend.integrations.internal_mcp import catalogue
from src.backend.server.routes import von_routes
from src.backend.services import conversation_concept_service as concepts
from src.backend.services import chat_history_service as history
from src.backend.services import shared_conversation_service as sharing
from src.backend.services.conversation_reference_service import (
    build_turn_reference,
    split_conversation_reference,
)

PARENT = "#V#conversation_0123456789ab"


@pytest.fixture
def source(monkeypatch):
    state = {
        "title": "Original title",
        "owner": "#V#alice",
        "invite": None,
        "messages": [
            {"role": "user", "content": "legacy"},
            {
                "role": "assistant",
                "content": "Exact private answer",
                "turn_id": "a-stored",
            },
        ],
    }
    monkeypatch.setattr(
        concepts,
        "get_conversation_source_locator_for_authorisation",
        lambda cid: "source-session" if cid == PARENT else None,
    )
    monkeypatch.setattr(
        concepts,
        "get_source_conversation_identity_for_authorisation",
        lambda sid: PARENT if sid == "source-session" else None,
    )
    monkeypatch.setattr(
        concepts, "get_or_create_conversation_concept", lambda *a, **kw: PARENT
    )
    monkeypatch.setattr(
        sharing, "resolve_conversation_owner", lambda **kw: state["owner"]
    )
    monkeypatch.setattr(
        sharing, "get_accepted_invite_for_user_session", lambda **kw: state["invite"]
    )
    monkeypatch.setattr(catalogue, "_is_user_member_of_organisation", lambda **kw: True)
    monkeypatch.setattr(
        history,
        "has_chat_history_session",
        lambda actor, sid, **kw: actor == state["owner"] and sid == "source-session",
    )
    monkeypatch.setattr(
        history, "resolve_chat_history_namespace", lambda actor: actor + "@org"
    )

    def position(**kw):
        positions = [
            i
            for i, m in enumerate(state["messages"])
            if m.get("turn_id") == kw["turn_id"]
        ]
        return positions[0] if len(positions) == 1 else None

    monkeypatch.setattr(history, "find_chat_history_turn_position", position)

    def page(**kw):
        assert kw["user_id"] == state["owner"]
        assert kw["session_id"] == "source-session"
        start, size = kw["offset"], kw["page_size"]
        messages = state["messages"][start : start + size]
        end = start + len(messages)
        return {
            "found": True,
            "session_name": state["title"],
            "messages": messages,
            "message_count": len(state["messages"]),
            "page_count": len(messages),
            "offset": start,
            "has_more": end < len(state["messages"]),
            "next_offset": end if end < len(state["messages"]) else None,
            "coverage_complete": end >= len(state["messages"]),
        }

    monkeypatch.setattr(history, "get_chat_history_transcript_page", page)
    monkeypatch.setattr(
        catalogue, "_bounded_delegated_telemetry_payload", lambda payload, **kw: payload
    )
    return state


def read(reference, actor="#V#alice", **kwargs):
    return catalogue._conversation_get(
        conversation_ref=reference,
        acting_user_concept_id=actor,
        namespace=actor + "@org",
        organisation_concept_id="#V#org",
        **kwargs,
    )


def test_browser_copy_agent_read_rename_and_reordered_source(monkeypatch, source):
    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *a: {"namespace": "#V#alice@org", "organisation_id": "#V#org"},
    )
    app = Flask(__name__)
    app.secret_key = "fixture-only"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_concept_id"] = "#V#alice"
        response = client.post(
            "/von/api/session/conversation_reference",
            json={"session_id": "source-session", "turn_id": "a-stored"},
        )
        assert response.status_code == 200
        reference = response.json["concept_reference"]
        assert split_conversation_reference(reference) == (PARENT, "a-stored")
        first = read(reference)
        assert first["messages"][-1]["content"] == "Exact private answer"
        source["title"] = "Renamed"
        source["messages"].insert(0, {"role": "user", "content": "Inserted before"})
        second = read(reference)
        assert second["session_name"] == "Renamed"
        assert second["open_action"]["session_id"] == "source-session"
        assert second["turn_id"] == "a-stored"
        assert second["messages"][-1]["content"] == "Exact private answer"
        anonymous = app.test_client().post(
            "/von/api/session/conversation_reference",
            json={"conversation_ref": reference},
        )
        assert anonymous.status_code == 401


def test_denied_actor_and_revoked_invite_get_no_private_metadata(source):
    reference = build_turn_reference(PARENT, "a-stored")
    denied = read(reference, "#V#bob")
    assert denied["success"] is False
    assert "Original title" not in json.dumps(denied)
    assert "Exact private answer" not in json.dumps(denied)
    source["invite"] = {"organisation_concept_id": "#V#org"}
    allowed = read(reference, "#V#bob")
    assert allowed["success"] is True
    assert allowed["messages"][-1]["turn_id"] == "a-stored"
    source["invite"] = None
    assert read(reference, "#V#bob")["success"] is False


def test_conflicting_source_missing_and_duplicate_turn_are_honest(source):
    reference = build_turn_reference(PARENT, "a-stored")
    assert read(reference, session_id="wrong-source")["success"] is False
    assert read(build_turn_reference(PARENT, "not-stored"))["success"] is False
    source["messages"].append(source["messages"][-1].copy())
    assert read(reference)["success"] is False
    assert read(PARENT)["success"] is True


@pytest.mark.parametrize(
    "value",
    [
        "#V#conversation_x_turn_z",
        "#V#conversation_x_turn_ff",
        "#V#conversation_x_turn_20",
    ],
)
def test_malformed_turn_reference(value):
    with pytest.raises(ValueError):
        split_conversation_reference(value)


def test_bounded_context_continuation_and_legacy_json(source, monkeypatch):
    from src.backend.services import opaque_cursor_service

    tokens = {}

    def encode(**kw):
        token = str(len(tokens))
        tokens[token] = kw["payload"]
        return token

    monkeypatch.setattr(opaque_cursor_service, "encode_opaque_cursor", encode)
    monkeypatch.setattr(
        opaque_cursor_service, "decode_opaque_cursor", lambda **kw: tokens[kw["cursor"]]
    )
    source["messages"].extend(
        {"turn_id": f"a-{i}", "role": "assistant", "content": f"message {i}"}
        for i in range(20)
    )
    reference = build_turn_reference(PARENT, "a-stored")
    first = read(reference, page_size=4)
    assert len(first["messages"]) == 4
    assert first["has_more"] is True
    next_page = read(reference, page_size=4, cursor=first["next_cursor"])
    assert next_page["transcript_offset"] == 4
    assert len(next_page["messages"]) == 4
    assert read(PARENT, page_size=4, cursor=first["next_cursor"])["success"] is False
    legacy = catalogue._conversation_transcript_page(
        conversation_ref={
            "schema_version": "conversation_reference.v1",
            "binding_kind": "explicit_session_id",
            "session_id": "source-session",
            "user_concept_id": "#V#alice",
            "namespace": "#V#alice@org",
            "organisation_concept_id": "#V#org",
        },
        acting_user_concept_id="#V#alice",
        namespace="#V#alice@org",
        organisation_concept_id="#V#org",
        page_size=4,
    )
    assert legacy["success"] is True
    assert legacy["messages"] == first["messages"]


def test_source_change_between_anchor_and_page_is_retryable(source, monkeypatch):
    monkeypatch.setattr(history, "find_chat_history_turn_position", lambda **kw: 100)
    result = read(build_turn_reference(PARENT, "a-stored"))
    assert result["success"] is False
    assert result["error_code"] == "conversation_turn_changed"


def test_named_reference_metadata_matches_viewer_state_and_preserves_source(
    monkeypatch, source
):
    from src.backend.services import conversation_management_service as management

    monkeypatch.setattr(
        von_routes,
        "get_effective_context",
        lambda *a: {"namespace": "#V#alice@org", "organisation_id": "#V#org"},
    )
    preference = {"session_name_override": "My conversation name"}
    monkeypatch.setattr(
        management,
        "get_conversation_preferences",
        lambda **kw: {"source-session": preference},
    )
    app = Flask(__name__)
    app.secret_key = "fixture-only"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user_concept_id"] = "#V#alice"

        def metadata():
            response = client.post(
                "/von/api/session/conversation_reference",
                json={"conversation_ref": PARENT, "metadata_only": True},
            )
            assert response.status_code == 200
            return response.json

        result = metadata()
        assert result["session_name"] == "My conversation name"
        assert result["canonical_session_name"] == "Original title"
        assert result["session_name_source"] == "actor_preference"
        assert "messages" not in result
        preference.clear()
        source["title"] = "Renamed canonical title"
        result = metadata()
        assert (
            result["session_name"]
            == result["canonical_session_name"]
            == "Renamed canonical title"
        )
        assert result["session_name_source"] == "chat_history"
