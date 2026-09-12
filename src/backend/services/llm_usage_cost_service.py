"""Pure token-usage normalisation and estimated-cost projection.

The caller supplies already-recorded LLM calls and, optionally, a model
registry snapshot.  This module performs no I/O.  Missing usage, exact model
identity, or pricing remains unavailable rather than becoming a misleading
zero.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any

LLM_USAGE_COST_SUMMARY_SCHEMA_VERSION = "llm_usage_cost_summary.v1"
LLM_MODEL_PRICING_SCHEMA_VERSION = "llm_model_pricing.v1"
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "total_tokens",
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _amount(value: Decimal | None) -> float | None:
    if value is None:
        return None
    try:
        amount = float(value)
    except (OverflowError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


def _utc_datetime(value: Any) -> datetime | None:
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


def normalise_llm_usage(
    value: Any,
    *,
    provider_request_sent: bool | None = None,
) -> dict[str, Any]:
    """Normalise existing OpenAI-style and provider-neutral token fields."""

    if provider_request_sent is False:
        return {
            "status": "not_applicable",
            "input_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    usage = value if isinstance(value, Mapping) else {}
    input_tokens = _count(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _count(usage.get("prompt_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _count(usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, Mapping):
        input_details = usage.get("prompt_tokens_details")
    cached_input_tokens = _count(usage.get("cached_input_tokens"))
    if cached_input_tokens is None and isinstance(input_details, Mapping):
        cached_input_tokens = _count(input_details.get("cached_tokens"))
    cache_write_input_tokens = _count(usage.get("cache_write_input_tokens"))
    if cache_write_input_tokens is None and isinstance(input_details, Mapping):
        cache_write_input_tokens = _count(input_details.get("cache_write_tokens"))
    total_tokens = _count(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    if input_tokens is not None and output_tokens is not None:
        status = "reported"
    elif any(value is not None for value in (input_tokens, output_tokens, total_tokens)):
        status = "partial"
    else:
        status = "unavailable"
    return {
        "status": status,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _provider(value: Any) -> str | None:
    cleaned = _text(value)
    return cleaned.lower() if cleaned else None


def _model(value: Any, *, provider: str | None) -> str | None:
    cleaned = _text(value)
    if not cleaned:
        return None
    lowered = cleaned.lower()
    prefix = f"{provider}:" if provider else None
    return lowered[len(prefix) :] if prefix and lowered.startswith(prefix) else lowered


def exact_model_identity_matches(
    *,
    provider: Any,
    model: Any,
    candidate_provider: Any,
    candidate_model: Any,
) -> bool:
    """Match an exact provider/model pair without family-prefix expansion."""

    provider_key = _provider(provider)
    candidate_provider_key = _provider(candidate_provider)
    if not provider_key or provider_key != candidate_provider_key:
        return False
    return bool(
        _model(model, provider=provider_key)
        and _model(model, provider=provider_key)
        == _model(candidate_model, provider=provider_key)
    )


def _registry_entries(value: Any) -> list[Mapping[str, Any]]:
    models = value.get("models") if isinstance(value, Mapping) else value
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
        return []
    return [entry for entry in models if isinstance(entry, Mapping)]


def _exact_pricing(
    *,
    provider: str | None,
    effective_model: str | None,
    model_registry: Any,
) -> Mapping[str, Any] | None:
    if not provider or not effective_model:
        return None
    for entry in _registry_entries(model_registry):
        pricing = entry.get("pricing")
        if not isinstance(pricing, Mapping):
            continue
        if exact_model_identity_matches(
            provider=provider,
            model=effective_model,
            candidate_provider=entry.get("provider"),
            candidate_model=pricing.get("model_id") or entry.get("model_id"),
        ):
            return pricing
    return None


def _pricing_metadata(
    pricing: Mapping[str, Any], *, effective_model: str
) -> dict[str, Any]:
    metadata = {
        "schema_version": _text(pricing.get("schema_version")),
        "version": _text(pricing.get("version")),
        "source": _text(pricing.get("source")),
        "effective_at_utc": _text(pricing.get("effective_at_utc")),
        "model_id": _text(pricing.get("model_id")) or effective_model,
        "unit_tokens": _count(pricing.get("unit_tokens")),
    }
    effective_until_utc = _text(pricing.get("effective_until_utc"))
    if effective_until_utc:
        metadata["effective_until_utc"] = effective_until_utc
    return metadata


def _estimate_call_cost(
    *,
    usage: Mapping[str, Any],
    provider_request_sent: bool | None,
    provider: str | None,
    effective_model: str | None,
    pricing: Mapping[str, Any] | None,
    effective_service_tier: str | None,
    connection_id: str | None,
    pricing_context_input_tokens: int | None,
    pricing_as_of: datetime,
) -> dict[str, Any]:
    if provider_request_sent is False:
        return {
            "status": "not_applicable",
            "amount": None,
            "known_amount": None,
            "currency": None,
            "reason": "provider_request_not_sent",
            "pricing": None,
        }
    if provider == "ollama":
        return {
            "status": "not_applicable",
            "amount": None,
            "known_amount": None,
            "currency": None,
            "reason": "local_provider_financial_cost_not_applicable",
            "pricing": None,
        }
    if not effective_model:
        reason = "effective_model_unavailable"
    elif not isinstance(pricing, Mapping):
        reason = "exact_model_pricing_unavailable"
    else:
        reason = None
    if reason:
        return {
            "status": "unavailable",
            "amount": None,
            "known_amount": None,
            "currency": None,
            "reason": reason,
            "pricing": None,
        }

    assert isinstance(pricing, Mapping) and effective_model is not None
    metadata = _pricing_metadata(pricing, effective_model=effective_model)
    currency = _text(pricing.get("currency"))
    currency = currency.upper() if currency else None
    unit_tokens = _count(pricing.get("unit_tokens"))
    effective_at_raw = _text(pricing.get("effective_at_utc"))
    effective_until_raw = _text(pricing.get("effective_until_utc"))
    effective_at = _utc_datetime(effective_at_raw)
    effective_until = _utc_datetime(effective_until_raw)
    if (effective_at_raw and effective_at is None) or (
        effective_until_raw and effective_until is None
    ) or (
        effective_at is not None
        and effective_until is not None
        and effective_at >= effective_until
    ):
        return {
            "status": "unavailable",
            "amount": None,
            "known_amount": None,
            "currency": currency,
            "reason": "pricing_effective_window_invalid",
            "pricing": metadata,
        }
    if effective_at is not None and pricing_as_of < effective_at:
        return {
            "status": "unavailable",
            "amount": None,
            "known_amount": None,
            "currency": currency,
            "reason": "pricing_not_yet_effective",
            "pricing": metadata,
        }
    if effective_until is not None and pricing_as_of >= effective_until:
        return {
            "status": "unavailable",
            "amount": None,
            "known_amount": None,
            "currency": currency,
            "reason": "pricing_expired",
            "pricing": metadata,
        }
    applicability = pricing.get("applicability")
    if isinstance(applicability, Mapping):
        allowed_tiers = applicability.get("effective_service_tiers")
        allowed_tiers = (
            {
                str(item).strip().lower()
                for item in allowed_tiers
                if isinstance(item, str) and item.strip()
            }
            if isinstance(allowed_tiers, Sequence)
            and not isinstance(allowed_tiers, (str, bytes))
            else set()
        )
        if allowed_tiers and effective_service_tier not in allowed_tiers:
            return {
                "status": "unavailable",
                "amount": None,
                "known_amount": None,
                "currency": currency,
                "reason": "effective_service_tier_unpriced",
                "pricing": metadata,
            }
        allowed_connections = applicability.get("connection_ids")
        allowed_connections = (
            {
                str(item).strip()
                for item in allowed_connections
                if isinstance(item, str) and item.strip()
            }
            if isinstance(allowed_connections, Sequence)
            and not isinstance(allowed_connections, (str, bytes))
            else set()
        )
        if allowed_connections and connection_id not in allowed_connections:
            return {
                "status": "unavailable",
                "amount": None,
                "known_amount": None,
                "currency": currency,
                "reason": "provider_connection_unpriced",
                "pricing": metadata,
            }
    rates = pricing.get("rates")
    input_tokens = _count(usage.get("input_tokens"))
    output_tokens = _count(usage.get("output_tokens"))
    context_band = "standard"
    long_context = pricing.get("long_context")
    if isinstance(long_context, Mapping):
        threshold = _count(long_context.get("threshold_input_tokens"))
        if threshold is None:
            return {
                "status": "unavailable",
                "amount": None,
                "known_amount": None,
                "currency": currency,
                "reason": "pricing_metadata_incomplete",
                "pricing": metadata,
            }
        context_input_tokens = (
            pricing_context_input_tokens
            if pricing_context_input_tokens is not None
            else input_tokens
        )
        if context_input_tokens is None:
            return {
                "status": "unavailable",
                "amount": None,
                "known_amount": None,
                "currency": currency,
                "reason": "input_tokens_required_for_context_band",
                "pricing": metadata,
            }
        metadata["context_threshold_input_tokens"] = threshold
        if context_input_tokens > threshold:
            rates = long_context.get("rates")
            context_band = "long"
        else:
            context_band = "short"
    metadata["context_band"] = context_band
    if effective_service_tier:
        metadata["effective_service_tier"] = effective_service_tier
    input_rate = _decimal(rates.get("input_tokens")) if isinstance(rates, Mapping) else None
    output_rate = _decimal(rates.get("output_tokens")) if isinstance(rates, Mapping) else None
    pricing_complete = bool(
        pricing.get("schema_version") == LLM_MODEL_PRICING_SCHEMA_VERSION
        and metadata.get("version")
        and metadata.get("source")
        and metadata.get("effective_at_utc")
        and currency
        and unit_tokens
    )
    if not pricing_complete:
        return {
            "status": "unavailable",
            "amount": None,
            "known_amount": None,
            "currency": currency,
            "reason": "pricing_metadata_incomplete",
            "pricing": metadata,
        }

    subtotal = Decimal(0)
    priced_components = 0
    missing_components: list[str] = []
    detailed_input_pricing = bool(
        isinstance(rates, Mapping)
        and (
            "cached_input_tokens" in rates
            or "cache_write_input_tokens" in rates
        )
    )
    if detailed_input_pricing:
        cached_input_tokens = _count(usage.get("cached_input_tokens"))
        cache_write_input_tokens = _count(usage.get("cache_write_input_tokens"))
        if (
            input_tokens is not None
            and cached_input_tokens is not None
            and cache_write_input_tokens is not None
            and cached_input_tokens + cache_write_input_tokens > input_tokens
        ):
            return {
                "status": "unavailable",
                "amount": None,
                "known_amount": None,
                "currency": currency,
                "reason": "usage_breakdown_invalid",
                "pricing": metadata,
            }
        ordinary_input_tokens = (
            input_tokens - cached_input_tokens - cache_write_input_tokens
            if input_tokens is not None
            and cached_input_tokens is not None
            and cache_write_input_tokens is not None
            else None
        )
        cost_components = (
            ("input_tokens", ordinary_input_tokens, input_rate),
            (
                "cached_input_tokens",
                cached_input_tokens,
                _decimal(rates.get("cached_input_tokens")),
            ),
            (
                "cache_write_input_tokens",
                cache_write_input_tokens,
                _decimal(rates.get("cache_write_input_tokens")),
            ),
            ("output_tokens", output_tokens, output_rate),
        )
    else:
        cost_components = (
            ("input_tokens", input_tokens, input_rate),
            ("output_tokens", output_tokens, output_rate),
        )
    try:
        for field, count, rate in cost_components:
            if count is None:
                missing_components.append(field)
            elif rate is None and count > 0:
                missing_components.append(f"{field}_rate")
            elif rate is not None:
                subtotal += Decimal(count) * rate
                priced_components += 1
        known_amount = subtotal / Decimal(unit_tokens) if priced_components else None
    except DecimalException:
        return {
            "status": "unavailable",
            "amount": None,
            "known_amount": None,
            "currency": currency,
            "reason": "estimated_cost_not_representable",
            "pricing": metadata,
        }
    if not missing_components and usage.get("status") == "reported":
        status = "estimated"
        amount = known_amount
        reason = None
    elif known_amount is not None:
        status = "partial"
        amount = None
        reason = "usage_or_rate_coverage_partial"
    else:
        status = "unavailable"
        amount = None
        known_amount = None
        reason = "usage_or_rate_coverage_unavailable"
    rendered_amount = _amount(amount)
    rendered_known_amount = _amount(known_amount)
    if status == "estimated" and rendered_amount is None:
        status = "unavailable"
        reason = "estimated_cost_not_representable"
    elif status == "partial" and rendered_known_amount is None:
        status = "unavailable"
        reason = "known_cost_not_representable"
    if status == "unavailable":
        rendered_amount = None
        rendered_known_amount = None
    return {
        "status": status,
        "amount": rendered_amount,
        "known_amount": rendered_known_amount,
        "amount_decimal": str(amount) if rendered_amount is not None else None,
        "known_amount_decimal": str(known_amount) if rendered_known_amount is not None else None,
        "currency": currency,
        "reason": reason,
        "pricing": metadata,
    }


def _merge_duplicate(previous: Mapping[str, Any], later: Mapping[str, Any]) -> dict:
    merged = dict(previous)
    for key, value in later.items():
        if value not in (None, "", [], {}):
            merged[str(key)] = value
    if isinstance(previous.get("usage"), Mapping) and isinstance(
        later.get("usage"), Mapping
    ):
        merged["usage"] = {**previous["usage"], **later["usage"]}
    return merged


def _deduplicate(value: Any) -> tuple[list[Mapping[str, Any]], int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], 0, 0
    raw = [call for call in value if isinstance(call, Mapping)]
    result: list[Mapping[str, Any]] = []
    indexes: dict[str, int] = {}
    duplicates = 0
    for call in raw:
        call_id = _text(call.get("call_id"))
        if not call_id:
            result.append(call)
        elif call_id not in indexes:
            indexes[call_id] = len(result)
            result.append(call)
        else:
            index = indexes[call_id]
            result[index] = _merge_duplicate(result[index], call)
            duplicates += 1
    return result, len(raw), duplicates


def _normalise_call(
    call: Mapping[str, Any],
    *,
    source_index: int,
    model_registry: Any,
    pricing_as_of: datetime,
) -> dict[str, Any]:
    provider_request_sent = (
        call.get("provider_request_sent")
        if isinstance(call.get("provider_request_sent"), bool)
        else None
    )
    provider = _provider(call.get("provider"))
    effective_model = _text(call.get("effective_model"))
    identity_source = _text(call.get("model_identity_source"))
    pricing_effective_model = (
        effective_model if identity_source == "provider_response" else None
    )
    usage_value = call.get("usage")
    usage = normalise_llm_usage(
        usage_value if isinstance(usage_value, Mapping) else call,
        provider_request_sent=provider_request_sent,
    )
    transport = call.get("transport")
    transport = transport if isinstance(transport, Mapping) else {}
    effective_service_tier = _text(
        call.get("effective_service_tier")
        or transport.get("effective_service_tier")
    )
    effective_service_tier = (
        effective_service_tier.lower() if effective_service_tier else None
    )
    connection_id = _text(
        call.get("connection_id")
        or transport.get("effective_connection_id")
        or transport.get("connection_id")
    )
    pricing_context_input_tokens = _count(
        transport.get("pricing_context_input_tokens")
    )
    cost = _estimate_call_cost(
        usage=usage,
        provider_request_sent=provider_request_sent,
        provider=provider,
        effective_model=pricing_effective_model,
        pricing=_exact_pricing(
            provider=provider,
            effective_model=pricing_effective_model,
            model_registry=model_registry,
        ),
        effective_service_tier=effective_service_tier,
        connection_id=connection_id,
        pricing_context_input_tokens=pricing_context_input_tokens,
        pricing_as_of=pricing_as_of,
    )
    return {
        "call_id": _text(call.get("call_id")),
        "source_index": source_index,
        "type": _text(call.get("type") or call.get("call_type")),
        "stage": _text(call.get("stage")),
        "status": _text(call.get("status")),
        "success": call.get("success") if isinstance(call.get("success"), bool) else None,
        "provider_request_sent": provider_request_sent,
        "provider": provider,
        "requested_model": _text(call.get("requested_model")),
        "selected_model": _text(call.get("selected_model")),
        "effective_model": effective_model,
        "model_identity_source": identity_source,
        "effective_service_tier": effective_service_tier,
        "connection_id": connection_id,
        "usage": usage,
        "estimated_cost": cost,
    }


def _usage_summary(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    billable = [call for call in calls if call.get("provider_request_sent") is not False]
    statuses = [call["usage"]["status"] for call in billable]
    if not billable:
        status = "not_applicable"
    elif all(value == "reported" for value in statuses):
        status = "reported"
    elif any(value in {"reported", "partial"} for value in statuses):
        status = "partial"
    else:
        status = "unavailable"
    result: dict[str, Any] = {
        "status": status,
        "reported_call_count": statuses.count("reported"),
        "partial_call_count": statuses.count("partial"),
        "unavailable_call_count": statuses.count("unavailable"),
    }
    for field in _TOKEN_FIELDS:
        values = [
            _count(call["usage"].get(field))
            for call in billable
            if call["usage"].get("status") != "not_applicable"
        ]
        known = [value for value in values if value is not None]
        known_total = sum(known) if known else None
        result[f"known_{field}"] = known_total
        result[field] = known_total if values and len(known) == len(values) else None
    return result


def _cost_summary(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    billable = [
        call
        for call in calls
        if call["estimated_cost"].get("status") != "not_applicable"
    ]
    costs = [call["estimated_cost"] for call in billable]
    currencies = {
        cost["currency"]
        for cost in costs
        if cost.get("known_amount") is not None and cost.get("currency")
    }
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    known_values = [
        _decimal(cost.get("known_amount_decimal") or cost.get("known_amount"))
        for cost in costs
        if currency and cost.get("currency") == currency
    ]
    known_values = [value for value in known_values if value is not None]
    known_amount = sum(known_values, Decimal(0)) if known_values else None
    priced = sum(cost.get("status") == "estimated" for cost in costs)
    partial = sum(cost.get("status") == "partial" for cost in costs)
    unavailable = sum(cost.get("status") == "unavailable" for cost in costs)
    if not billable:
        status = "not_applicable"
        known_amount = None
    elif priced == len(billable) and currency:
        status = "estimated"
    elif known_amount is not None and currency:
        status = "partial"
    else:
        status = "unavailable"
        known_amount = None
    versions = sorted(
        {
            cost["pricing"]["version"]
            for cost in costs
            if isinstance(cost.get("pricing"), Mapping)
            and cost["pricing"].get("version")
        }
    )
    pricing_identities = {
        (
            cost["pricing"].get("source"),
            cost["pricing"].get("version"),
            cost["pricing"].get("effective_at_utc"),
        )
        for cost in costs
        if isinstance(cost.get("pricing"), Mapping)
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
        "status": status,
        "amount": _amount(known_amount) if status == "estimated" else None,
        "known_amount": _amount(known_amount),
        "amount_decimal": str(known_amount) if status == "estimated" else None,
        "known_amount_decimal": str(known_amount) if known_amount is not None else None,
        "currency": currency,
        "priced_call_count": priced,
        "partial_call_count": partial,
        "unpriced_call_count": unavailable,
        "pricing_versions": versions,
        "pricing": pricing,
        "unavailable_reasons": sorted(
            {
                str(cost.get("reason"))
                for cost in costs
                if cost.get("status") == "unavailable" and cost.get("reason")
            }
        )[:20],
    }


def _model_identity_summary(
    calls: Sequence[Mapping[str, Any]], *, limit: int = 20
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    indexes: dict[tuple[Any, ...], int] = {}
    seen: set[tuple[Any, ...]] = set()
    for call in calls:
        identity = {
            "provider": call.get("provider"),
            "requested_model": call.get("requested_model"),
            "selected_model": call.get("selected_model"),
            "effective_model": call.get("effective_model"),
            "model_identity_source": call.get("model_identity_source"),
            "provider_request_sent": call.get("provider_request_sent"),
            "effective_service_tier": call.get("effective_service_tier"),
            "connection_id": call.get("connection_id"),
        }
        key = tuple(identity.values())
        seen.add(key)
        if key in indexes:
            rows[indexes[key]]["call_count"] += 1
            continue
        if len(rows) >= limit:
            continue
        indexes[key] = len(rows)
        rows.append({**identity, "call_count": 1})
    return rows, max(0, len(seen) - len(rows))


def _pricing_usage_group_summary(
    calls: Sequence[Mapping[str, Any]], *, limit: int = 50
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Retain bounded content-free usage groups for honest re-pricing.

    Calls are grouped only when their exact per-call usage is identical. The
    aggregate token counts therefore remain linear while
    ``pricing_context_input_tokens`` preserves the per-request input size used
    to select a context price band.
    """

    rows: list[dict[str, Any]] = []
    indexes: dict[tuple[Any, ...], int] = {}
    seen: set[tuple[Any, ...]] = set()
    covered_call_count = 0
    unattributed_call_count = 0
    for call in calls:
        if call.get("provider_request_sent") is False or call.get("provider") == "ollama":
            continue
        if (
            not call.get("provider")
            or not call.get("effective_model")
            or call.get("model_identity_source") != "provider_response"
        ):
            unattributed_call_count += 1
            continue
        usage = call.get("usage")
        if not isinstance(usage, Mapping):
            continue
        identity = (
            call.get("provider"),
            call.get("effective_model"),
            call.get("model_identity_source"),
            call.get("provider_request_sent"),
            call.get("effective_service_tier"),
            call.get("connection_id"),
        )
        usage_key = tuple(usage.get(field) for field in ("status", *_TOKEN_FIELDS))
        key = (*identity, *usage_key)
        seen.add(key)
        if key in indexes:
            row = rows[indexes[key]]
            row["call_count"] += 1
            covered_call_count += 1
            aggregate_usage = row["usage"]
            for field in _TOKEN_FIELDS:
                prior = _count(aggregate_usage.get(field))
                current = _count(usage.get(field))
                aggregate_usage[field] = (
                    prior + current
                    if prior is not None and current is not None
                    else None
                )
            continue
        if len(rows) >= limit:
            continue
        indexes[key] = len(rows)
        rows.append(
            {
                "provider": call.get("provider"),
                "effective_model": call.get("effective_model"),
                "model_identity_source": call.get("model_identity_source"),
                "provider_request_sent": call.get("provider_request_sent"),
                "effective_service_tier": call.get("effective_service_tier"),
                "connection_id": call.get("connection_id"),
                "pricing_context_input_tokens": _count(
                    usage.get("input_tokens")
                ),
                "usage": {str(key): value for key, value in usage.items()},
                "call_count": 1,
            }
        )
        covered_call_count += 1
    return (
        rows,
        max(0, len(seen) - len(rows)),
        covered_call_count,
        unattributed_call_count,
    )


