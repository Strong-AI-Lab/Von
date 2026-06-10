"""Tests for the per-turn decision-attribution projection (JVNAUTOSCI-2499)."""

from __future__ import annotations

from src.backend.services.turn_decision_attribution_service import (
    AUTHORITY_ABSENT,
    AUTHORITY_PYTHON_FALLBACK,
    AUTHORITY_REPRESENTED,
    AUTHORITY_SETTINGS_DEFAULT,
    AUTHORITY_UNKNOWN,
    DECISION_KINDS,
    TURN_DECISION_ATTRIBUTION_SCHEMA_VERSION,
    build_turn_decision_attribution,
)


def _fully_represented_diagnostics() -> dict:
    return {
        "workflow_discovery": {
            "discovery_payload_origin": "durable_action_discover_workflows_for_turn",
            "query_source": "user_prompt",
            "candidate_count": 3,
            "candidates": [{"concept_id": "#V#paper_workflow"}],
        },
        "workflow_routing": {
            "selected_workflow_id": "#V#paper_workflow",
            "selector_source": "selector_llm_verdict",
            "selection_rationale": "selector_verdict",
        },
        "workflow_model_policy": {
            "policy_source": "graph",
            "graph_completeness": "graph_complete",
            "policy_id": "#V#default_workflow_model_policy",
        },
        "completion_gate": {
            "verdict_source": "postcondition_critic_workflow",
            "status": "passed",
        },
    }


def _dispatch_preflight_event(*, override: bool) -> dict:
    return {
        "decision_authority_origin": "python",
        "stage": "workflow_dispatch",
        "component": "internal_mcp_orchestrator",
        "function": "record_turn_contract_dispatch_preflight",
        "decision_class": "workflow_dispatch_turn_contract_check",
        "decision_source": "turn_expected_outcome_contract",
        "changed_outcome": override,
        "reason_code": (
            "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
            if override
            else "selected_workflow_satisfies_contract"
        ),
    }


def test_fully_represented_turn_scores_one():
    payload = build_turn_decision_attribution(
        diagnostics=_fully_represented_diagnostics(),
        aux_entries=[_dispatch_preflight_event(override=False)],
    )

    assert payload["schema_version"] == TURN_DECISION_ATTRIBUTION_SCHEMA_VERSION
    breakdown = payload["summary"]["decision_kind_breakdown"]
    assert set(breakdown) == set(DECISION_KINDS)
    assert breakdown["discovery"] == AUTHORITY_REPRESENTED
    assert breakdown["selection"] == AUTHORITY_REPRESENTED
    assert breakdown["dispatch"] == AUTHORITY_REPRESENTED
    assert breakdown["model_choice"] == AUTHORITY_REPRESENTED
    assert breakdown["recovery"] == AUTHORITY_ABSENT
    assert breakdown["acceptance"] == AUTHORITY_REPRESENTED
    assert payload["summary"]["architecture_integrity_score"] == 1.0
    assert payload["summary"]["python_fallback_count"] == 0

    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["selection"]["concept_ids"] == ["#V#paper_workflow"]
    assert by_kind["model_choice"]["concept_ids"] == [
        "#V#default_workflow_model_policy"
    ]


def test_python_fallback_selection_drops_score_and_names_location():
    diagnostics = _fully_represented_diagnostics()
    diagnostics["workflow_routing"] = {
        "selected_workflow_id": "#V#paper_workflow",
        "selector_source": "durable_discovery_fallback",
        "selection_rationale": "first_routing_match",
    }

    payload = build_turn_decision_attribution(
        diagnostics=diagnostics, aux_entries=[]
    )

    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["selection"]["authority"] == AUTHORITY_PYTHON_FALLBACK
    assert (
        by_kind["selection"]["evidence"]["selector_source"]
        == "durable_discovery_fallback"
    )
    score = payload["summary"]["architecture_integrity_score"]
    assert score is not None and score < 1.0


