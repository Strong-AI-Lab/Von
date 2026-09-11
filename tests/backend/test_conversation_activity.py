from datetime import UTC, datetime

import pytest
from flask import Flask

from src.backend.security import access_control
from src.backend.server.routes import von_routes
from src.backend.services import chat_history_service as chats
from src.backend.services import conversation_read_service as reads

STAMP = datetime(2026, 9, 11, 19, 1, 15, 659000, tzinfo=UTC)


def test_history_keeps_precise_observed_timestamp():
    segments = chats._split_history_into_segments_with_locations(
        [
            {
                "role": "assistant",
                "content": "Answer",
                "timestamp": STAMP,
                "turn_id": "turn-1",
            }
        ],
        session_id="session-1",
    )
    assert segments[0][0]["contribution_timestamp"] == STAMP.isoformat()
    assert segments[0][0]["timestamp"] == STAMP


@pytest.mark.parametrize(
    "message,visible,incoming",
    [
        ({"role": "assistant", "content": "Answer"}, True, True),
        (
            {"role": "user", "content": "Question", "author_user_id": "#V#alice"},
            True,
            False,
        ),
        (
            {
                "role": "user",
                "content": "Shared contribution",
                "author_user_id": "#V#bob",
            },
            True,
            True,
        ),
        (
            {"role": "user", "image_attachments": [{"concept_id": "#V#image"}]},
            True,
            False,
        ),
        ({"role": "tool", "content": "Heartbeat"}, False, False),
        ({"role": "assistant", "content": ""}, False, False),
    ],
)
def test_only_visible_contributions_change_activity(message, visible, incoming):
    fields = chats._conversation_activity_fields(message, "#V#alice", STAMP)
    assert ("last_contribution_at" in fields) == visible
    assert ("last_incoming_contribution_at" in fields) == incoming


@pytest.mark.parametrize(
    "observed,turn_id,status",
    [
        (STAMP.isoformat(), None, 200),
        ("Fri, 11 Sep 2026 19:01:15 GMT", "turn-1", 200),
        ("2026-09-11T19:01:15Z", "unseen-turn", 409),
        ("2099-01-01T00:00:00Z", None, 409),
    ],
)
def test_read_receipt_requires_an_exact_observed_contribution(
    monkeypatch, observed, turn_id, status
):
    monkeypatch.setattr(
        access_control, "get_effective_user_concept_id", lambda: "#V#alice"
    )
    monkeypatch.setattr(
        von_routes, "get_effective_context", lambda *a, **k: {"namespace": "alice@lab"}
    )

    def summary(actor, sid, **kwargs):
        assert actor == "#V#alice" and kwargs["namespace"] == "alice@lab"
        return {
            "last_incoming_contribution_at": STAMP.isoformat(),
            "last_incoming_timestamp": STAMP.isoformat(),
            "last_incoming_turn_id": "turn-1",
        }

    monkeypatch.setattr(chats, "get_chat_history_session_summary", summary)
    acknowledged = []
    monkeypatch.setattr(
        reads,
        "acknowledge",
        lambda *args: acknowledged.append(args) or STAMP.isoformat(),
    )
    app = Flask(__name__)
    app.secret_key = "activity-test-only"
    app.register_blueprint(von_routes.von_bp, url_prefix="/von")
    response = app.test_client().post(
        "/von/history/read",
        json={
            "session_id": "session-1",
            "observed_timestamp": observed,
            "observed_turn_id": turn_id,
        },
    )
    assert response.status_code == status
    assert len(acknowledged) == (1 if status == 200 else 0)
