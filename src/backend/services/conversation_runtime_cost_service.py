"""Content-free runtime cost snapshots for an authorised conversation.

This module deliberately does not resolve an actor, namespace, or conversation
owner.  Those are authority decisions made by the authenticated route.  It
only projects already-scoped turn records and reuses the canonical LLM usage
and cost summary builder.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .llm_usage_cost_service import build_llm_usage_cost_summary
from .turn_execution_record_service import get_turn_execution_records_collection

CONVERSATION_RUNTIME_COST_SNAPSHOT_SCHEMA_VERSION = (
    "conversation_runtime_cost_snapshot.v1"
)


_CALL_FIELDS = (
    "call_id",
    "type",
    "call_type",
    "stage",
    "status",
    "success",
    "provider_request_sent",
    "provider",
    "requested_model",
    "selected_model",
    "effective_model",
    "model_identity_source",
    "effective_service_tier",
    "connection_id",
    "transport.effective_service_tier",
    "transport.effective_connection_id",
    "transport.connection_id",
    "transport.pricing_context_input_tokens",
    "usage",
    "started_at_utc",
    "completed_at_utc",
    "at_utc",
)


def turn_execution_cost_projection() -> dict[str, int]:
    """Return the content-free fields required for exact cost re-projection."""

    projection = {
        "_id": 0,
        "request_id": 1,
        "user_id": 1,
        "namespace": 1,
        "session_id": 1,
        "created_at_utc": 1,
        "updated_at_utc": 1,
    }
    for root in ("llm_calls", "execution.llm_calls"):
        for field in _CALL_FIELDS:
            projection[f"{root}.{field}"] = 1
    return projection


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _calls_from_one_canonical_source(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Read one call list per turn, matching the persisted-call convention."""

    calls = record.get("llm_calls")
    if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)):
        return [call for call in calls if isinstance(call, Mapping)]
    execution = record.get("execution")
    calls = execution.get("llm_calls") if isinstance(execution, Mapping) else None
    if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)):
        return [call for call in calls if isinstance(call, Mapping)]
    return []


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(
                f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
            )
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _call_is_since_restart(call: Mapping[str, Any], *, server_started_at: datetime) -> bool:
    """A call belongs to this process only when a call timestamp proves it.

    Prefer the completion timestamp, but accept the start timestamp when a
    completion value was not retained.  This keeps the boundary conservative
    while still accounting for an in-flight call which began after restart.
    """

    completed = _parse_utc(call.get("completed_at_utc")) or _parse_utc(
        call.get("at_utc")
    )
    started = _parse_utc(call.get("started_at_utc"))
    return bool(
        (completed is not None and completed >= server_started_at)
        or (started is not None and started >= server_started_at)
    )