def build_llm_usage_cost_summary(
    llm_calls: Any,
    *,
    model_registry: Any = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Build one content-free summary from one canonical call sequence."""

    unique, raw_count, duplicate_count = _deduplicate(llm_calls)
    pricing_as_of = _utc_datetime(as_of) if as_of is not None else datetime.now(UTC)
    if pricing_as_of is None:
        raise ValueError("as_of must be a valid datetime")
    calls = [
        _normalise_call(
            call,
            source_index=index,
            model_registry=model_registry,
            pricing_as_of=pricing_as_of,
        )
        for index, call in enumerate(unique)
    ]
    model_identities, omitted_identity_count = _model_identity_summary(calls)
    (
        pricing_usage_groups,
        omitted_usage_group_count,
        covered_usage_call_count,
        unattributed_usage_call_count,
    ) = (
        _pricing_usage_group_summary(calls)
    )
    return {
        "schema_version": LLM_USAGE_COST_SUMMARY_SCHEMA_VERSION,
        "call_count": raw_count,
        "unique_call_count": len(calls),
        "duplicate_call_count": duplicate_count,
        "billable_call_count": sum(
            call["estimated_cost"].get("status") != "not_applicable"
            for call in calls
        ),
        "usage": _usage_summary(calls),
        "estimated_cost": _cost_summary(calls),
        "model_identities": model_identities,
        "model_identity_omitted_count": omitted_identity_count,
        "pricing_usage_groups": pricing_usage_groups,
        "pricing_usage_group_omitted_count": omitted_usage_group_count,
        "pricing_usage_group_covered_call_count": covered_usage_call_count,
        "pricing_usage_group_unattributed_call_count": (
            unattributed_usage_call_count
        ),
    }
