"""Baseline workflow telemetry counters for WS0 inventory reporting.

These counters are intentionally lightweight, process-local, and additive.
They provide a quick baseline for parity and execution-path diagnostics:
1. Workflow discovery executable hit ratio.
2. Generic fallback MCP invocation volume and outcomes.
3. Write-policy allow/deny decisions grouped by stage.
"""

from __future__ import annotations

import copy
from threading import Lock
from typing import Any, Dict, Mapping

_lock = Lock()
_state: Dict[str, Any] = {
    "workflow_discovery_queries_total": 0,
    "workflow_discovery_matches_total": 0,
    "workflow_discovery_executable_matches_total": 0,
    "workflow_discovery_budget_exhausted_total": 0,
    "workflow_discovery_budget_exhaustion_stage_counts": {},
    "workflow_discovery_advisory_crossing_total": 0,
    "workflow_discovery_advisory_crossing_stage_counts": {},
    "workflow_discovery_timeout_budget_seconds_sum": 0.0,
    "workflow_discovery_timeout_budget_observation_count": 0,
    "generic_fallback_mcp_invocations_total": 0,
    "generic_fallback_mcp_invocations_success": 0,
    "generic_fallback_mcp_invocations_failed": 0,
    "write_policy_allow_total": 0,
    "write_policy_deny_total": 0,
    "write_policy_stage_counts": {},
    "mutation_guardrail_events_total": 0,
    "mutation_guardrail_decision_counts": {},
    "mutation_guardrail_surface_counts": {},
    "mutation_guardrail_risk_counts": {},
    "mutation_guardrail_stage_counts": {},
    "recent_mutation_guardrail_events": [],
}


def _get_stage_bucket(stage: str) -> Dict[str, int]:
    stage_counts = _state.get("write_policy_stage_counts")
    if not isinstance(stage_counts, dict):
        stage_counts = {}
        _state["write_policy_stage_counts"] = stage_counts
    bucket = stage_counts.get(stage)
    if not isinstance(bucket, dict):
        bucket = {"allow": 0, "deny": 0}
        stage_counts[stage] = bucket
    return bucket


def _increment_bucket(name: str, key: str) -> None:
    bucket = _state.get(name)
    if not isinstance(bucket, dict):
        bucket = {}
        _state[name] = bucket
    cleaned = key.strip() if isinstance(key, str) and key.strip() else "unknown"
    bucket[cleaned] = int(bucket.get(cleaned, 0)) + 1


def record_workflow_discovery_observation(
    *,
    discovered_match_count: int,
    executable_match_count: int,
    budget_exhausted: bool = False,
    budget_exhaustion_stage: str | None = None,
    timeout_budget_seconds: float | None = None,
    elapsed_time_enforcement: str = "hard",
) -> None:
    """Record workflow discovery counters for one query."""
    safe_discovered = max(0, int(discovered_match_count))
    safe_executable = max(0, int(executable_match_count))
    safe_timeout_budget_seconds: float | None = None
    if timeout_budget_seconds is not None:
        try:
            safe_timeout_budget_seconds = max(0.0, float(timeout_budget_seconds))
        except Exception:
            safe_timeout_budget_seconds = None

    with _lock:
        _state["workflow_discovery_queries_total"] += 1
        _state["workflow_discovery_matches_total"] += safe_discovered
        _state["workflow_discovery_executable_matches_total"] += min(
            safe_discovered, safe_executable
        )
        if bool(budget_exhausted):
            if str(elapsed_time_enforcement).strip().lower() == "advisory":
                _state["workflow_discovery_advisory_crossing_total"] += 1
                _increment_bucket(
                    "workflow_discovery_advisory_crossing_stage_counts",
                    budget_exhaustion_stage or "unknown",
                )
            else:
                _state["workflow_discovery_budget_exhausted_total"] += 1
                _increment_bucket(
                    "workflow_discovery_budget_exhaustion_stage_counts",
                    budget_exhaustion_stage or "unknown",
                )
        if safe_timeout_budget_seconds is not None:
            _state[
                "workflow_discovery_timeout_budget_seconds_sum"
            ] += safe_timeout_budget_seconds
            _state["workflow_discovery_timeout_budget_observation_count"] += 1


