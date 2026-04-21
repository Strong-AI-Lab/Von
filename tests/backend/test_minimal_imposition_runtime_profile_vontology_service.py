from __future__ import annotations

import json

from src.backend.services import (
    minimal_imposition_runtime_profile_vontology_service as service,
)


def test_canonical_minimal_imposition_runtime_profile_ids_are_deterministic() -> None:
    concept_ids = service.canonical_minimal_imposition_runtime_profile_concept_ids()
    assert concept_ids == ("#V#minimal_imposition_runtime_profile_write_policy_v1",)


def test_ensure_canonical_minimal_imposition_runtime_profiles_creates_and_links(
    monkeypatch,
) -> None:
    concept_docs: dict[str, dict] = {
        "#V#write_tool_policy_workflow": {
            "concept_id": "#V#write_tool_policy_workflow",
            "relationships": {},
        }
    }
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

    report = service.ensure_canonical_minimal_imposition_runtime_profiles()

    assert report["success"] is True
    assert report["profile_type_created"] is True
    assert report["created_profile_concept_ids"] == [
        "#V#minimal_imposition_runtime_profile_write_policy_v1"
    ]
    assert report["linked_workflow_ids"] == ["#V#write_tool_policy_workflow"]
    texts_by_concept = {
        (item["concept_id"], item["predicate"]): item["text"] for item in upserts
    }
    assert (
        "#V#minimal_imposition_runtime_profile_write_policy_v1",
        "#V#has_minimal_imposition_runtime_profile_json",
    ) in texts_by_concept
    assert (
        "#V#write_tool_policy_workflow",
        "#V#has_minimal_imposition_runtime_profile",
    ) in texts_by_concept


def test_load_minimal_imposition_runtime_profile_uses_workflow_link(monkeypatch) -> None:
    payload = {
        "profile_id": "write_policy_runtime_v1",
        "decision_policy": {
            "policy_version": "minimal_imposition_runtime.v1",
            "require_clear_request_for_recoverable_mutation": True,
        },
        "tool_risk_classes": {
            "download_paper": "additive_low_risk",
            "delete_concept": "destructive",
        },
        "scenario_policies": [{"scenario_id": "reversible_update"}],
        "tool_feature_overrides": {
            "workflow_bind_event": {
                "blast_radius": "high",
                "process_sensitive": True,
            }
        },
    }

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}}
        if concept_id
        in {
            "#V#minimal_imposition_runtime_profile_write_policy_v1",
            "#V#write_tool_policy_workflow",
        }
        else None,
    )

    def _mock_get_texts_for_concept(concept_id: str, predicate: str, limit: int = 1):
        if (
            concept_id == "#V#write_tool_policy_workflow"
            and predicate == "#V#has_minimal_imposition_runtime_profile"
        ):
            return [{"text": "#V#minimal_imposition_runtime_profile_write_policy_v1"}]
        if (
            concept_id == "#V#minimal_imposition_runtime_profile_write_policy_v1"
            and predicate == "#V#has_minimal_imposition_runtime_profile_json"
        ):
            return [{"text": json.dumps(payload)}]
        return []

    monkeypatch.setattr(service, "get_texts_for_concept", _mock_get_texts_for_concept)

    profile, diagnostics = service.load_minimal_imposition_runtime_profile(
        workflow_id="#V#write_tool_policy_workflow"
    )

    assert profile is not None
    assert profile["profile_concept_id"] == (
        "#V#minimal_imposition_runtime_profile_write_policy_v1"
    )
    assert (
        profile["decision_policy"]["require_clear_request_for_recoverable_mutation"]
        is True
    )
    assert profile["tool_risk_classes"]["download_paper"] == "additive_low_risk"
    assert profile["tool_risk_classes"]["delete_concept"] == "destructive"
    assert profile["tool_feature_overrides"]["workflow_bind_event"]["blast_radius"] == (
        "high"
    )
    assert (
        diagnostics["loaded_profile_concept_id"]
        == "#V#minimal_imposition_runtime_profile_write_policy_v1"
    )
