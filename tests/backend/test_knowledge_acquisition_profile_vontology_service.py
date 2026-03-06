from __future__ import annotations

import json

from src.backend.services import knowledge_acquisition_profile_vontology_service as service


def test_canonical_knowledge_acquisition_profile_concept_ids_are_deterministic() -> None:
    concept_ids = service.canonical_knowledge_acquisition_profile_concept_ids()
    assert concept_ids == (
        "#V#knowledge_acquisition_profile_low_imposition_relation_completion",
    )


def test_ensure_canonical_knowledge_acquisition_profiles_creates_and_links(monkeypatch) -> None:
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

    report = service.ensure_canonical_knowledge_acquisition_profiles()

    assert report["success"] is True
    assert report["profile_type_created"] is True
    assert report["created_profile_concept_ids"] == [
        "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
    ]
    assert report["linked_workflow_ids"] == ["#V#rumination_workflow"]
    texts_by_concept = {
        (item["concept_id"], item["predicate"]): item["text"] for item in upserts
    }
    assert (
        "#V#knowledge_acquisition_profile_low_imposition_relation_completion",
        "#V#has_knowledge_acquisition_profile_json",
    ) in texts_by_concept
    assert (
        "#V#rumination_workflow",
        "#V#has_knowledge_acquisition_profile",
    ) in texts_by_concept


def test_load_knowledge_acquisition_profile_uses_workflow_link(monkeypatch) -> None:
    payload = {
        "profile_id": "low_imposition_relation_completion",
        "dispatch_mode": "relation_completion",
        "question_limit": 1,
        "detail_limit": 12,
        "decision_policy": {"ask_at_most_one_question_per_run": True},
        "relation_auto_apply_policy": {"policy_version": "knowledge_acquisition_profile.v1"},
    }

    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}}
        if concept_id
        == "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
        else {"concept_id": concept_id, "relationships": {}}
        if concept_id == "#V#rumination_workflow"
        else None,
    )

    def _mock_get_texts_for_concept(concept_id: str, predicate: str, limit: int = 1):
        if (
            concept_id == "#V#rumination_workflow"
            and predicate == "#V#has_knowledge_acquisition_profile"
        ):
            return [
                {
                    "text": "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
                }
            ]
        if (
            concept_id == "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
            and predicate == "#V#has_knowledge_acquisition_profile_json"
        ):
            return [{"text": json.dumps(payload)}]
        return []

    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        _mock_get_texts_for_concept,
    )

    profile, diagnostics = service.load_knowledge_acquisition_profile(
        workflow_id="#V#rumination_workflow"
    )

    assert profile is not None
    assert profile["profile_concept_id"] == (
        "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
    )
    assert profile["question_limit"] == 1
    assert profile["detail_limit"] == 12
    assert (
        diagnostics["loaded_profile_concept_id"]
        == "#V#knowledge_acquisition_profile_low_imposition_relation_completion"
    )
