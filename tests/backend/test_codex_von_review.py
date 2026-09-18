"""Review progress requires a source-bound substantive canonical receipt."""

import copy
import json

import pytest

from scripts import codex_von_review as review

ACTOR, ORG, OWNER = "#V#manager", "#V#org", "#V#owner"


def event(n):
    return {
        "observed_at": f"2026-09-18T05:{n:02}:00+00:00",
        "signature": str(n),
        "snapshot": {"main": {"sha": str(n) * 40}},
    }


def reply(pending):
    receipt = {
        "status": "reviewed",
        "actor_id": ACTOR,
        "organisation_id": ORG,
        "source_message_id": pending["message_id"],
        "signature": pending["signature"],
        "reviewed_revision": pending["revision"],
        "summary": "Reviewed change; no new regression.",
        "evidence": [
            {
                "reference": "retained/test-receipt.json",
                "observation": "Targeted tests passed.",
            }
        ],
    }
    return {
        "message_id": "#V#reply",
        "sender_id": ACTOR,
        "organisation_concept_id": ORG,
        "recipient_ids": [OWNER],
        "reply_to_id": pending["message_id"],
        "content": "Review conclusion\n```von-review-receipt\n"
        + json.dumps(receipt)
        + "\n```",
    }


def advance(state, ev, *, deliver=None, replies=None, persist=None):
    return review.advance(
        state,
        ev,
        persist=persist or (lambda s: None),
        deliver=deliver or (lambda e: "#V#request" + e["signature"]),
        replies=replies or (lambda p: []),
        actor=ACTOR,
        organisation=ORG,
        recipient=OWNER,
    )


@pytest.mark.parametrize(
    "field",
    [
        "actor_id",
        "organisation_id",
        "source_message_id",
        "signature",
        "reviewed_revision",
        "status",
        "summary",
        "evidence",
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_wrong_or_missing_receipt_cannot_advance(field, missing):
    state = {}
    advance(state, event(1))
    row = reply(state["pending"])
    body = json.loads(
        row["content"].split("```von-review-receipt\n")[1].split("\n```")[0]
    )
    if missing:
        body.pop(field)
    else:
        body[field] = "" if field != "evidence" else [{"reference": "file"}]
    row["content"] = "```von-review-receipt\n" + json.dumps(body) + "\n```"
    assert advance(state, event(2), replies=lambda p: [row]) == "coalesced"
    assert "last_reviewed" not in state
    assert state["pending"]["signature"] == "1"
    assert state["latest"]["signature"] == "2"


@pytest.mark.parametrize(
    "field,value",
    [
        ("sender_id", "#V#wrong"),
        ("organisation_concept_id", None),
        ("reply_to_id", "#V#other"),
        ("recipient_ids", []),
        ("content", "Queued for review."),
    ],
)
def test_canonical_envelope_and_queue_ack_are_not_review(field, value):
    state = {}
    advance(state, event(1))
    row = dict(reply(state["pending"]), **{field: value})
    assert advance(state, event(2), replies=lambda p: [row]) == "coalesced"
    assert "last_reviewed" not in state


def test_one_pending_and_latest_successor_survive_reordering_and_readback_failure():
    state, delivered, persisted = {}, [], []

    def send(e):
        delivered.append(e["signature"])
        return "#V#request" + e["signature"]

    advance(state, event(1), deliver=send)
    row = reply(state["pending"])

    def failed_read(p):
        raise OSError("Canonical read-back temporarily unavailable")

    with pytest.raises(OSError):
        advance(
            state,
            event(3),
            replies=failed_read,
            persist=lambda s: persisted.append(copy.deepcopy(s)),
        )
    assert persisted[-1]["latest"] == event(3)
    state = persisted[-1]  # restarted relay
    assert (
        advance(state, event(2), replies=lambda p: [row], deliver=send)
        == "delivered_not_yet_reviewed"
    )
    assert delivered == ["1", "3"]
    assert state["last_reviewed"]["signature"] == "1"
    assert state["pending"]["signature"] == "3"
    advance(state, event(1), replies=lambda p: [row], deliver=send)
    assert delivered == ["1", "3"]  # stale reply cannot answer successor
    advance(state, event(3), replies=lambda p: [reply(p)], deliver=send)
    assert advance(state, event(1), deliver=send) == "unchanged"
    assert delivered == ["1", "3"]


def test_interrupted_delivery_recovers_same_intent_before_successor():
    state, effects = {}, {}

    def send(e):
        effects.setdefault(e["signature"], "#V#request" + e["signature"])
        if len(effects) == 1 and not state.get("retried"):
            raise OSError("Lost acknowledgement after send")
        return effects[e["signature"]]

    with pytest.raises(OSError):
        advance(state, event(1), deliver=send)
    state["retried"] = True
    advance(state, event(2), deliver=send)
    assert list(effects) == ["1"]
    advance(state, event(2), deliver=send, replies=lambda p: [reply(p)])
    assert list(effects) == ["1", "2"]


def test_legacy_answered_cursor_is_not_reused_as_review():
    state = {"last_delivered_signature": "1", "last_answered": {"signature": "1"}}
    assert advance(state, event(1)) == "delivered_not_yet_reviewed"
    assert state["legacy_last_answered"] == {"signature": "1"}
    assert "last_reviewed" not in state
