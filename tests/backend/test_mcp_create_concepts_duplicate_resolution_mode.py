"""Focused create_concepts duplicate-mode and cancellation regressions."""

from __future__ import annotations

from typing import Any

import pytest

from src.backend.integrations.internal_mcp.catalogue import (
    _concepts_create_input_schema,
    _create_concepts,
)
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import (
    InternalMCPHandlerCancelled,
)
from src.backend.services.create_concepts_parent_resolution_service import (
    ParentResolutionResult,
)
from src.backend.utils.concept_id_utils import canonicalise_vontology_concept_id


_PARENT_ID = "#V#abstract_object"


def _patch_common_preflights(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda parent_id: ParentResolutionResult(
            requested_parent_id=parent_id,
            canonical_parent_id=_PARENT_ID,
            resolved_parent_id=_PARENT_ID,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service."
        "resolve_event_actor_context",
        lambda **_kwargs: (None, None),
    )


def _created_payload(**kwargs: Any) -> dict[str, Any]:
    name = str(kwargs["new_concept_name"])
    concept_id = canonicalise_vontology_concept_id(name)
    assert concept_id
    return {
        "success": True,
        "message": "created",
        "concept": {"concept_id": concept_id},
        "canonical_concept_id": concept_id,
        "input_name": name,
    }


def _not_found_resolution(**_kwargs: Any) -> dict[str, Any]:
    return {
        "success": True,
        "status": "not_found",
        "resolved_concept_id": None,
        "candidates": [],
        "audit": [],
    }


def test_canonical_id_only_skips_semantic_resolution_after_exact_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    semantic_calls: list[str] = []
    exact_queries: list[dict[str, Any]] = []

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> None:
        exact_queries.append(query)
        return None

    def _resolve_concept_by_name(**kwargs: Any) -> dict[str, Any]:
        semantic_calls.append(str(kwargs.get("name")))
        return _not_found_resolution()

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        _resolve_concept_by_name,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _created_payload,
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "stable represented identity", "kind": "type"}],
        duplicate_resolution_mode="canonical_id_only",
    )

    assert result["successful"] == 1
    assert semantic_calls == []
    assert exact_queries == [{"concept_id": "#V#stable_represented_identity"}]


def test_default_mode_retains_semantic_duplicate_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    semantic_calls: list[str] = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda _query, _projection=None: None,
    )

    def _resolve_concept_by_name(**kwargs: Any) -> dict[str, Any]:
        semantic_calls.append(str(kwargs.get("name")))
        return _not_found_resolution()

    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        _resolve_concept_by_name,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _created_payload,
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "human supplied label", "kind": "type"}],
    )

    assert result["successful"] == 1
    assert semantic_calls == ["human supplied label"]


def test_canonical_id_only_still_reuses_exact_existing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    semantic_calls: list[str] = []
    create_calls: list[dict[str, Any]] = []

    existing = {
        "concept_id": "#V#stable_existing_identity",
        "relationships": {
            "is_a_type_of": [_PARENT_ID],
            "is_an_instance_of": [],
        },
    }
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            existing if query.get("concept_id") == existing["concept_id"] else None
        ),
    )

    def _resolve_concept_by_name(**kwargs: Any) -> dict[str, Any]:
        semantic_calls.append(str(kwargs.get("name")))
        return _not_found_resolution()

    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        _resolve_concept_by_name,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "stable existing identity", "kind": "type"}],
        duplicate_resolution_mode="canonical_id_only",
    )

    item = result["results"][0]
    assert result["successful"] == 0
    assert result["already_existed"] == 1
    assert item["existing_concept_id"] == existing["concept_id"]
    assert item["duplicate_match_source"] == "canonical_concept_id"
    assert semantic_calls == []
    assert create_calls == []


def test_duplicate_resolution_mode_schema_and_handler_reject_unknown_value() -> None:
    schema = _concepts_create_input_schema()
    assert schema.enum_values["duplicate_resolution_mode"] == [
        "canonical_id_only",
        None,
    ]
    valid, errors = validate_payload(
        schema,
        {
            "parent_id": _PARENT_ID,
            "concepts": [],
            "duplicate_resolution_mode": "canonical_id_only",
        },
    )
    assert valid is True
    assert errors == []

    valid_null, errors = validate_payload(
        schema,
        {
            "parent_id": _PARENT_ID,
            "concepts": [],
            "duplicate_resolution_mode": None,
        },
    )
    assert valid_null is True
    assert errors == []

    invalid, errors = validate_payload(
        schema,
        {
            "parent_id": _PARENT_ID,
            "concepts": [],
            "duplicate_resolution_mode": "semantic",
        },
    )
    assert invalid is False
    assert any("canonical_id_only" in error for error in errors)

    handler_result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[],
        duplicate_resolution_mode="semantic",
    )
    assert handler_result["error_code"] == "invalid_parameter"
    assert handler_result["error_details"]["supported_duplicate_resolution_modes"] == [
        "canonical_id_only"
    ]


def test_cancellation_after_duplicate_preflight_prevents_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    duplicate_preflight_completed = False
    create_calls: list[dict[str, Any]] = []

    def _duplicate_guard(**_kwargs: Any) -> None:
        nonlocal duplicate_preflight_completed
        duplicate_preflight_completed = True
        return None

    def _raise_when_preflight_completed() -> None:
        if duplicate_preflight_completed:
            raise InternalMCPHandlerCancelled("cancelled before create")

    monkeypatch.setattr(
        "src.backend.services.create_concepts_duplicate_guard_service."
        "find_existing_concept_for_create_concepts",
        _duplicate_guard,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        _raise_when_preflight_completed,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    with pytest.raises(InternalMCPHandlerCancelled):
        _create_concepts(
            parent_id=_PARENT_ID,
            concepts=[{"name": "cancelled identity", "kind": "type"}],
        )

    assert create_calls == []


def test_cancellation_between_batch_items_prevents_later_creates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    created_names: list[str] = []

    monkeypatch.setattr(
        "src.backend.services.create_concepts_duplicate_guard_service."
        "find_existing_concept_for_create_concepts",
        lambda **_kwargs: None,
    )

    def _cancel_after_first_complete() -> None:
        if created_names:
            raise InternalMCPHandlerCancelled("cancelled between items")

    def _create(**kwargs: Any) -> dict[str, Any]:
        created_names.append(str(kwargs["new_concept_name"]))
        return _created_payload(**kwargs)

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        _cancel_after_first_complete,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )

    with pytest.raises(InternalMCPHandlerCancelled):
        _create_concepts(
            parent_id=_PARENT_ID,
            concepts=[
                {"name": "first complete identity", "kind": "type"},
                {"name": "later blocked identity", "kind": "type"},
            ],
        )

    assert created_names == ["first complete identity"]
