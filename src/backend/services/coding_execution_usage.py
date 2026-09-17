"""Usage observations, never subscription charges or API-price allocations.

Only a fresh, single-root-turn exec stream is attributable without taking
possibly cumulative thread counters as attempt deltas. Child/tool usage is not
added to the root aggregate. Unsupported streams retain unknown coverage.
"""

from __future__ import annotations

import hashlib
import json

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
SOURCE = "codex.exec.turn.completed"


def tokens(value):
    if not isinstance(value, dict):
        raise ValueError("Missing token counters")  # noqa: TRY004 - invalid receipt value
    result = {key: value.get(key) for key in TOKEN_FIELDS}
    if any(type(n) is not int or n < 0 for n in result.values()):
        raise ValueError("Token counters must be non-negative integers")
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise ValueError("Cached tokens are a subset of input tokens")
    return result


def unknown(reason, *, configured_model=None, thread_id=None, source=SOURCE):
    return {
        "coverage": "unknown",
        "reason": reason,
        "source": source,
        "provider": None,
        "observed_model": None,
        "configured_model": configured_model,
        "thread_id": thread_id,
        "turn_id": None,
        "turn_ordinal": None,
        "source_sha256": None,
        "tokens": None,
    }


def from_exec_stream(lines, *, configured_model=None):
    """Inspect only top-level lifecycle events from one controller-owned run.

    A digest binds the receipt to the retained bytes without publishing prompts,
    tool arguments, paths or raw event contents into task-visible metadata.
    """
    receipt = unknown(
        "No attributable completed root turn", configured_model=configured_model
    )
    digest = hashlib.sha256()
    threads, starts, completions = set(), [], []
    invalid = False
    for line in lines:
        digest.update(line)
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            invalid = True
            continue
        if not isinstance(event, dict):
            invalid = True
            continue
        kind = event.get("type")
        if kind == "thread.started":
            if event.get("parent_thread_id") or event.get("parent_turn_id"):
                invalid = True
            thread = event.get("thread_id")
            if isinstance(thread, str) and thread:
                threads.add(thread)
            else:
                invalid = True
        elif kind == "turn.started":
            if len(threads) != 1:
                invalid = True
            starts.append(event)
        elif kind == "turn.completed":
            if len(starts) != 1:
                invalid = True
            # Repeated identical terminal delivery is one observation.
            if event not in completions:
                completions.append(event)
        elif kind == "turn.failed":
            invalid = True
    receipt["source_sha256"] = digest.hexdigest()
    if len(threads) == 1:
        receipt["thread_id"] = next(iter(threads))
    if invalid or len(threads) != 1 or len(starts) != 1 or len(completions) != 1:
        receipt["reason"] = "Missing, malformed or ambiguous single-root-turn stream"
        return receipt
    completed = completions[0]
    for event in (starts[0], completed):
        if event.get("thread_id", receipt["thread_id"]) != receipt["thread_id"]:
            receipt["reason"] = "Event thread differs from the root thread"
            return receipt
        if event.get("parent_thread_id") or event.get("parent_turn_id"):
            receipt["reason"] = "Child usage cannot be added to a root aggregate"
            return receipt
    turn_id = completed.get("turn_id") or starts[0].get("turn_id")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        return receipt
    if (
        starts[0].get("turn_id")
        and completed.get("turn_id")
        and starts[0]["turn_id"] != completed["turn_id"]
    ):
        return receipt
    try:
        counters = tokens(completed.get("usage"))
    except ValueError:
        receipt["reason"] = "Missing or invalid terminal token counters"
        return receipt
    receipt.update(
        coverage="partial",
        reason="Observed root aggregate only; child/tool coverage and provider model are unknown",
        tokens=counters,
        turn_id=turn_id,
        turn_ordinal=1,
    )
    return receipt


def validate(receipt):
    """Canonical writer accepts the bounded observation, never money fields."""
    if not isinstance(receipt, dict) or set(receipt) != set(unknown("")):
        raise ValueError("Invalid coding usage receipt fields")
    if receipt["source"] not in {SOURCE, "codex_vscode.consumer_thread"} or receipt[
        "coverage"
    ] not in {"unknown", "partial"}:
        raise ValueError("Unsupported coding usage source or coverage")
    if receipt["provider"] is not None or receipt["observed_model"] is not None:
        raise ValueError("Exec usage does not attest provider/model identity")
    if not isinstance(receipt["reason"], str) or not receipt["reason"]:
        raise ValueError("Usage coverage requires a reason")
    for key in ("configured_model", "thread_id", "turn_id"):
        if receipt[key] is not None and (
            not isinstance(receipt[key], str) or not receipt[key]
        ):
            raise ValueError("Invalid usage identity")
    sha = receipt["source_sha256"]
    if sha is not None and (
        not isinstance(sha, str)
        or len(sha) != 64
        or any(c not in "0123456789abcdef" for c in sha)
    ):
        raise ValueError("Invalid usage evidence digest")
    if receipt["coverage"] == "partial":
        if receipt["source"] != SOURCE:
            raise ValueError("Interactive thread IDs do not attest task token usage")
        if (
            not receipt["thread_id"]
            or not sha
            or type(receipt["turn_ordinal"]) is not int
            or receipt["turn_ordinal"] != 1
        ):
            raise ValueError("Observed usage requires a single root turn and evidence")
        if receipt["tokens"] != tokens(receipt["tokens"]):
            raise ValueError("Unsupported token fields")
    elif receipt["tokens"] is not None or receipt["turn_ordinal"] is not None:
        raise ValueError("Unknown usage cannot supply token totals")


def project(attempts):
    """Task-local observed subtotal; duplicate thread aggregates never add twice.

    Different observations of a reused thread might be cumulative. Exclude the
    whole conflicting group instead of guessing a delta or summing snapshots.
    """
    by_thread = {}
    observed_attempts = 0
    for attempt in attempts:
        receipt = attempt.get("usage") or {}
        if receipt.get("coverage") != "partial":
            continue
        observed_attempts += 1
        by_thread.setdefault(receipt["thread_id"], []).append(receipt["tokens"])
    included, conflicts = [], []
    for thread, observations in by_thread.items():
        if all(value == observations[0] for value in observations):
            included.append(observations[0])
        else:
            conflicts.append(thread)
    subtotal = (
        {key: sum(value[key] for value in included) for key in TOKEN_FIELDS}
        if included
        else None
    )
    return {
        "coverage": "partial" if subtotal is not None else "unknown",
        "observed_tokens": subtotal,
        "observed_total_tokens": (subtotal["input_tokens"] + subtotal["output_tokens"])
        if subtotal is not None
        else None,
        "total_basis": "Observed root aggregates only; cached input is already included in input",
        "observed_attempts": observed_attempts,
        "unknown_attempts": len(attempts) - observed_attempts,
        "distinct_root_threads": len(included),
        "conflicting_threads": sorted(conflicts),
        "reason": "Task-wide coverage is incomplete; no child totals or monetary charges are inferred",
    }
