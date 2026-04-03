from __future__ import annotations

import json

from src.backend.services import (
    minimal_imposition_benchmark_profile_vontology_service as service,
)


def test_canonical_minimal_imposition_profile_concept_ids_are_deterministic() -> None:
    concept_ids = service.canonical_minimal_imposition_benchmark_profile_concept_ids()
    assert concept_ids == (
        "#V#minimal_imposition_benchmark_profile_autopilot_v1",
    )


def test_ensure_canonical_minimal_imposition_profiles_creates_and_links(
    monkeypatch,
) -> None:
    concept_docs: dict[str, dict] = {}
    upserts: list[dict[str, str]] = []

    def _mock_get_concept_by_concept_id(concept_id: str):
        return concept_docs.get(concept_id)

    def _mock_create_concept(**kwargs):
        concept_docs[kwargs["concept_id"]] = {
            "concept_id": kwargs["concept_id"],
            "relationships": {},
        }
        return {"concept_id": kwargs["concept_id"]}

    def _mock_upsert_singleton_text_relation(**kwargs):
        upserts.append(
            {
                "concept_id": kwargs["subject_concept_id"],
                "predicate": kwargs["predicate"],
                "text": kwargs["text"],
            }
        )
        return {"success": True}

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        _mock_get_concept_by_concept_id,
    )
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        _mock_create_concept,
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _mock_upsert_singleton_text_relation,
    )

    report = service.ensure_canonical_minimal_imposition_benchmark_profiles()

    assert report["success"] is True
    assert report["profile_type_created"] is True
    assert report["created_profile_concept_ids"] == [
        "#V#minimal_imposition_benchmark_profile_autopilot_v1"
    ]
    assert set(report["linked_workflow_ids"]) == {
        "#V#tool_calling_workflow",
        "#V#write_tool_policy_workflow",
    }
    texts_by_concept = {
        (item["concept_id"], item["predicate"]): item["text"] for item in upserts
    }
    assert (
        "#V#minimal_imposition_benchmark_profile_autopilot_v1",
        "#V#has_minimal_imposition_benchmark_profile_json",
    ) in texts_by_concept
    assert (
        "#V#tool_calling_workflow",
        "#V#has_minimal_imposition_benchmark_profile",
    ) in texts_by_concept


def test_load_minimal_imposition_profile_uses_workflow_link(monkeypatch) -> None:
    payload = {
        "profile_id": "autopilot_minimal_imposition_v1",
        "benchmark_surface_ids": ["turn_execution_build_benchmark"],
        "dimensions": [
            {
                "dimension_id": "user_interruption_burden",
                "formula_id": "turn_follow_up_rate_pct",
                "weight": 0.2,
                "evidence_kind": "direct",
                "ideal_max_pct": 5.0,
                "warning_max_pct": 10.0,
                "fail_max_pct": 20.0,
            }
        ],
        "scenario_families": [{"scenario_id": "low_risk_additive_internal_write"}],
        "composite_policy": {"policy_version": "minimal_imposition_cost.v1"},
    }

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}}
        if concept_id
        in {
            "#V#minimal_imposition_benchmark_profile_autopilot_v1",
            "#V#tool_calling_workflow",
        }
        else None,
    )

    def _mock_get_texts_for_concept(concept_id: str, predicate: str, limit: int = 1):
        if (
            concept_id == "#V#tool_calling_workflow"
            and predicate == "#V#has_minimal_imposition_benchmark_profile"
        ):
            return [{"text": "#V#minimal_imposition_benchmark_profile_autopilot_v1"}]
        if (
            concept_id == "#V#minimal_imposition_benchmark_profile_autopilot_v1"
            and predicate == "#V#has_minimal_imposition_benchmark_profile_json"
        ):
            return [{"text": json.dumps(payload)}]
        return []

    monkeypatch.setattr(service, "get_texts_for_concept", _mock_get_texts_for_concept)

    profile, diagnostics = service.load_minimal_imposition_benchmark_profile(
        workflow_id="#V#tool_calling_workflow"
    )

    assert profile is not None
    assert profile["profile_concept_id"] == (
        "#V#minimal_imposition_benchmark_profile_autopilot_v1"
    )
    assert profile["benchmark_surface_ids"] == ["turn_execution_build_benchmark"]
    assert profile["dimensions"][0]["dimension_id"] == "user_interruption_burden"
    assert (
        diagnostics["loaded_profile_concept_id"]
        == "#V#minimal_imposition_benchmark_profile_autopilot_v1"
    )
