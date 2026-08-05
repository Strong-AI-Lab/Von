"""Bounded actor-scoped historical model-cost projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .llm_usage_cost_service import (
    LLM_USAGE_COST_SUMMARY_SCHEMA_VERSION,
    build_llm_usage_cost_summary,
    exact_model_identity_matches,
)
from .turn_execution_record_service import get_turn_execution_records_collection

LLM_MODEL_TURN_COST_SUMMARY_SCHEMA_VERSION = "llm_model_turn_cost_summary.v1"


def _calls_from_one_canonical_source(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    calls = record.get("llm_calls")
    if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)):
        return [call for call in calls if isinstance(call, Mapping)]
    execution = record.get("execution")
    calls = execution.get("llm_calls") if isinstance(execution, Mapping) else None
    if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)):
        return [call for call in calls if isinstance(call, Mapping)]
    return []


def _matching_calls(
    calls: Sequence[Mapping[str, Any]], *, provider: str, model: str
) -> list[Mapping[str, Any]]:
    return [
        call
        for call in calls
        if call.get("provider_request_sent") is not False
        if call.get("model_identity_source") == "provider_response"
        if exact_model_identity_matches(
            provider=provider,
            model=model,
            candidate_provider=call.get("provider"),
            candidate_model=call.get("effective_model"),
        )
    ]


def _calls_from_content_free_summary(
    record: Mapping[str, Any], *, provider: str, model: str
) -> tuple[bool, bool, list[Mapping[str, Any]] | None]:
    """Project safely repriceable calls from a persisted bounded summary.

    Returns whether the record has the current summary schema, whether that
    summary attributes the turn to the target, and either the complete
    content-free call groups needed to price that turn or ``None`` when the
    retained evidence is incomplete.
    """

    summary = record.get("llm_usage_cost_summary")
    if not isinstance(summary, Mapping) or (
        summary.get("schema_version") != LLM_USAGE_COST_SUMMARY_SCHEMA_VERSION
    ):
        return False, False, None

    identities_value = summary.get("model_identities")
    identities = (
        [row for row in identities_value if isinstance(row, Mapping)]
        if isinstance(identities_value, Sequence)
        and not isinstance(identities_value, (str, bytes))
        else []
    )
    billable_identities = [
        row for row in identities if row.get("provider_request_sent") is not False
    ]
    matching_identities = [
        row
        for row in billable_identities
        if row.get("model_identity_source") == "provider_response"
        if exact_model_identity_matches(
            provider=provider,
            model=model,
            candidate_provider=row.get("provider"),
            candidate_model=row.get("effective_model"),
        )
    ]
    if not matching_identities:
        return True, False, None

    groups_value = summary.get("pricing_usage_groups")
    if isinstance(groups_value, Sequence) and not isinstance(
        groups_value, (str, bytes)
    ):
        omitted_groups = summary.get("pricing_usage_group_omitted_count")
        if (
            not isinstance(omitted_groups, int)
            or isinstance(omitted_groups, bool)
            or omitted_groups != 0
        ):
            return True, True, None
        valid_groups = [
            row
            for row in groups_value
            if isinstance(row, Mapping)
            and row.get("provider_request_sent") is not False
            and row.get("model_identity_source") == "provider_response"
        ]
        covered_call_count = summary.get(
            "pricing_usage_group_covered_call_count"
        )
        grouped_call_count = sum(
            row.get("call_count")
            for row in valid_groups
            if isinstance(row.get("call_count"), int)
            and not isinstance(row.get("call_count"), bool)
            and row.get("call_count") > 0
        )
        if (
            not isinstance(covered_call_count, int)
            or isinstance(covered_call_count, bool)
            or covered_call_count != summary.get("billable_call_count")
            or grouped_call_count != covered_call_count
            or len(valid_groups) != len(groups_value)
        ):
            return True, True, None
        matching_groups = [
            row
            for row in valid_groups
            if exact_model_identity_matches(
                provider=provider,
                model=model,
                candidate_provider=row.get("provider"),
                candidate_model=row.get("effective_model"),
            )
        ]
        if not matching_groups:
            return True, True, None
        projected_calls: list[Mapping[str, Any]] = []
        for group in valid_groups:
            usage = group.get("usage")
            if not isinstance(usage, Mapping):
                return True, True, None
            projected_calls.append(
                {
                    "provider": group.get("provider"),
                    "effective_model": group.get("effective_model"),
                    "model_identity_source": "provider_response",
                    "provider_request_sent": True,
                    "usage": usage,
                    "transport": {
                        "effective_service_tier": group.get(
                            "effective_service_tier"
                        ),
                        "effective_connection_id": group.get("connection_id"),
                        "pricing_context_input_tokens": group.get(
                            "pricing_context_input_tokens"
                        ),
                    },
                }
            )
        return True, True, projected_calls

    # A pre-group v1 summary can be re-priced only when it represents exactly
    # one provider-observed call. Aggregating multiple calls loses the
    # per-request input size required for context-band pricing.
    omitted_identities = summary.get("model_identity_omitted_count")
    if (
        summary.get("unique_call_count") != 1
        or not isinstance(omitted_identities, int)
        or isinstance(omitted_identities, bool)
        or omitted_identities != 0
        or len(matching_identities) != len(billable_identities)
    ):
        return True, True, None
    usage = summary.get("usage")
    if not isinstance(usage, Mapping):
        return True, True, None
    identity = matching_identities[0]
    return True, True, [
        {
            "provider": provider,
            "effective_model": model,
            "model_identity_source": "provider_response",
            "provider_request_sent": True,
            "usage": usage,
            "transport": {
                "effective_service_tier": identity.get(
                    "effective_service_tier"
                ),
                "effective_connection_id": identity.get("connection_id"),
                "pricing_context_input_tokens": usage.get("input_tokens"),
            },
        }
    ]


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def build_historical_model_cost_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    provider: str,
    model: str,
    model_registry: Any = None,
    limit: int = 200,
    window_days: int = 30,
) -> dict[str, Any]:
    attributed = priced = partial = unavailable = 0
    priced_amounts: list[Decimal] = []
    currency: str | None = None
    timestamps: list[str] = []
    pricing_rows: list[Mapping[str, Any]] = []

    for record in records[:limit]:
        has_summary, summary_attributed, summary_calls = (
            _calls_from_content_free_summary(record, provider=provider, model=model)
        )
        if has_summary:
            if not summary_attributed:
                continue
            matching = summary_calls
        else:
            canonical_calls = _calls_from_one_canonical_source(record)
            matching = _matching_calls(
                canonical_calls, provider=provider, model=model
            )
            if not matching:
                continue
        attributed += 1
        timestamp = str(record.get("created_at_utc") or "").strip()
        if timestamp:
            timestamps.append(timestamp)

        if matching is None:
            unavailable += 1
            continue

        # Usage and exact effective identity are durable observations. Rebuild
        # their estimate under the current one-version pricing basis so calls
        # recorded before pricing existed are not permanently stranded and a
        # sample cannot silently mix price versions.
        turn_summary = build_llm_usage_cost_summary(
            matching,
            model_registry=model_registry,
        )
        turn_cost = turn_summary.get("estimated_cost")
        turn_cost = turn_cost if isinstance(turn_cost, Mapping) else {}
        turn_currency = str(turn_cost.get("currency") or "").strip().upper()
        turn_amount = _decimal(turn_cost.get("amount"))
        if (
            turn_cost.get("status") == "estimated"
            and turn_currency
            and turn_amount is not None
        ):
            if currency is None:
                currency = turn_currency
            if currency == turn_currency:
                priced += 1
                priced_amounts.append(turn_amount)
                if isinstance(turn_cost.get("pricing"), Mapping):
                    pricing_rows.append(turn_cost["pricing"])
                continue
        if (
            turn_cost.get("status") == "partial"
            and turn_currency
            and _decimal(turn_cost.get("known_amount")) is not None
        ):
            partial += 1
        else:
            unavailable += 1

    average = (
        sum(priced_amounts, Decimal(0)) / Decimal(len(priced_amounts))
        if priced_amounts
        else None
    )
    if attributed == 0:
        status = "insufficient_coverage"
    elif priced == attributed:
        status = "available"
    elif priced or partial:
        status = "partial"
    else:
        status = "unavailable"
    pricing_identities = {
        (
            row.get("source"),
            row.get("version"),
            row.get("effective_at_utc"),
        )
        for row in pricing_rows
    }
    pricing = None
    if len(pricing_identities) == 1:
        source, version, effective_at_utc = next(iter(pricing_identities))
        pricing = {
            "source": source,
            "version": version,
            "effective_at_utc": effective_at_utc,
        }
    return {
        "schema_version": LLM_MODEL_TURN_COST_SUMMARY_SCHEMA_VERSION,
        "status": status,
        "model_identity": {"provider": provider.lower(), "model": model},
        "average_attributed_cost_per_turn": {
            "amount": (
                float(average)
                if status == "available" and average is not None
                else None
            ),
            "currency": currency if status == "available" else None,
        },
        "sample_window": {
            "attributed_turn_count": attributed,
            "started_at_utc": min(timestamps) if timestamps else None,
            "ended_at_utc": max(timestamps) if timestamps else None,
            "limit": limit,
            "window_days": window_days,
        },
        "coverage": {
            "priced_turn_count": priced,
            "partial_turn_count": partial,
            "unavailable_turn_count": unavailable,
            "attributed_turn_count": attributed,
        },
        "pricing": pricing,
    }


def get_actor_historical_model_cost_summary(
    *,
    user_id: str,
    namespace: str,
    provider: str,
    model: str,
    model_registry: Any = None,
    limit: int = 200,
    window_days: int = 30,
) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), 500))
    safe_days = max(1, min(int(window_days), 365))
    collection = get_turn_execution_records_collection()
    records: list[Mapping[str, Any]] = []
    if collection is not None:
        since = (datetime.now(UTC) - timedelta(days=safe_days)).isoformat()
        provider_key = provider.strip().lower()
        model_key = model.strip()

        def _matching_call() -> dict[str, Any]:
            return {
                "provider": provider_key,
                "effective_model": model_key,
                "model_identity_source": "provider_response",
                "provider_request_sent": {"$ne": False},
            }

        current_summary = {
            "llm_usage_cost_summary.schema_version": (
                LLM_USAGE_COST_SUMMARY_SCHEMA_VERSION
            ),
            "llm_usage_cost_summary.model_identities": {
                "$elemMatch": {
                    **_matching_call(),
                }
            },
        }
        legacy_summary = {
            "llm_usage_cost_summary.schema_version": {
                "$ne": LLM_USAGE_COST_SUMMARY_SCHEMA_VERSION
            }
        }

        cursor = collection.find(
            {
                "user_id": user_id,
                "namespace": namespace,
                "created_at_utc": {"$gte": since},
                "$or": [
                    current_summary,
                    {
                        **legacy_summary,
                        "llm_calls": {"$elemMatch": _matching_call()},
                    },
                    {
                        **legacy_summary,
                        "llm_calls": {"$exists": False},
                        "execution.llm_calls": {"$elemMatch": _matching_call()},
                    },
                ],
            },
            {
                "_id": 0,
                "created_at_utc": 1,
                "llm_usage_cost_summary": 1,
                "llm_calls": 1,
                "execution.llm_calls": 1,
            },
        )
        try:
            cursor = cursor.sort("created_at_utc", -1).limit(safe_limit)
        except AttributeError:
            pass
        records = [record for record in cursor if isinstance(record, Mapping)]
    return build_historical_model_cost_summary(
        records,
        provider=provider,
        model=model,
        model_registry=model_registry,
        limit=safe_limit,
        window_days=safe_days,
    )
