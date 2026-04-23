from __future__ import annotations

import json

from src.backend.services import (
    episode_self_improvement_profile_vontology_service as service,
)


def test_canonical_episode_self_improvement_profile_concept_ids_are_deterministic() -> None:
    concept_ids = service.canonical_episode_self_improvement_profile_concept_ids()
    assert concept_ids == (
        "#V#episode_self_improvement_profile_workflow_revision_default",
    )


def test_ensure_canonical_episode_self_improvement_profiles_creates_and_links(
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

    report = service.ensure_canonical_episode_self_improvement_profiles()

    assert report["success"] is True
    assert report["profile_type_created"] is True
    assert report["created_profile_concept_ids"] == [
        "#V#episode_self_improvement_profile_workflow_revision_default"
    ]
    assert report["linked_workflow_ids"] == [
        "#V#episode_evaluation_workflow",
        "#V#episode_self_improvement_proposal_workflow",
        "#V#episode_self_improvement_promotion_workflow",
    ]
    texts_by_concept = {
        (item["concept_id"], item["predicate"]): item["text"] for item in upserts
    }
    assert (
        "#V#episode_self_improvement_profile_workflow_revision_default",
        "#V#has_episode_self_improvement_profile_json",
    ) in texts_by_concept
    assert (
        "#V#episode_evaluation_workflow",
        "#V#has_episode_self_improvement_profile",
    ) in texts_by_concept


def test_load_episode_self_improvement_profile_uses_workflow_link(monkeypatch) -> None:
    payload = {
        "profile_id": "workflow_revision_default",
        "candidate_selection_policy": {
            "policy_version": "episode_self_improvement.candidate_selection.v1",
            "max_candidate_launches": 2,
            "priority_order": ["medium", "high", "low"],
            "eligible_target_surfaces": ["workflow"],
            "dedupe_identity_fields_by_surface": {
                "workflow": ["target_workflow_id"],
            },
        },
        "benchmark_policy": {
            "policy_version": "episode_self_improvement.benchmark.v1",
            "scan_limit": 17,
            "max_audit_cases": 2,
        },
    }

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}}
        if concept_id
        in (
            "#V#episode_self_improvement_profile_workflow_revision_default",
            "#V#episode_evaluation_workflow",
        )
        else None,
    )

    def _mock_get_texts_for_concept(concept_id: str, predicate: str, limit: int = 1):
        if (
            concept_id == "#V#episode_evaluation_workflow"
            and predicate == "#V#has_episode_self_improvement_profile"
        ):
            return [
                {
                    "text": "#V#episode_self_improvement_profile_workflow_revision_default"
                }
            ]
        if (
            concept_id == "#V#episode_self_improvement_profile_workflow_revision_default"
            and predicate == "#V#has_episode_self_improvement_profile_json"
        ):
            return [{"text": json.dumps(payload)}]
        return []

    monkeypatch.setattr(service, "get_texts_for_concept", _mock_get_texts_for_concept)

    profile, diagnostics = service.load_episode_self_improvement_profile(
        workflow_id="#V#episode_evaluation_workflow"
    )

    assert profile is not None
    assert profile["profile_concept_id"] == (
        "#V#episode_self_improvement_profile_workflow_revision_default"
    )
    assert profile["candidate_selection_policy"]["max_candidate_launches"] == 2
    assert profile["candidate_selection_policy"]["priority_order"] == [
        "medium",
        "high",
        "low",
    ]
    assert profile["benchmark_policy"]["scan_limit"] == 17
    assert profile["benchmark_policy"]["max_audit_cases"] == 2
    assert (
        diagnostics["loaded_profile_concept_id"]
        == "#V#episode_self_improvement_profile_workflow_revision_default"
    )


def test_load_episode_self_improvement_profile_rejects_invalid_policy(
    monkeypatch,
) -> None:
    payload = {
        "profile_id": "workflow_revision_default",
        "candidate_selection_policy": {
            "policy_version": "episode_self_improvement.candidate_selection.v1",
            "priority_order": ["high", "medium", "low"],
            "eligible_target_surfaces": ["workflow"],
            "dedupe_identity_fields_by_surface": {
                "workflow": ["target_workflow_id"],
            },
        },
        "benchmark_policy": {
            "policy_version": "episode_self_improvement.benchmark.v1",
            "scan_limit": 17,
            "max_audit_cases": 2,
        },
    }

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}}
        if concept_id
        in (
            "#V#episode_self_improvement_profile_workflow_revision_default",
            "#V#episode_evaluation_workflow",
        )
        else None,
    )

    def _mock_get_texts_for_concept(concept_id: str, predicate: str, limit: int = 1):
        if (
            concept_id == "#V#episode_evaluation_workflow"
            and predicate == "#V#has_episode_self_improvement_profile"
        ):
            return [
                {
                    "text": "#V#episode_self_improvement_profile_workflow_revision_default"
                }
            ]
        if (
            concept_id == "#V#episode_self_improvement_profile_workflow_revision_default"
            and predicate == "#V#has_episode_self_improvement_profile_json"
        ):
            return [{"text": json.dumps(payload)}]
        return []

    monkeypatch.setattr(service, "get_texts_for_concept", _mock_get_texts_for_concept)

    profile, diagnostics = service.load_episode_self_improvement_profile(
        workflow_id="#V#episode_evaluation_workflow"
    )

    assert profile is None
    assert diagnostics["error_code"] == "episode_self_improvement_profile_invalid"
    assert "invalid_max_candidate_launches" in diagnostics["validation_errors"]
