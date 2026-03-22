from src.backend.workflows.workflow_baseline_telemetry import (
    get_workflow_baseline_telemetry_snapshot,
    record_generic_fallback_mcp_invocation,
    record_mutation_guardrail_event,
    record_workflow_discovery_observation,
    record_write_policy_decision,
)


def test_workflow_discovery_observation_updates_ratio():
    before = get_workflow_baseline_telemetry_snapshot()
    before_queries = int(before.get("workflow_discovery_queries_total", 0))
    before_matches = int(before.get("workflow_discovery_matches_total", 0))
    before_exec = int(before.get("workflow_discovery_executable_matches_total", 0))

    record_workflow_discovery_observation(
        discovered_match_count=4,
        executable_match_count=3,
    )

    after = get_workflow_baseline_telemetry_snapshot()
    assert int(after.get("workflow_discovery_queries_total", 0)) == before_queries + 1
    assert int(after.get("workflow_discovery_matches_total", 0)) == before_matches + 4
    assert int(after.get("workflow_discovery_executable_matches_total", 0)) == (
        before_exec + 3
    )
    ratio = after.get("workflow_discovery_executable_hit_ratio")
    assert ratio is None or (0.0 <= float(ratio) <= 1.0)


def test_fallback_and_write_policy_counters_increment():
    before = get_workflow_baseline_telemetry_snapshot()
    before_invocations = int(before.get("generic_fallback_mcp_invocations_total", 0))
    before_allow = int(before.get("write_policy_allow_total", 0))
    before_deny = int(before.get("write_policy_deny_total", 0))

    record_generic_fallback_mcp_invocation(success=True)
    record_generic_fallback_mcp_invocation(success=False)
    record_write_policy_decision(stage="write_policy.decide", allowed_tools_count=1)
    record_write_policy_decision(stage="write_policy.decide", allowed_tools_count=0)

    after = get_workflow_baseline_telemetry_snapshot()
    assert int(after.get("generic_fallback_mcp_invocations_total", 0)) == (
        before_invocations + 2
    )
    assert int(after.get("write_policy_allow_total", 0)) == before_allow + 1
    assert int(after.get("write_policy_deny_total", 0)) == before_deny + 1

    stage_counts = after.get("write_policy_stage_counts", {})
    assert "write_policy.decide" in stage_counts


def test_mutation_guardrail_event_updates_counters_and_recent_history():
    before = get_workflow_baseline_telemetry_snapshot()
    before_total = int(before.get("mutation_guardrail_events_total", 0))

    record_mutation_guardrail_event(
        {
            "type": "mutation_guardrail",
            "guardrail_surface": "execution",
            "stage": "tool_execute",
            "tool_name": "delete_concept",
            "risk_class": "destructive",
            "decision": "approval_required",
        }
    )

    after = get_workflow_baseline_telemetry_snapshot()
    assert int(after.get("mutation_guardrail_events_total", 0)) == before_total + 1
    assert after.get("mutation_guardrail_decision_counts", {}).get(
        "approval_required", 0
    ) >= 1
    assert after.get("mutation_guardrail_surface_counts", {}).get("execution", 0) >= 1
    assert after.get("mutation_guardrail_risk_counts", {}).get("destructive", 0) >= 1
    recent = after.get("recent_mutation_guardrail_events", [])
    assert isinstance(recent, list)
    assert recent
