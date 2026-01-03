from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from src.backend.services.concept_merge_service import merge_concepts


def _concept(
    concept_id: str, *, relationships: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    return {
        "concept_id": concept_id,
        "name": concept_id.replace("#V#", ""),
        "relationships": relationships or {},
    }


def test_merge_concepts_simulate_reports_text_relation_migration() -> None:
    source_id = "#V#A"
    target_id = "#V#a"

    def fake_get(cid: str) -> Dict[str, Any] | None:
        if cid == source_id:
            return _concept(source_id)
        if cid == target_id:
            return _concept(target_id)
        return None

    source_relations: List[Dict[str, Any]] = [
        {
            "_id": "r1",
            "subject_concept_id": source_id,
            "predicate": "hasNote",
            "object_text_id": "t1",
        },
        {
            "_id": "r2",
            "subject_concept_id": source_id,
            "predicate": "hasDescription",
            "object_text_id": "t2",
        },
    ]
    target_relations: List[Dict[str, Any]] = [
        {
            "_id": "r3",
            "subject_concept_id": target_id,
            "predicate": "hasNote",
            "object_text_id": "t1",
        },
    ]

    with (
        patch(
            "src.backend.services.concept_merge_service.get_concept_by_id",
            side_effect=fake_get,
        ),
        patch(
            "src.backend.services.concept_merge_service.ConceptsRepository"
        ) as mock_concepts_repo,
        patch(
            "src.backend.services.concept_merge_service.TextRelationsRepository"
        ) as mock_text_rel_repo,
    ):
        mock_concepts_repo.find.return_value = []

        def fake_find(filter: Dict[str, Any], *args: Any, **kwargs: Any):
            if filter == {"subject_concept_id": source_id}:
                return list(source_relations)
            if filter == {"subject_concept_id": target_id}:
                return list(target_relations)
            return []

        mock_text_rel_repo.find.side_effect = fake_find

        report = merge_concepts(source_id, target_id, simulate=True)

    assert report["success"] is True
    ops = report["operations"]
    migrate = [op for op in ops if op.get("type") == "migrate_text_relations"]
    assert migrate
    assert migrate[0]["move_count"] == 1
    assert migrate[0]["duplicate_count"] == 1


def test_merge_concepts_apply_updates_text_relations_and_deletes_source() -> None:
    source_id = "#V#A"
    target_id = "#V#a"

    def fake_get(cid: str) -> Dict[str, Any] | None:
        if cid == source_id:
            return _concept(source_id)
        if cid == target_id:
            return _concept(target_id)
        return None

    other_doc = {
        "concept_id": "#V#Other",
        "relationships": {"related_to": [source_id]},
    }

    source_relations: List[Dict[str, Any]] = [
        {
            "_id": "r1",
            "subject_concept_id": source_id,
            "predicate": "hasNote",
            "object_text_id": "t1",
        },
    ]

    with (
        patch(
            "src.backend.services.concept_merge_service.get_concept_by_id",
            side_effect=fake_get,
        ),
        patch(
            "src.backend.services.concept_merge_service.ConceptsRepository"
        ) as mock_concepts_repo,
        patch(
            "src.backend.services.concept_merge_service.TextRelationsRepository"
        ) as mock_text_rel_repo,
    ):
        mock_concepts_repo.find.return_value = [other_doc]
        mock_concepts_repo.update_one = MagicMock()
        mock_concepts_repo.delete_one = MagicMock()

        def fake_find(filter: Dict[str, Any], *args: Any, **kwargs: Any):
            if filter == {"subject_concept_id": target_id}:
                return []
            if filter == {"subject_concept_id": source_id}:
                return list(source_relations)
            return []

        mock_text_rel_repo.find.side_effect = fake_find
        mock_text_rel_repo.update_one = MagicMock()
        mock_text_rel_repo.delete_one = MagicMock()

        report = merge_concepts(source_id, target_id, simulate=False)

    assert report["success"] is True
    mock_text_rel_repo.update_one.assert_called()
    mock_concepts_repo.delete_one.assert_called_with({"concept_id": source_id})