def test_dispatch_override_is_python_fallback_with_event_record():
    payload = build_turn_decision_attribution(
        diagnostics=_fully_represented_diagnostics(),
        aux_entries=[_dispatch_preflight_event(override=True)],
    )

    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    dispatch = by_kind["dispatch"]
    assert dispatch["authority"] == AUTHORITY_PYTHON_FALLBACK
    events = dispatch["python_decision_events"]
    assert events and events[0]["changed_outcome"] is True
    assert events[0]["function"] == "record_turn_contract_dispatch_preflight"


def test_model_choice_classifications():
    base = _fully_represented_diagnostics()

    base["workflow_model_policy"] = {
        "policy_source": "json",
        "policy_id": "#V#default_workflow_model_policy",
    }
    payload = build_turn_decision_attribution(diagnostics=base, aux_entries=[])
    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["model_choice"]["authority"] == AUTHORITY_REPRESENTED
    assert (
        by_kind["model_choice"]["authority_surface"]
        == "vontology_model_policy_json_text_relation"
    )

    base["workflow_model_policy"] = {"policy_source": "disabled"}
    payload = build_turn_decision_attribution(diagnostics=base, aux_entries=[])
    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["model_choice"]["authority"] == AUTHORITY_SETTINGS_DEFAULT

    del base["workflow_model_policy"]
    payload = build_turn_decision_attribution(diagnostics=base, aux_entries=[])
    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["model_choice"]["authority"] == AUTHORITY_UNKNOWN


def test_model_policy_telemetry_found_in_nested_containers():
    diagnostics = _fully_represented_diagnostics()
    telemetry = diagnostics.pop("workflow_model_policy")
    diagnostics["selected_workflow_trace"] = {
        "metadata": {"workflow_model_policy": telemetry}
    }

    payload = build_turn_decision_attribution(
        diagnostics=diagnostics, aux_entries=[]
    )
    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["model_choice"]["authority"] == AUTHORITY_REPRESENTED


def test_recovery_markers_classified_python_fallback():
    diagnostics = _fully_represented_diagnostics()
    diagnostics["workflow_routing"]["selection_rationale"] = (
        "single_specialised_candidate_recovery_from_selector_fallback"
    )

    payload = build_turn_decision_attribution(
        diagnostics=diagnostics, aux_entries=[]
    )
    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["recovery"]["authority"] == AUTHORITY_PYTHON_FALLBACK
    # The recovery rationale also makes the selection python-attributed.
    assert by_kind["selection"]["authority"] == AUTHORITY_PYTHON_FALLBACK


def test_no_match_discovery_is_absent_not_fallback():
    diagnostics = _fully_represented_diagnostics()
    diagnostics["workflow_discovery"] = {
        "discovery_payload_origin": "durable_action_no_match_fallback",
        "candidate_count": 0,
        "match_absence_reason": "durable_workflow_discovery_no_match",
    }

    payload = build_turn_decision_attribution(
        diagnostics=diagnostics, aux_entries=[]
    )
    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["discovery"]["authority"] == AUTHORITY_ABSENT


def test_empty_diagnostics_yields_no_score():
    payload = build_turn_decision_attribution(diagnostics={}, aux_entries=None)

    summary = payload["summary"]
    assert summary["attributable_decision_count"] == 0
    assert summary["architecture_integrity_score"] is None
    breakdown = summary["decision_kind_breakdown"]
    assert breakdown["discovery"] == AUTHORITY_ABSENT
    assert breakdown["selection"] == AUTHORITY_ABSENT
    assert breakdown["model_choice"] == AUTHORITY_UNKNOWN