def _calls_from_records(
    records: Sequence[Mapping[str, Any]], *, exclude_request_id: str | None = None
) -> list[Mapping[str, Any]]:
    calls: list[Mapping[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if exclude_request_id and _safe_text(record.get("request_id")) == exclude_request_id:
            continue
        calls.extend(_calls_from_one_canonical_source(record))
    return calls


def _mark_temporally_incomplete_summary(
    summary: dict[str, Any], *, included_call_count: int, missing_timestamp_call_count: int
) -> dict[str, Any]:
    """Keep an unprovable restart boundary from looking like a zero-cost run."""

    coverage_status = "complete"
    if missing_timestamp_call_count:
        coverage_status = "partial" if included_call_count else "unavailable"
        cost = summary.get("estimated_cost")
        if isinstance(cost, dict):
            if cost.get("status") == "estimated":
                cost["status"] = "partial"
                cost["amount"] = None
            elif cost.get("status") == "not_applicable":
                cost.update(
                    {
                        "status": "unavailable",
                        "amount": None,
                        "known_amount": None,
                        "currency": None,
                        "reason": "call_timestamp_unavailable",
                    }
                )
        usage = summary.get("usage")
        if isinstance(usage, dict):
            if usage.get("status") == "reported":
                usage["status"] = "partial"
            elif usage.get("status") == "not_applicable":
                usage["status"] = "unavailable"
    summary["runtime_coverage"] = {
        "status": coverage_status,
        "timestamp_eligible_call_count": included_call_count,
        "missing_timestamp_call_count": missing_timestamp_call_count,
    }
    return summary


def build_conversation_runtime_cost_snapshot(
    *,
    conversation_records: Sequence[Mapping[str, Any]],
    actor_runtime_records: Sequence[Mapping[str, Any]],
    conversation_session_id: str,
    actor_concept_id: str,
    namespace: str,
    server_started_at: datetime,
    pid: int,
    server_started_at_utc: str,
    model_registry: Any = None,
    exclude_request_id: str | None = None,
    observe_request_id: str | None = None,
    observe_request_id_persisted: bool = False,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Build a pure snapshot from records already constrained by authority.

    ``conversation_records`` must be the authorised exact session scope
    (including authorised shared-session participants) and
    ``actor_runtime_records`` must be the current actor/current namespace.
    Keeping these inputs separate prevents shared-conversation access from
    broadening the actor's since-restart aggregate.
    """

    clean_exclude_request_id = _safe_text(exclude_request_id)
    clean_observe_request_id = _safe_text(observe_request_id)
    conversation_calls = _calls_from_records(
        conversation_records, exclude_request_id=clean_exclude_request_id
    )
    candidate_runtime_calls = _calls_from_records(
        actor_runtime_records, exclude_request_id=clean_exclude_request_id
    )
    runtime_calls = [
        call
        for call in candidate_runtime_calls
        if _call_is_since_restart(call, server_started_at=server_started_at)
    ]
    missing_timestamp_call_count = sum(
        call.get("provider_request_sent") is not False
        and (_safe_text(call.get("provider")) or "").lower() != "ollama"
        and not any(
            _parse_utc(call.get(key)) is not None
            for key in ("completed_at_utc", "at_utc", "started_at_utc")
        )
        for call in candidate_runtime_calls
    )
    generated_at = (as_of or datetime.now(UTC)).astimezone(UTC)
    return {
        "schema_version": CONVERSATION_RUNTIME_COST_SNAPSHOT_SCHEMA_VERSION,
        "scope": {
            "conversation_session_id": conversation_session_id,
            "actor_concept_id": actor_concept_id,
            "namespace": namespace,
        },
        "runtime": {
            "pid": pid,
            "started_at_utc": server_started_at_utc,
        },
        "conversation": build_llm_usage_cost_summary(
            conversation_calls, model_registry=model_registry
        ),
        "since_restart": _mark_temporally_incomplete_summary(
            build_llm_usage_cost_summary(runtime_calls, model_registry=model_registry),
            included_call_count=len(runtime_calls),
            missing_timestamp_call_count=missing_timestamp_call_count,
        ),
        "handover": (
            {
                "request_id": clean_observe_request_id,
                "observed": bool(observe_request_id_persisted),
            }
            if clean_observe_request_id
            else None
        ),
        "as_of_utc": generated_at.isoformat().replace("+00:00", "Z"),
    }


def get_conversation_runtime_cost_snapshot(
    *,
    conversation_session_id: str,
    actor_concept_id: str,
    actor_namespace: str,
    server_started_at: datetime,
    server_started_at_utc: str,
    pid: int,
    model_registry: Any = None,
    exclude_request_id: str | None = None,
    observe_request_id: str | None = None,
) -> dict[str, Any]:
    """Load content-free scoped records and return a runtime cost snapshot.

    ``exclude_request_id`` is for a separately rendered cumulative live
    request summary.  A not-yet-persisted ID is therefore a safe no-op; a
    persisted ID may be excluded only when it belongs to this actor, namespace
    and selected conversation.  ``observe_request_id`` reports whether that
    same actor-scoped record has reached durable projection so the caller can
    hand a live overlay back to the stored aggregate without guessing.
    """

    collection = get_turn_execution_records_collection()
    if collection is None:
        raise RuntimeError("turn_execution_records_unavailable")
    clean_exclude_request_id = _safe_text(exclude_request_id)
    clean_observe_request_id = _safe_text(observe_request_id)
    if clean_exclude_request_id:
        excluded_record = collection.find_one(
            {
                "session_id": conversation_session_id,
                "request_id": clean_exclude_request_id,
            },
            {"_id": 0, "user_id": 1, "namespace": 1},
        )
        if isinstance(excluded_record, Mapping) and (
            excluded_record.get("user_id") != actor_concept_id
            or excluded_record.get("namespace") != actor_namespace
        ):
            raise ValueError("exclude_request_id_not_owned_by_actor")
    observed_record = None
    if clean_observe_request_id:
        observed_record = collection.find_one(
            {
                "session_id": conversation_session_id,
                "request_id": clean_observe_request_id,
                "user_id": actor_concept_id,
                "namespace": actor_namespace,
            },
            {"_id": 1},
        )
    projection = turn_execution_cost_projection()
    conversation_records: list[Mapping[str, Any]] = []
    actor_runtime_records: list[Mapping[str, Any]] = []
    # An authenticated caller has already proved access to this exact
    # conversation.  Records can be authored by the owner or an accepted
    # participant, so restricting this aggregate to owner scope would make a
    # visible chat total silently incomplete.
    conversation_query: dict[str, Any] = {"session_id": conversation_session_id}
    actor_query: dict[str, Any] = {
        "user_id": actor_concept_id,
        "namespace": actor_namespace,
        "$or": [
            {"created_at_utc": {"$gte": server_started_at_utc}},
            {"updated_at_utc": {"$gte": server_started_at_utc}},
        ],
    }
    if clean_exclude_request_id:
        # Exclude in both reads so an active-record insert between validation
        # and aggregation cannot be counted under the persisted base as well
        # as the live overlay.
        conversation_query["request_id"] = {"$ne": clean_exclude_request_id}
        actor_query["request_id"] = {"$ne": clean_exclude_request_id}
    conversation_cursor = collection.find(conversation_query, projection)
    actor_cursor = collection.find(actor_query, projection)
    conversation_records = [
        record for record in conversation_cursor if isinstance(record, Mapping)
    ]
    actor_runtime_records = [
        record for record in actor_cursor if isinstance(record, Mapping)
    ]
    return build_conversation_runtime_cost_snapshot(
        conversation_records=conversation_records,
        actor_runtime_records=actor_runtime_records,
        conversation_session_id=conversation_session_id,
        actor_concept_id=actor_concept_id,
        namespace=actor_namespace,
        server_started_at=server_started_at,
        server_started_at_utc=server_started_at_utc,
        pid=pid,
        model_registry=model_registry,
        exclude_request_id=clean_exclude_request_id,
        observe_request_id=clean_observe_request_id,
        observe_request_id_persisted=isinstance(observed_record, Mapping),
    )
