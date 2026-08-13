from __future__ import annotations

import pytest

from src.backend.integrations.internal_mcp.catalogue import (
    _create_concepts as _governed_create_concepts,
)
from src.backend.services import (
    create_concepts_duplicate_guard_service,
    create_concepts_parent_resolution_service,
    relationship_extent_index_service,
    workflow_event_integration_service,
)
from src.backend.services.create_concepts_parent_resolution_service import (
    ParentResolutionResult,
)
from src.backend.vontology import utils_vontology

_create_concepts = _governed_create_concepts.__wrapped__


def test_create_concepts_coalesces_relationship_extent_sync_for_the_batch(
    monkeypatch,
) -> None:
    parent_id = "#V#bounded_parent"
    single_syncs: list[str] = []
    batch_syncs: list[list[str]] = []

    monkeypatch.setattr(
        create_concepts_parent_resolution_service,
        "resolve_parent_for_create_concepts",
        lambda requested_parent_id: ParentResolutionResult(
            requested_parent_id=requested_parent_id,
            canonical_parent_id=parent_id,
            resolved_parent_id=parent_id,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
        ),
    )
    monkeypatch.setattr(
        create_concepts_duplicate_guard_service,
        "find_existing_concept_for_create_concepts",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        workflow_event_integration_service,
        "resolve_event_actor_context",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        relationship_extent_index_service,
        "_sync_relationship_extent_index_for_concept_id_now",
        lambda source_id: single_syncs.append(source_id) or {"success": True},
    )
    monkeypatch.setattr(
        relationship_extent_index_service,
        "_sync_relationship_extent_index_for_concept_ids_now",
        lambda source_ids: batch_syncs.append(list(source_ids)) or {"success": True},
    )

    def _fake_create_vontology_concept(
        *,
        parent_id: str,
        new_concept_name: str,
        **_kwargs,
    ) -> dict:
        concept_id = f"#V#{new_concept_name.lower().replace(' ', '_')}"
        relationship_extent_index_service.sync_relationship_extent_index_for_concept_id(
            parent_id
        )
        relationship_extent_index_service.sync_relationship_extent_index_for_concept_id(
            concept_id
        )
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        utils_vontology,
        "create_vontology_concept",
        _fake_create_vontology_concept,
    )

    result = _create_concepts(
        parent_id=parent_id,
        concepts=[
            {"name": "First bounded concept", "kind": "type"},
            {"name": "Second bounded concept", "kind": "type"},
        ],
    )

    assert result["successful"] == 2
    assert single_syncs == []
    assert batch_syncs == [
        [
            "#V#bounded_parent",
            "#V#first_bounded_concept",
            "#V#second_bounded_concept",
        ]
    ]


def test_create_concepts_flushes_completed_extent_syncs_when_batch_stops(
    monkeypatch,
) -> None:
    parent_id = "#V#bounded_parent"
    created_names: list[str] = []
    batch_syncs: list[list[str]] = []

    monkeypatch.setattr(
        create_concepts_parent_resolution_service,
        "resolve_parent_for_create_concepts",
        lambda requested_parent_id: ParentResolutionResult(
            requested_parent_id=requested_parent_id,
            canonical_parent_id=parent_id,
            resolved_parent_id=parent_id,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
        ),
    )
    monkeypatch.setattr(
        create_concepts_duplicate_guard_service,
        "find_existing_concept_for_create_concepts",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        workflow_event_integration_service,
        "resolve_event_actor_context",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        relationship_extent_index_service,
        "_sync_relationship_extent_index_for_concept_ids_now",
        lambda source_ids: batch_syncs.append(list(source_ids)) or {"success": True},
    )

    def _fake_create_vontology_concept(
        *,
        parent_id: str,
        new_concept_name: str,
        **_kwargs,
    ) -> dict:
        created_names.append(new_concept_name)
        if new_concept_name == "Second bounded concept":
            raise RuntimeError("simulated canonical write failure")
        concept_id = f"#V#{new_concept_name.lower().replace(' ', '_')}"
        relationship_extent_index_service.sync_relationship_extent_index_for_concept_id(
            parent_id
        )
        relationship_extent_index_service.sync_relationship_extent_index_for_concept_id(
            concept_id
        )
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        utils_vontology,
        "create_vontology_concept",
        _fake_create_vontology_concept,
    )

    with pytest.raises(RuntimeError, match="simulated canonical write failure"):
        _create_concepts(
            parent_id=parent_id,
            concepts=[
                {"name": "First bounded concept", "kind": "type"},
                {"name": "Second bounded concept", "kind": "type"},
                {"name": "Third bounded concept", "kind": "type"},
            ],
        )

    assert created_names == [
        "First bounded concept",
        "Second bounded concept",
    ]
    assert batch_syncs == [
        ["#V#bounded_parent", "#V#first_bounded_concept"]
    ]
