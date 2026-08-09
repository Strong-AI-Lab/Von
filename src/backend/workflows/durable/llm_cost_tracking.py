"""Content-free LLM usage collection for durable workflow executions.

Durable workflows can contain nested represented subworkflows. The child
runtime intentionally does not leak its general execution context into the
parent, but provider usage must still be attributable to the top-level durable
instance. This module keeps only the bounded fields consumed by the existing
LLM usage/cost service and builds the same canonical summary used by chat
turns.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from ...services.llm_usage_cost_service import build_llm_usage_cost_summary

LLM_USAGE_COST_SUMMARY_KEY = "llm_usage_cost_summary"

_COST_CALL_FIELDS = (
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
    "started_at_utc",
    "ended_at_utc",
    "duration_ms",
    "usage",
    "transport",
)


def project_llm_calls_for_cost(value: Any) -> list[dict[str, Any]]:
    """Return a bounded, content-free projection of recorded model calls."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    projected: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        call = {
            field: item.get(field)
            for field in _COST_CALL_FIELDS
            if item.get(field) is not None
        }
        if call:
            projected.append(call)
    return projected


def merge_nested_llm_calls_for_cost(
    parent_context: MutableMapping[str, Any],
    child_context: Mapping[str, Any] | None,
) -> None:
    """Accumulate child model usage without copying child prompts or outputs."""

    if not isinstance(child_context, Mapping):
        return
    child_calls = project_llm_calls_for_cost(child_context.get("llm_calls"))
    if not child_calls:
        return
    parent_calls = parent_context.get("llm_calls")
    if not isinstance(parent_calls, list):
        parent_calls = []
        parent_context["llm_calls"] = parent_calls
    for child_call in child_calls:
        if child_call not in parent_calls:
            parent_calls.append(child_call)


def build_durable_llm_usage_cost_summary(
    context: Mapping[str, Any] | None,
    *,
    model_registry: Any = None,
) -> dict[str, Any]:
    """Build one canonical summary for a durable workflow context."""

    calls = project_llm_calls_for_cost(
        context.get("llm_calls") if isinstance(context, Mapping) else None
    )
    return build_llm_usage_cost_summary(calls, model_registry=model_registry)


__all__ = [
    "LLM_USAGE_COST_SUMMARY_KEY",
    "build_durable_llm_usage_cost_summary",
    "merge_nested_llm_calls_for_cost",
    "project_llm_calls_for_cost",
]
