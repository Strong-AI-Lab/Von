from __future__ import annotations

from src.backend.services import minimal_imposition_benchmark_service as service


def _profile() -> dict:
    return {
        "profile_concept_id": "#V#minimal_imposition_benchmark_profile_autopilot_v1",
        "profile_id": "autopilot_minimal_imposition_v1",
        "description": "test profile",
        "benchmark_surface_ids": ["turn_execution_build_benchmark"],
        "composite_policy": {"policy_version": "minimal_imposition_cost.v1"},
        "dimensions": [
            {
                "dimension_id": "user_interruption_burden",
                "title": "User interruption burden",
                "formula_id": "turn_follow_up_rate_pct",
                "weight": 0.4,
                "evidence_kind": "direct",
                "ideal_max_pct": 5.0,
                "warning_max_pct": 15.0,
                "fail_max_pct": 30.0,
            },
            {
                "dimension_id": "unsafe_under_escalation",
                "title": "Unsafe under-escalation",
                "formula_id": "turn_false_success_rate_pct",
                "weight": 0.4,
                "evidence_kind": "direct",
                "ideal_max_pct": 0.0,
                "warning_max_pct": 1.0,
                "fail_max_pct": 5.0,
            },
            {
                "dimension_id": "epistemic_intrusion",
                "title": "Epistemic intrusion",
                "formula_id": "missing_epistemic_intrusion_telemetry",
                "weight": 0.2,
                "evidence_kind": "missing",
                "ideal_max_pct": 0.0,
                "warning_max_pct": 0.0,
                "fail_max_pct": 0.0,
            },
        ],
        "scenario_families": [{"scenario_id": "low_risk_additive_internal_write"}],
    }


def test_build_minimal_imposition_assessment_reports_direct_proxy_and_missing(monkeypatch):
    monkeypatch.setattr(
        service,
        "load_minimal_imposition_benchmark_profile",
        lambda **_: (_profile(), {"loaded_profile_concept_id": _profile()["profile_concept_id"]}),
    )

    report = service.build_minimal_imposition_assessment(
        turn_execution_metrics={
            "user_imposition_metrics": {
                "follow_up_turn_rate_pct": 12.5,
                "escalation_signal_rate_pct": 7.0,
            },
            "false_success_rate_pct": 0.0,
            "unresolved_follow_up_rate_pct": 12.5,
        }
    )

    assert report["success"] is True
    assert report["profile"]["profile_id"] == "autopilot_minimal_imposition_v1"
    assert report["dimension_counts"]["direct_count"] == 2
    assert report["dimension_counts"]["missing_count"] == 1
    assert report["weight_coverage"]["available_weight"] == 0.8
    assert report["weighted_cost_pct"] is not None
    assert report["weighted_score_pct"] is not None
    assert any(
        row.get("dimension_id") == "epistemic_intrusion" and row.get("status") == "missing"
        for row in report["dimension_rows"]
    )


def test_build_minimal_imposition_assessment_reports_missing_profile(monkeypatch):
    monkeypatch.setattr(
        service,
        "load_minimal_imposition_benchmark_profile",
        lambda **_: (
            None,
            {"missing_profile_concept_ids": ["#V#minimal_imposition_benchmark_profile_autopilot_v1"]},
        ),
    )

    report = service.build_minimal_imposition_assessment(
        turn_execution_metrics={"false_success_rate_pct": 10.0}
    )

    assert report["success"] is False
    assert report["error_code"] == "minimal_imposition_profile_missing"
    assert report["recommendations"][0]["recommendation_id"] == (
        "materialise_minimal_imposition_profile"
    )
