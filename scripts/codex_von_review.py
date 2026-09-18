"""Receipt-bearing coalescing for an existing operator-owned review relay.

The caller holds its relay lock and supplies canonical message operations. This
module grants no sender authority and starts no worker. Receipt evidence is the
reviewer's explicit attestation, not a claim that syntax verifies its judgement.
"""

from __future__ import annotations

import json
import re
from datetime import datetime


def receipt_from_reply(reply, pending, *, actor, organisation, recipient):
    """Accept only an exact, substantive canonical review of this delivery."""
    if (
        reply.get("sender_id") != actor
        or reply.get("organisation_concept_id") != organisation
        or recipient not in reply.get("recipient_ids", [])
        or reply.get("reply_to_id") != pending["message_id"]
        or not reply.get("message_id")
    ):
        return None
    blocks = re.findall(
        r"```von-review-receipt\s*\n(.*?)\n```", reply.get("content") or "", re.S
    )
    if len(blocks) != 1:
        return None
    try:
        receipt = json.loads(blocks[0])
    except ValueError:
        return None
    expected = {
        "status": "reviewed",
        "actor_id": actor,
        "organisation_id": organisation,
        "source_message_id": pending["message_id"],
        "signature": pending["signature"],
        "reviewed_revision": pending["revision"],
    }
    if not isinstance(receipt, dict) or any(
        receipt.get(k) != v for k, v in expected.items()
    ):
        return None
    evidence = receipt.get("evidence")
    if (
        not isinstance(receipt.get("summary"), str)
        or not receipt["summary"].strip()
        or not isinstance(evidence, list)
        or not evidence
        or any(
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(k), str) or not item[k].strip()
                for k in ("reference", "observation")
            )
            for item in evidence
        )
    ):
        return None
    return receipt


def request_contract(event, *, actor, organisation):
    """Append to the existing authorised request, not to arbitrary Git text."""
    example = {
        "status": "reviewed",
        "actor_id": actor,
        "organisation_id": organisation,
        "source_message_id": "<exact canonical incoming message ID>",
        "signature": event["signature"],
        "reviewed_revision": event["snapshot"]["main"]["sha"],
        "summary": "<substantive review conclusion, including remaining work>",
        "evidence": [
            {
                "reference": "<inspected evidence locator>",
                "observation": "<actual finding>",
            }
        ],
    }
    return (
        "\n\nAfter actually reviewing this exact snapshot, include one fenced "
        "von-review-receipt JSON block in your answer using the following contract. "
        "A queue-only acknowledgement must omit it. Review completion does not "
        "complete an implementation task. Keep task_id empty for a review-only "
        "reply; route unfinished implementation separately to its existing task. "
        "Use the immutable snapshot in this message for these bindings even if "
        "the latest input has advanced.\n```von-review-receipt\n"
        + json.dumps(example, indent=2)
        + "\n```"
    )


def advance(state, event, *, persist, deliver, replies, actor, organisation, recipient):
    """Reconcile one delivery and retain the newest successor before any effect.

    deliver(event) must use a signature-stable key and read back the canonical
    source before returning its ID. replies(pending) supplies canonical replies.
    Exceptions retain the write-ahead intent for the next scheduled invocation.
    """
    # Old last_answered entries proved delivery only, never a substantive review.
    if state.get("last_answered") and "legacy_last_answered" not in state:
        state["legacy_last_answered"] = state.pop("last_answered")
    latest = state.get("latest")
    when = datetime.fromisoformat(event["observed_at"])
    if latest is None or when > datetime.fromisoformat(latest["observed_at"]):
        state["latest"] = event
        persist(state)
    pending = state.get("pending")
    if pending:
        for reply in replies(pending):
            receipt = receipt_from_reply(
                reply,
                pending,
                actor=actor,
                organisation=organisation,
                recipient=recipient,
            )
            if receipt is not None:
                state["last_reviewed"] = {
                    **pending,
                    "reply_id": reply["message_id"],
                    "receipt": receipt,
                }
                state.setdefault("reviewed_signatures", []).append(pending["signature"])
                state.pop("pending")
                persist(state)
                break
        if state.get("pending"):
            return "coalesced"
    # A lost send/read-back must be recovered before a newer event is delivered.
    intent = state.get("delivery_intent")
    if intent is None:
        intent = state["latest"]
        if intent["signature"] in state.get("reviewed_signatures", []):
            return "unchanged"
        state["delivery_intent"] = intent
        persist(state)
    message_id = deliver(intent)
    state["pending"] = {
        "message_id": message_id,
        "signature": intent["signature"],
        "revision": intent["snapshot"]["main"]["sha"],
    }
    state.pop("delivery_intent")
    persist(state)
    return "delivered_not_yet_reviewed"