def record_generic_fallback_mcp_invocation(*, success: bool) -> None:
    """Record one generic fallback MCP invocation."""
    with _lock:
        _state["generic_fallback_mcp_invocations_total"] += 1
        if success:
            _state["generic_fallback_mcp_invocations_success"] += 1
        else:
            _state["generic_fallback_mcp_invocations_failed"] += 1


def record_write_policy_decision(
    *,
    stage: str,
    allowed_tools_count: int,
) -> None:
    """Record one write-policy decision with stage-level aggregation."""
    bucket_stage = (
        stage.strip() if isinstance(stage, str) and stage.strip() else "unknown"
    )
    is_allow = int(allowed_tools_count) > 0

    with _lock:
        if is_allow:
            _state["write_policy_allow_total"] += 1
        else:
            _state["write_policy_deny_total"] += 1

        bucket = _get_stage_bucket(bucket_stage)
        if is_allow:
            bucket["allow"] = int(bucket.get("allow", 0)) + 1
        else:
            bucket["deny"] = int(bucket.get("deny", 0)) + 1


def record_mutation_guardrail_event(event: Mapping[str, Any]) -> None:
    """Record one structured mutation-guardrail encounter."""

    if not isinstance(event, Mapping):
        return

    decision = str(event.get("decision") or "").strip() or "unknown"
    surface = str(event.get("guardrail_surface") or "").strip() or "unknown"
    risk_class = str(event.get("risk_class") or "").strip() or "unknown"
    stage = str(event.get("stage") or "").strip() or "unknown"
    event_copy = {
        str(key): value for key, value in event.items() if isinstance(key, str)
    }

    with _lock:
        _state["mutation_guardrail_events_total"] += 1
        _increment_bucket("mutation_guardrail_decision_counts", decision)
        _increment_bucket("mutation_guardrail_surface_counts", surface)
        _increment_bucket("mutation_guardrail_risk_counts", risk_class)
        _increment_bucket("mutation_guardrail_stage_counts", stage)

        recent = _state.get("recent_mutation_guardrail_events")
        if not isinstance(recent, list):
            recent = []
            _state["recent_mutation_guardrail_events"] = recent
        recent.append(event_copy)
        if len(recent) > 100:
            del recent[:-100]


def get_workflow_baseline_telemetry_snapshot() -> Dict[str, Any]:
    """Return a deep-copied baseline telemetry snapshot with derived ratios."""
    with _lock:
        snapshot = copy.deepcopy(_state)

    matches_total = int(snapshot.get("workflow_discovery_matches_total", 0))
    executable_total = int(
        snapshot.get("workflow_discovery_executable_matches_total", 0)
    )
    queries_total = int(snapshot.get("workflow_discovery_queries_total", 0))
    budget_exhausted_total = int(
        snapshot.get("workflow_discovery_budget_exhausted_total", 0)
    )
    advisory_crossing_total = int(
        snapshot.get("workflow_discovery_advisory_crossing_total", 0)
    )
    timeout_budget_observations = int(
        snapshot.get("workflow_discovery_timeout_budget_observation_count", 0)
    )
    timeout_budget_sum = float(
        snapshot.get("workflow_discovery_timeout_budget_seconds_sum", 0.0)
    )
    invocations_total = int(snapshot.get("generic_fallback_mcp_invocations_total", 0))
    invocations_success = int(
        snapshot.get("generic_fallback_mcp_invocations_success", 0)
    )

    snapshot["workflow_discovery_executable_hit_ratio"] = (
        (executable_total / matches_total) if matches_total > 0 else None
    )
    snapshot["workflow_discovery_budget_exhausted_ratio"] = (
        (budget_exhausted_total / queries_total) if queries_total > 0 else None
    )
    snapshot["workflow_discovery_advisory_crossing_ratio"] = (
        (advisory_crossing_total / queries_total) if queries_total > 0 else None
    )
    snapshot["workflow_discovery_timeout_budget_seconds_avg"] = (
        (timeout_budget_sum / timeout_budget_observations)
        if timeout_budget_observations > 0
        else None
    )
    snapshot["generic_fallback_mcp_success_ratio"] = (
        (invocations_success / invocations_total) if invocations_total > 0 else None
    )
    return snapshot