def test_unclassified_values_report_unknown_not_guess():
    diagnostics = {
        "workflow_routing": {
            "selected_workflow_id": "#V#paper_workflow",
        },
    }
    payload = build_turn_decision_attribution(
        diagnostics=diagnostics, aux_entries=[]
    )
    by_kind = {d["decision_kind"]: d for d in payload["decisions"]}
    assert by_kind["selection"]["authority"] == AUTHORITY_UNKNOWN
    assert by_kind["dispatch"]["authority"] == AUTHORITY_UNKNOWN


def test_aggregate_reports_mean_score_and_fallback_signatures():
    from src.backend.services.turn_decision_attribution_service import (
        TURN_DECISION_ATTRIBUTION_AGGREGATE_SCHEMA_VERSION,
        aggregate_turn_decision_attributions,
    )

    clean = build_turn_decision_attribution(
        diagnostics=_fully_represented_diagnostics(),
        aux_entries=[_dispatch_preflight_event(override=False)],
    )
    fallback_diagnostics = _fully_represented_diagnostics()
    fallback_diagnostics["workflow_routing"] = {
        "selected_workflow_id": "#V#paper_workflow",
        "selector_source": "durable_discovery_fallback",
        "selection_rationale": "first_routing_match",
    }
    rescued = build_turn_decision_attribution(
        diagnostics=fallback_diagnostics,
        aux_entries=[_dispatch_preflight_event(override=True)],
    )

    aggregate = aggregate_turn_decision_attributions([clean, rescued])

    assert (
        aggregate["schema_version"]
        == TURN_DECISION_ATTRIBUTION_AGGREGATE_SCHEMA_VERSION
    )
    assert aggregate["turn_count"] == 2
    assert aggregate["scored_turn_count"] == 2
    mean_score = aggregate["mean_architecture_integrity_score"]
    assert mean_score is not None and 0.0 < mean_score < 1.0

    kind_counts = aggregate["decision_kind_authority_counts"]
    assert kind_counts["selection"][AUTHORITY_REPRESENTED] == 1
    assert kind_counts["selection"][AUTHORITY_PYTHON_FALLBACK] == 1
    assert kind_counts["dispatch"][AUTHORITY_PYTHON_FALLBACK] == 1

    signatures = aggregate["python_fallback_signatures"]
    assert any(key.startswith("selection:") for key in signatures)
    assert any(key.startswith("dispatch:") for key in signatures)


def test_aggregate_of_nothing_is_empty_not_error():
    from src.backend.services.turn_decision_attribution_service import (
        aggregate_turn_decision_attributions,
    )

    aggregate = aggregate_turn_decision_attributions([])
    assert aggregate["turn_count"] == 0
    assert aggregate["mean_architecture_integrity_score"] is None
    assert aggregate["python_fallback_signatures"] == {}


def test_embedded_debug_shape_is_recognised():
    """The chat-history embedded shape (workflow_routing_diagnostics,
    completion_gate_verdict, llm_debug container) attributes equivalently."""

    diagnostics = {
        "workflow_routing_diagnostics": {
            "selected_workflow_id": "#V#paper_workflow",
            "selector_source": "selector_llm_verdict",
        },
        "workflow_discovery": {
            "discovery_payload_origin": (
                "durable_action_discover_workflows_for_turn"
            ),
            "candidate_count": 2,
        },
        "llm_debug": {
            "workflow_model_policy": {
                "policy_source": "graph",
                "graph_completeness": "graph_complete",
                "policy_id": "#V#default_workflow_model_policy",
            },
            "completion_gate_verdict": {
                "verdict_source": "postcondition_critic_workflow",
                "status": "passed",
            },
        },
    }

    payload = build_turn_decision_attribution(
        diagnostics=diagnostics, aux_entries=[]
    )
    breakdown = payload["summary"]["decision_kind_breakdown"]
    assert breakdown["selection"] == AUTHORITY_REPRESENTED
    assert breakdown["model_choice"] == AUTHORITY_REPRESENTED
    assert breakdown["acceptance"] == AUTHORITY_REPRESENTED
    assert payload["summary"]["architecture_integrity_score"] == 1.0
