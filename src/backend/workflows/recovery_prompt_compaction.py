"""Compaction of turn-execution recovery-decision LLM context.

JVNAUTOSCI-2514. The conversation-turn ``recovery_decision`` step inlines the
whole accumulated turn state -- the selected-workflow trace, the workflow
discovery result, completion-gate evidence, prior recovery attempts, and a long
tail of guidance fields -- into a single system prompt via the step's
``llm_policy.context_fields``. Most of that payload is "just in case" bulk: the
selected-workflow trace alone embeds a second full copy of the discovery
result, and the discovery candidates carry verbose descriptions and routing
internals that the recovery decision never needs. For one real incident
(request ``2ea8b384``) the composed prompt reached 150,704 chars (~37K tokens)
to drive a 206-char decision; prefilling that on the local ``qwen3:8b`` model is
the dominant cost of every recovery call.

This module is runtime prompt-shaping support, not recovery policy. It drops
duplicated/internal telemetry sub-payloads that are available elsewhere or are
execution bookkeeping, truncates oversized values, and enforces hard per-field
and total character budgets so the recovery prompt can never balloon again,
regardless of how large the live turn state grows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

RECOVERY_DECISION_STATE_ID = "recovery_decision"

# Per-field and total character budgets for the recovery-decision prompt's
# context sections. The base instruction prompt (~6 KB) and response contract
# are composed separately and are never truncated by these budgets; only the
# inlined evidence fields are bounded here.
RECOVERY_CONTEXT_FIELD_CHAR_BUDGET = 4000
RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET = 20000

# Recursive compaction thresholds applied within each context field value.
_MAX_STRING_CHARS = 600
_MAX_LIST_ITEMS = 6
_MAX_DEPTH = 6

_STRING_TRUNCATION_SUFFIX = "...[+{count} chars omitted]"
_LIST_TRUNCATION_ITEM = "...[+{count} more items]"
_DEPTH_TRUNCATION_MARKER = "...[truncated: nested too deep]"
FIELD_TRUNCATION_MARKER = "\n...[recovery evidence truncated to fit prompt budget]"
TOTAL_TRUNCATION_MARKER = "[remaining recovery evidence omitted to fit prompt budget]"

# Keys dropped anywhere inside a recovery context value. These are duplicate
# payloads already supplied by dedicated context fields, selector bookkeeping, or
# low-level timing/cache telemetry. The support surface deliberately preserves
# payloads that are not listed here, including failed tools, failure codes,
# unresolved preconditions, missing/required tools, execution summaries, and
# selected workflow IDs.
_DROP_KEYS = frozenset(
    {
        # Whole-payload duplicates already supplied as their own context fields.
        "workflow_discovery_result",
        "expected_outcome_contract",
        "expected_outcome_contract_state",
        # Selector provenance / fast-path bookkeeping: not needed to choose the
        # next recovery action.
        "selector_context_lineage",
        "selector_represented_fast_path",
        "selector_selection_metadata",
        "selector_fast_path_policy",
        "selector_fast_path_policy_diagnostics",
        # Per-candidate routing internals; the decision uses ids + verdicts.
        "routing_index_metadata",
        "routing_readiness_detail",
        "routing_readiness_diagnostics",
        "routing_profile",
        "executability_detail",
        # Caches and fine-grained timing telemetry.
        "workflow_discovery_cache",
        "stage_timings",
    }
)


@dataclass(frozen=True)
class RecoveryContextCompactionResult:
    value: Any
    diagnostics: dict[str, Any]


def is_recovery_decision_state(workflow_state_id: Any) -> bool:
    """Return True when the workflow state is the recovery-decision step."""

    return str(workflow_state_id or "").strip().lower() == RECOVERY_DECISION_STATE_ID


def _new_stats() -> dict[str, Any]:
    return {
        "changed": False,
        "dropped_key_count": 0,
        "dropped_keys": {},
        "truncated_string_count": 0,
        "truncated_string_omitted_chars": 0,
        "truncated_list_count": 0,
        "truncated_list_omitted_items": 0,
        "depth_truncation_count": 0,
    }


def _record_dropped_key(stats: dict[str, Any], key: str) -> None:
    stats["changed"] = True
    stats["dropped_key_count"] = int(stats.get("dropped_key_count") or 0) + 1
    dropped_keys = stats.get("dropped_keys")
    if not isinstance(dropped_keys, dict):
        dropped_keys = {}
        stats["dropped_keys"] = dropped_keys
    dropped_keys[key] = int(dropped_keys.get(key) or 0) + 1


def _compact_value(value: Any, *, depth: int, stats: dict[str, Any]) -> Any:
    if depth > _MAX_DEPTH:
        stats["changed"] = True
        stats["depth_truncation_count"] = (
            int(stats.get("depth_truncation_count") or 0) + 1
        )
        return _DEPTH_TRUNCATION_MARKER
    if isinstance(value, Mapping):
        compacted: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key in _DROP_KEYS:
                _record_dropped_key(stats, key)
                continue
            compacted[key] = _compact_value(
                raw_value,
                depth=depth + 1,
                stats=stats,
            )
        return compacted
    if isinstance(value, (list, tuple)):
        items = [
            _compact_value(item, depth=depth + 1, stats=stats)
            for item in list(value)[:_MAX_LIST_ITEMS]
        ]
        extra = len(value) - _MAX_LIST_ITEMS
        if extra > 0:
            stats["changed"] = True
            stats["truncated_list_count"] = (
                int(stats.get("truncated_list_count") or 0) + 1
            )
            stats["truncated_list_omitted_items"] = (
                int(stats.get("truncated_list_omitted_items") or 0) + extra
            )
            items.append(_LIST_TRUNCATION_ITEM.format(count=extra))
        return items
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            omitted = len(value) - _MAX_STRING_CHARS
            stats["changed"] = True
            stats["truncated_string_count"] = (
                int(stats.get("truncated_string_count") or 0) + 1
            )
            stats["truncated_string_omitted_chars"] = (
                int(stats.get("truncated_string_omitted_chars") or 0) + omitted
            )
            return value[
                :_MAX_STRING_CHARS
            ].rstrip() + _STRING_TRUNCATION_SUFFIX.format(count=omitted)
        return value
    return value


def compact_recovery_context_value(value: Any) -> Any:
    """Return a structurally compacted copy of a recovery context value.

    Drops duplicated/internal sub-payloads, truncates long strings, and caps
    long lists. Returns the same scalar for non-container inputs.
    """

    return compact_recovery_context_field(value).value


def compact_recovery_context_field(value: Any) -> RecoveryContextCompactionResult:
    """Return compacted recovery context plus projection diagnostics."""

    stats = _new_stats()
    compacted = _compact_value(value, depth=0, stats=stats)
    stats.update(
        {
            "schema_version": "recovery_context_field_compaction.v1",
            "max_string_chars": _MAX_STRING_CHARS,
            "max_list_items": _MAX_LIST_ITEMS,
            "max_depth": _MAX_DEPTH,
        }
    )
    return RecoveryContextCompactionResult(value=compacted, diagnostics=stats)


def render_compacted_recovery_field_text(
    serialised_text: str,
    *,
    char_budget: int,
) -> str:
    """Apply the per-field character cap to an already-serialised section body."""

    if char_budget <= 0:
        return ""
    if len(serialised_text) <= char_budget:
        return serialised_text
    if char_budget <= len(FIELD_TRUNCATION_MARKER):
        return FIELD_TRUNCATION_MARKER[:char_budget]
    body_budget = char_budget - len(FIELD_TRUNCATION_MARKER)
    return serialised_text[:body_budget].rstrip() + FIELD_TRUNCATION_MARKER
