from __future__ import annotations

import pytest

from src.backend.services import relationship_write_service
from src.backend.services import relationship_removal_service
from src.backend.security.visibility_predicates import (
    VISIBILITY_PREDICATE_ALIAS_TO_CANONICAL,
)
from src.backend.vontology import utils_vontology


def _normalise_for_test(predicate: str) -> str:
    return VISIBILITY_PREDICATE_ALIAS_TO_CANONICAL.get(predicate, predicate)


@pytest.mark.parametrize(
    ("predicate", "structural_predicates"),
    [
        ("is_a_type_of", {"is_a_type_of"}),
        ("specific_to_organisation", set()),
        ("#V#specific_to_user", set()),
    ],
)
def test_tree_relevant_relationship_addition_invalidates_vontology_projection(
    monkeypatch,
    predicate: str,
    structural_predicates: set[str],
) -> None:
    invalidations: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        relationship_write_service,
        "get_relationship_kinds_set",
        lambda: structural_predicates,
    )
    monkeypatch.setattr(
        relationship_write_service,
        "normalise_structural_predicate",
        _normalise_for_test,
    )
    monkeypatch.setattr(
        relationship_write_service,
        "add_structural_relationship",
        lambda *_args, **_kwargs: {"success": True, "forward_modified": True},
    )
    monkeypatch.setattr(
        relationship_write_service,
        "add_dynamic_relationship",
        lambda *_args, **_kwargs: {"success": True, "modified": True},
    )
    monkeypatch.setattr(
        relationship_write_service,
        "_invalidate_workflow_routing_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        utils_vontology,
        "invalidate_vontology_caches",
        lambda affected, correlation_id: invalidations.append(
            (affected, correlation_id)
        ),
    )

    result = relationship_write_service.add_relationship(
        "#V#source",
        predicate,
        "#V#target",
    )

    assert result["success"] is True
    assert len(invalidations) == 1
    affected, correlation_id = invalidations[0]
    assert affected == ["#V#source", "#V#target"]
    assert correlation_id.startswith("relationship-add:#V#source:")


def test_inverse_only_hierarchy_repair_invalidates_tree(
    monkeypatch,
) -> None:
    invalidations: list[list[str]] = []
    monkeypatch.setattr(
        relationship_write_service,
        "get_relationship_kinds_set",
        lambda: {"has_subtype"},
    )
    monkeypatch.setattr(
        relationship_write_service,
        "normalise_structural_predicate",
        _normalise_for_test,
    )
    monkeypatch.setattr(
        relationship_write_service,
        "add_structural_relationship",
        lambda *_args, **_kwargs: {
            "success": True,
            "forward_modified": False,
            "inverse_modified": True,
        },
    )
    monkeypatch.setattr(
        relationship_write_service,
        "_invalidate_workflow_routing_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        utils_vontology,
        "invalidate_vontology_caches",
        lambda affected, **_kwargs: invalidations.append(affected),
    )

    relationship_write_service.add_relationship(
        "#V#parent",
        "has_subtype",
        "#V#child",
    )

    assert invalidations == [["#V#parent", "#V#child"]]


def test_unrelated_structural_relationship_does_not_invalidate_tree(
    monkeypatch,
) -> None:
    invalidations = 0

    def invalidate(*_args, **_kwargs) -> None:
        nonlocal invalidations
        invalidations += 1

    monkeypatch.setattr(
        relationship_write_service,
        "get_relationship_kinds_set",
        lambda: {"related_to"},
    )
    monkeypatch.setattr(
        relationship_write_service,
        "normalise_structural_predicate",
        _normalise_for_test,
    )
    monkeypatch.setattr(
        relationship_write_service,
        "add_structural_relationship",
        lambda *_args, **_kwargs: {"success": True, "forward_modified": True},
    )
    monkeypatch.setattr(
        relationship_write_service,
        "_invalidate_workflow_routing_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        utils_vontology,
        "invalidate_vontology_caches",
        invalidate,
    )

    result = relationship_write_service.add_relationship(
        "#V#source",
        "related_to",
        "#V#target",
    )

    assert result["success"] is True
    assert invalidations == 0


def test_relationship_undo_invalidates_vontology_projection(monkeypatch) -> None:
    class Tombstones:
        def find(self, *_args, **_kwargs):
            return [
                {
                    "source_id": "#V#child",
                    "predicate": "is_a_type_of",
                    "target": "#V#parent",
                }
            ]

    invalidations: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        relationship_removal_service,
        "_tombstone_collection",
        lambda: Tombstones(),
    )
    monkeypatch.setattr(
        relationship_removal_service,
        "_resolve_actor_context",
        lambda: ("#V#actor", "user"),
    )
    monkeypatch.setattr(
        relationship_removal_service,
        "_append_audit_record",
        lambda _record: None,
    )
    monkeypatch.setattr(
        relationship_removal_service.ConceptsRepository,
        "mutate_relationship_edge",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        relationship_removal_service,
        "invalidate_vontology_caches",
        lambda affected, correlation_id: invalidations.append(
            (affected, correlation_id)
        ),
    )

    result = relationship_removal_service.undo_relationship_removal(
        undo_token="undo:test",
        request_id="request-test",
    )

    assert result["success"] is True
    assert result["restored_count"] == 1
    assert invalidations == [(["#V#child", "#V#parent"], "request-test")]
