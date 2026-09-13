"""Shared, model-free retry admission for the DGX and operator Mac adapters.

The adapter owns canonical reads, actor binding, persistence and its existing
exclusive lock. A change in task bookkeeping is never evidence of recovery.
This module compares exact observations; semantic recovery remains in the
existing source-authorised inbox, once per incoming request.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

BLOCKER_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "key": {"type": "string"},
        "kind": {"type": "string", "enum": ["dependency", "transient"]},
        "owner": {"type": "string"},
        "recovery": {"type": "string"},
        # JSON text avoids an open-ended structured-output schema. The payload
        # is only exact data: no executable command, path or authority grant.
        "scope_json": {"type": "string"},
        "required_observations_json": {"type": "string"},
        "probe_key": {"type": ["string", "null"]},
    },
    "required": [
        "key",
        "kind",
        "owner",
        "recovery",
        "scope_json",
        "required_observations_json",
        "probe_key",
    ],
    "additionalProperties": False,
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def validate_blocker(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(BLOCKER_SCHEMA["required"]):
        raise ValueError("Invalid blocker fields")
    for key in set(value) - {"probe_key"}:
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("Blocker fields must be non-empty text")
    if value["kind"] not in {"dependency", "transient"} or not (
        value["probe_key"] is None or isinstance(value["probe_key"], str)
    ):
        raise ValueError("Invalid blocker kind or probe key")
    for key in ("scope_json", "required_observations_json"):
        parsed = json.loads(value[key])
        if not isinstance(parsed, dict):
            raise ValueError(  # noqa: TRY004 - invalid terminal result
                "Blocker observations and scope must be JSON objects"
            )
    return value


def retain_blocker(config, state):
    """Incrementally retain old text failures without pretending to classify them."""
    result = state.get("result") or {}
    if result.get("status") not in {"blocked", "needs_input"}:
        return None
    existing = state.get("blocker")
    if existing and existing.get("attempt") == state.get("attempt"):
        return existing
    supplied = validate_blocker(result.get("blocker")) or {}
    blocker = {
        "attempt": state.get("attempt", "legacy"),
        "key": supplied.get("key")
        or digest([result.get("summary"), result.get("question")]),
        "kind": supplied.get("kind", "unclassified"),
        "reason": result.get("summary", "Prior attempt is blocked"),
        "owner": supplied.get("owner", "delegator / operator triage"),
        "recovery": supplied.get("recovery")
        or result.get("question")
        or "Inspect the retained result and supply relevant repair evidence or an explicit retry reason.",
        "observed_at": state.get("finished_at") or state.get("started_at"),
        "scope": json.loads(supplied.get("scope_json", "{}")),
        "required_observations": json.loads(
            supplied.get("required_observations_json", "{}")
        ),
        "probe_key": supplied.get("probe_key"),
        "binding": {
            "task_id": state["task_id"],
            "agent_id": config["agent_id"],
            "organisation_id": config["organisation_id"],
        },
    }
    state["blocker"] = blocker
    return blocker


def waiting_text(blocker):
    return (
        f"Waiting without another coding launch: {blocker['reason']}\n"
        f"Last attempt: {blocker['attempt']} ({blocker.get('observed_at') or 'time unavailable'}).\n"
        f"Recovery owner: {blocker['owner']}. Required: {blocker['recovery']}\n"
        f"Retry after review with a task comment: /retry-coding {blocker['attempt']} <reason>\n"
        + (
            f"Next cheap check: configured probe {blocker['probe_key']} (with backoff)."
            if blocker.get("kind") == "transient" and blocker.get("probe_key")
            else "Next check: canonical repair/retry input on the normal poll; no timed coding retry."
        )
    )


def verified_repair(blocker, receipt, now):
    """A trusted producer must attest exact, fresh, attempt-bound observations.

    No recursive search for a 'ready' boolean: required observations (including
    authentication when relevant) must all match, even if setup_ready is true.
    A receipt can link a private file copy without this module opening it.
    """
    if not isinstance(receipt, dict):
        return False
    observed = timestamp(receipt.get("observed_at"))
    blocked_at = timestamp(blocker.get("observed_at"))
    required = blocker["required_observations"]
    observations = receipt.get("observations")
    return bool(
        required
        and receipt.get("attempt") == blocker["attempt"]
        and receipt.get("blocker_key") == blocker["key"]
        and receipt.get("binding") == blocker["binding"]
        and receipt.get("scope") == blocker["scope"]
        and receipt.get("evidence_reference")
        and receipt.get("reason")
        and observed
        and blocked_at
        and blocked_at < observed <= now
        and isinstance(observations, dict)
        and all(
            key in observations
            and json.dumps(observations[key], sort_keys=True)
            == json.dumps(value, sort_keys=True)
            for key, value in required.items()
        )
    )


def admission(config, state, inputs, *, now=None):
    """Return one recoverable admission, or None. Never infer repair from edits.

    `followup.retry` is written only by the source-authorised inbox adapter.
    Comments are canonical task-service rows; source metadata alone is not
    trusted authorship. The adapter must recheck task authority before launch.
    """
    blocker = retain_blocker(config, state)
    if not blocker:
        return {"kind": "initial"}
    now = now or datetime.now(UTC)
    consumed = state.get("consumed_retry_tokens", [])
    followup = inputs.get("followup") or {}
    retry = followup.get("retry") or {}
    if (
        retry.get("attempt") == blocker["attempt"]
        and retry.get("binding") == blocker["binding"]
        and retry.get("reason")
        and followup.get("message_id")
    ):
        token = "message:" + followup["message_id"]
        if token not in consumed:
            return {
                "kind": "authorised_followup",
                "token": token,
                "reason": retry["reason"],
            }
    for row in inputs.get("comments", []):
        if row.get("author_concept_id") != config["delegator_id"]:
            continue
        comment_id = row.get("comment_id")
        token = "comment:" + str(comment_id)
        if not comment_id or token in consumed:
            continue
        # A deliberate command in the owner's own comment, never quoted text
        # or an arbitrary occurrence of 'retry' in a status report.
        match = re.fullmatch(
            r"/retry-coding ([^\s]+) ([\s\S]+)", row.get("body", "").strip()
        )
        if match and match[1] == blocker["attempt"] and match[2].strip():
            return {"kind": "manual_retry", "token": token, "reason": match[2]}
        receipt = (row.get("source") or {}).get("coding_retry")
        if verified_repair(blocker, receipt, now):
            return {
                "kind": "verified_repair",
                "token": token,
                "reason": receipt["reason"],
                "receipt": receipt,
            }
    return None


def consume(state, decision):
    """Persist with the new running attempt, BEFORE any coding subprocess."""
    token = decision.get("token")
    if token:
        state.setdefault("consumed_retry_tokens", []).append(token)
    state["launch_admission"] = decision
    state.pop("blocker", None)
    state.pop("waiting_report", None)
