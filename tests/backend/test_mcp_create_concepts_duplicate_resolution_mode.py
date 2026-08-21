"""Focused create_concepts duplicate-mode and cancellation regressions."""

from __future__ import annotations

from typing import Any

import pytest

from src.backend.integrations.internal_mcp.catalogue import (
    _concepts_create_input_schema,
    _create_concepts as _governed_create_concepts,
)
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import (
    InternalMCPHandlerCancelled,
)
from src.backend.services.create_concepts_parent_resolution_service import (
    ParentResolutionResult,
)
from src.backend.services.concept_external_identity_service import (
    ExternalIdentityResolution,
)
from src.backend.utils.concept_id_utils import canonicalise_vontology_concept_id

_create_concepts = _governed_create_concepts.__wrapped__

_PARENT_ID = "#V#abstract_object"
_EXTERNAL_IDENTITY_PARENT_ID = "#V#archival_record"
_ALTERNATE_IDENTITY_PARENT_ID = "#V#catalogue_entry"


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


def _patch_external_identity_preflights(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parent_id: str = _EXTERNAL_IDENTITY_PARENT_ID,
    actor_user_id: str | None = None,
    actor_org_id: str | None = None,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
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
        "src.backend.services.workflow_event_integration_service."
        "resolve_event_actor_context",
        lambda **_kwargs: (actor_user_id, actor_org_id),
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


def test_canonical_id_only_reports_wrong_kind_as_canonical_identity_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    create_calls: list[dict[str, Any]] = []
    semantic_calls: list[str] = []
    existing = {
        "concept_id": "#V#occupied_identity",
        "relationships": {
            "is_a_type_of": [_PARENT_ID],
            "is_an_instance_of": [_PARENT_ID],
        },
    }
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            existing if query.get("concept_id") == existing["concept_id"] else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **kwargs: semantic_calls.append(str(kwargs.get("name")))
        or _not_found_resolution(),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "occupied identity", "kind": "instance"}],
        duplicate_resolution_mode="canonical_id_only",
    )

    item = result["results"][0]
    assert result["effect_status"] == "failed"
    assert result["successful"] == 0
    assert result["already_existed"] == 0
    assert result["failed"] == 1
    assert item["error_code"] == "canonical_identity_conflict"
    assert item["existing_concept_id"] == existing["concept_id"]
    assert item["existing_kind"] == "type"
    assert item["requested_kind"] == "instance"
    assert item["identity_mismatch_reasons"] == ["kind_mismatch"]
    assert item["changed"] is False
    assert semantic_calls == []
    assert create_calls == []


def test_canonical_id_only_reports_wrong_instance_parent_as_identity_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    create_calls: list[dict[str, Any]] = []
    existing = {
        "concept_id": "#V#occupied_instance",
        "relationships": {
            "is_a_type_of": [],
            "is_an_instance_of": ["#V#other_parent"],
        },
    }
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            existing if query.get("concept_id") == existing["concept_id"] else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "occupied instance", "kind": "instance"}],
        duplicate_resolution_mode="canonical_id_only",
    )

    item = result["results"][0]
    assert item["error_code"] == "canonical_identity_conflict"
    assert item["existing_kind"] == "instance"
    assert item["requested_parent_id"] == _PARENT_ID
    assert item["existing_parent_ids"] == ["#V#other_parent"]
    assert item["identity_mismatch_reasons"] == ["parent_mismatch"]
    assert create_calls == []


def test_type_identity_remains_singleton_across_requested_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    existing = {
        "concept_id": "#V#singleton_type",
        "relationships": {
            "is_a_type_of": ["#V#other_parent"],
            "is_an_instance_of": [],
        },
    }
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            existing if query.get("concept_id") == existing["concept_id"] else None
        ),
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "singleton type", "kind": "type"}],
        duplicate_resolution_mode="canonical_id_only",
    )

    item = result["results"][0]
    assert item["error_code"] == "already_exists"
    assert item["existing_concept_id"] == existing["concept_id"]
    assert item.get("identity_mismatch_reasons") is None


def test_secondary_name_match_reuses_recognised_workflow_instance_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    requested_parent = "#V#durable_workflow"
    existing_parent = "#V#ai_workflow"
    existing_concept_id = "#V#existing_named_workflow"
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda parent_id: ParentResolutionResult(
            requested_parent_id=parent_id,
            canonical_parent_id=requested_parent,
            resolved_parent_id=requested_parent,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
            resolved_parent_kind="type",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.create_concepts_duplicate_guard_service."
        "_workflow_type_ids",
        lambda: {requested_parent, existing_parent},
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            {
                "concept_id": existing_concept_id,
                "relationships": {
                    "is_a_type_of": [],
                    "is_an_instance_of": [existing_parent],
                },
            }
            if query.get("concept_id") == existing_concept_id
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **_kwargs: {
            "success": True,
            "status": "resolved",
            "resolved_concept_id": existing_concept_id,
            "match": {"stage": "exact", "score": 400},
            "candidates": [],
            "audit": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=requested_parent,
        concepts=[{"name": "Existing named workflow alias", "kind": "instance"}],
    )

    item = result["results"][0]
    assert result["already_existed"] == 1
    assert item["error_code"] == "already_exists"
    assert item["existing_concept_id"] == existing_concept_id
    assert item["duplicate_guard_scope"] == "workflow_instance"
    assert item["duplicate_match_source"] == "name_resolution"
    assert create_calls == []


def test_create_person_blocks_comma_order_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person_id = "#V#person"
    existing_concept_id = "#V#person_agnieszka_mensfelt"
    resolver_calls: list[dict[str, Any]] = []
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda requested_parent_id: ParentResolutionResult(
            requested_parent_id=requested_parent_id,
            canonical_parent_id=person_id,
            resolved_parent_id=person_id,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
            resolved_parent_kind="type",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service."
        "resolve_event_actor_context",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            {
                "concept_id": existing_concept_id,
                "relationships": {"is_an_instance_of": [person_id]},
            }
            if query.get("concept_id") == existing_concept_id
            else None
        ),
    )

    def _resolve(**kwargs: Any) -> dict[str, Any]:
        resolver_calls.append(dict(kwargs))
        return {
            "success": True,
            "status": "resolved",
            "resolved_concept_id": existing_concept_id,
            "match": {"stage": "person_comma_order_exact", "score": 370},
            "candidates": [],
            "audit": [],
        }

    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        _resolve,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=person_id,
        concepts=[{"name": "Mensfelt, Agnieszka", "kind": "instance"}],
    )

    item = result["results"][0]
    assert result["already_existed"] == 1
    assert item["error_code"] == "already_exists"
    assert item["existing_concept_id"] == existing_concept_id
    assert item["duplicate_match_source"] == "name_resolution"
    assert resolver_calls[0]["instance_of"] == person_id
    assert create_calls == []


def test_create_person_comma_order_ambiguity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person_id = "#V#person"
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda requested_parent_id: ParentResolutionResult(
            requested_parent_id=requested_parent_id,
            canonical_parent_id=person_id,
            resolved_parent_id=person_id,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
            resolved_parent_kind="type",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service."
        "resolve_event_actor_context",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **_kwargs: {
            "success": True,
            "status": "ambiguous",
            "resolved_concept_id": None,
            "candidates": [
                {
                    "concept_id": "#V#agnieszka_a",
                    "stage": "person_comma_order_exact",
                    "score": 370,
                },
                {
                    "concept_id": "#V#agnieszka_b",
                    "stage": "person_comma_order_exact",
                    "score": 370,
                },
            ],
            "audit": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=person_id,
        concepts=[{"name": "Mensfelt, Agnieszka", "kind": "instance"}],
    )

    item = result["results"][0]
    assert result["effect_status"] == "failed"
    assert item["error_code"] == "ambiguous_person_name_order_match"
    assert item["candidate_concept_ids"] == ["#V#agnieszka_a", "#V#agnieszka_b"]
    assert item["changed"] is False
    assert create_calls == []


def test_create_non_person_does_not_accept_person_comma_order_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **_kwargs: {
            "success": True,
            "status": "resolved",
            "resolved_concept_id": "#V#given_family",
            "match": {"stage": "person_comma_order_exact", "score": 370},
            "candidates": [],
            "audit": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs) or _created_payload(**kwargs),
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "Family, Given", "kind": "instance"}],
    )

    assert result["successful"] == 1
    assert len(create_calls) == 1


def test_create_concepts_disables_suffixing_unless_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    suffix_flags: list[bool] = []
    monkeypatch.setattr(
        "src.backend.services.create_concepts_duplicate_guard_service."
        "find_existing_concept_for_create_concepts",
        lambda **_kwargs: None,
    )

    def _create(**kwargs: Any) -> dict[str, Any]:
        suffix_flags.append(bool(kwargs["allow_duplicate_instance_suffix"]))
        return _created_payload(**kwargs)

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )

    default_result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "default singleton", "kind": "instance"}],
    )
    explicit_result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[{"name": "explicit homonym", "kind": "instance"}],
        allow_duplicate_instances=True,
    )

    assert default_result["successful"] == 1
    assert explicit_result["successful"] == 1
    assert suffix_flags == [False, True]


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


def test_actor_scoped_collision_recovery_fields_are_explicit_in_catalogue() -> None:
    schema = _concepts_create_input_schema()
    assert schema.enum_values["collision_resolution_mode"] == [
        "actor_scoped_referent",
        None,
    ]
    assert "requested_concept_id" in schema.optional
    assert "does not inspect or confirm whether that ID is occupied" in (
        schema.description
    )
    valid, errors = validate_payload(
        schema,
        {
            "parent_id": _PARENT_ID,
            "concepts": [
                {
                    "concept_id": "#V#scoped_referent_person_0123456789abcdef01234567",
                    "name": "Person",
                    "kind": "instance",
                }
            ],
            "collision_resolution_mode": "actor_scoped_referent",
            "requested_concept_id": "#V#person",
            "duplicate_resolution_mode": "canonical_id_only",
            "scope_mode": "user_only_default",
        },
    )
    assert valid is True
    assert errors == []

    invalid_mode = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[],
        collision_resolution_mode="invent_alias",
    )
    assert invalid_mode["error_code"] == "invalid_parameter"
    assert invalid_mode["error_details"][
        "supported_collision_resolution_modes"
    ] == ["actor_scoped_referent"]

    missing_requested_id = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[],
        collision_resolution_mode="actor_scoped_referent",
    )
    assert missing_requested_id["error_code"] == "invalid_parameter"
    assert missing_requested_id["error_details"]["missing"] == [
        "requested_concept_id"
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

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[
            {"name": "first complete identity", "kind": "type"},
            {"name": "later blocked identity", "kind": "type"},
        ],
    )

    assert created_names == ["first complete identity"]
    assert result["success"] is False
    assert result["effect_status"] == "partial"
    assert result["changed"] is True
    assert result["error_code"] == "handler_cancelled_after_partial_completion"
    assert result["cancellation_requested"] is True
    assert result["completed_before_cancellation"] == 1
    assert result["unattempted_count"] == 1
    assert result["successful"] == 1
    assert result["failed"] == 0
    assert result["created_concept_ids"] == ["#V#first_complete_identity"]


def test_external_identity_reuses_same_kind_across_parent_classifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(
        monkeypatch,
        parent_id=_ALTERNATE_IDENTITY_PARENT_ID,
    )
    existing_id = "#V#existing_catalogued_entity"
    create_calls: list[dict[str, Any]] = []

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if query.get("concept_id") == existing_id:
            return {
                "concept_id": existing_id,
                "relationships": {
                    "is_an_instance_of": [_EXTERNAL_IDENTITY_PARENT_ID],
                    "is_a_type_of": [],
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=(existing_id,),
            resolution_source="persisted_identity_marker",
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: {"success": True, "writes": [], "failures": []},
    )

    result = _create_concepts(
        parent_id=_ALTERNATE_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "A newer display label",
                "kind": "instance",
                "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
            }
        ],
        duplicate_resolution_mode="canonical_id_only",
    )

    item = result["results"][0]
    assert result["successful"] == 0
    assert result["already_existed"] == 1
    assert item["existing_concept_id"] == existing_id
    assert item["duplicate_match_source"] == "external_identifier:catalogue"
    assert item.get("identity_mismatch_reasons") is None
    assert create_calls == []


def test_external_identity_reuse_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    existing_id = "#V#existing_catalogued_entity"

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            {
                "concept_id": existing_id,
                "relationships": {
                    "is_an_instance_of": [_EXTERNAL_IDENTITY_PARENT_ID],
                    "is_a_type_of": [],
                },
            }
            if query.get("concept_id") == existing_id
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=(existing_id,),
            resolution_source="persisted_identity_marker",
        ),
    )
    persistence_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **kwargs: persistence_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the existing identity must be reused")
        ),
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "A newer display label",
                "kind": "instance",
                "external_identifiers": [
                    {"scheme": "catalogue", "value": "record-42"}
                ],
            }
        ],
    )

    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["changed"] is False
    assert result["already_existed"] == 1
    assert result["results"][0]["external_identity"]["persistence"] == {
        "success": True,
        "effect_status": "not_started",
        "changed": False,
        "reason_code": "duplicate_reuse_is_read_only",
    }
    assert persistence_calls == []


def test_external_identity_reuse_does_not_start_marker_repair_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    existing_id = "#V#existing_catalogued_entity"
    cancellation_checks = 0
    persistence_calls: list[str] = []

    def _cancel_before_marker_repair() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 3:
            raise InternalMCPHandlerCancelled("deadline elapsed")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        _cancel_before_marker_repair,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda query, _projection=None: (
            {
                "concept_id": existing_id,
                "relationships": {
                    "is_an_instance_of": [_EXTERNAL_IDENTITY_PARENT_ID],
                    "is_a_type_of": [],
                },
            }
            if query.get("concept_id") == existing_id
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=(existing_id,),
            resolution_source="persisted_identity_marker",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **kwargs: persistence_calls.append(str(kwargs["concept_id"])),
    )

    with pytest.raises(InternalMCPHandlerCancelled, match="deadline elapsed"):
        _create_concepts(
            parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
            concepts=[
                {
                    "name": "A newer display label",
                    "kind": "instance",
                    "external_identifiers": [
                        {"scheme": "catalogue", "value": "record-42"}
                    ],
                }
            ],
        )

    assert cancellation_checks == 3
    assert persistence_calls == []


def test_new_external_identity_preserves_created_concept_when_marker_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    concept_created = False
    persistence_calls: list[str] = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="no_visible_identity_evidence",
        ),
    )

    def _cancel_after_create() -> None:
        if concept_created:
            raise InternalMCPHandlerCancelled("deadline elapsed after create")

    def _create(**kwargs: Any) -> dict[str, Any]:
        nonlocal concept_created
        concept_created = True
        concept_id = str(kwargs["canonical_concept_id_override"])
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        _cancel_after_create,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **kwargs: persistence_calls.append(str(kwargs["concept_id"])),
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "New catalogue entity",
                "kind": "instance",
                "external_identifiers": [
                    {"scheme": "catalogue", "value": "record-42"}
                ],
            }
        ],
    )

    stable_id = result["created_concept_ids"][0]
    item = result["results"][0]
    assert persistence_calls == []
    assert result["success"] is False
    assert result["effect_status"] == "partial"
    assert result["changed"] is True
    assert result["error_code"] == "handler_cancelled_after_partial_completion"
    assert result["completed_before_cancellation"] == 1
    assert result["unattempted_count"] == 0
    assert result["partial_failures"] == [
        {
            "concept_id": stable_id,
            "stage": "external_identity_persistence",
            "error_code": (
                "external_identity_persistence_not_started_after_concept_creation"
            ),
            "effect_status": "not_started",
            "dispatched": False,
        }
    ]
    assert item["success"] is True
    assert item["effect_status"] == "partial"
    assert item["changed"] is True
    assert item["external_identity"]["persistence"] == {
        "success": False,
        "effect_status": "not_started",
        "changed": False,
        "writes": [],
        "failures": [],
        "indeterminate_failures": [],
        "error_code": (
            "external_identity_persistence_not_started_after_concept_creation"
        ),
        "cancellation": "deadline elapsed after create",
    }


def test_unverified_external_identity_candidate_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    candidate_id = "#V#legacy_catalogue_record"
    observed_assertions: list[tuple[str, ...]] = []
    create_calls: list[dict[str, Any]] = []

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if query.get("concept_id") == candidate_id:
            return {
                "concept_id": candidate_id,
                "relationships": {
                    "is_an_instance_of": [_ALTERNATE_IDENTITY_PARENT_ID],
                    "is_a_type_of": [],
                },
            }
        return None

    def _resolve(
        identifier,
        *,
        asserted_candidate_concept_ids=(),
        **_kwargs,
    ) -> ExternalIdentityResolution:
        asserted = tuple(asserted_candidate_concept_ids)
        observed_assertions.append(asserted)
        if asserted == (candidate_id,):
            return ExternalIdentityResolution(
                status="resolved",
                identifier=identifier,
                candidate_concept_ids=(candidate_id,),
                resolution_source="caller_confirmed_visible_evidence",
            )
        return ExternalIdentityResolution(
            status="unverified",
            identifier=identifier,
            candidate_concept_ids=(candidate_id,),
            resolution_source="legacy_text_reference",
        )

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        _resolve,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: {"success": True, "writes": [], "failures": []},
    )

    base_item = {
        "name": "Imported catalogue entity",
        "kind": "instance",
        "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
    }
    unconfirmed = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[base_item],
    )
    confirmed = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                **base_item,
                "identity_candidate_concept_ids": [candidate_id],
            }
        ],
    )

    blocked_item = unconfirmed["results"][0]
    confirmed_item = confirmed["results"][0]
    assert blocked_item["error_code"] == (
        "external_identity_candidates_require_confirmation"
    )
    assert blocked_item["candidate_concept_ids"] == [candidate_id]
    assert (
        blocked_item["external_identity_resolution_source"] == "legacy_text_reference"
    )
    assert blocked_item["changed"] is False
    assert confirmed["already_existed"] == 1
    assert confirmed_item["existing_concept_id"] == candidate_id
    assert confirmed_item["duplicate_match_source"] == ("external_identifier:catalogue")
    assert observed_assertions == [(), (candidate_id,)]
    assert create_calls == []


def test_reviewed_legacy_non_match_allows_new_stable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    candidate_id = "#V#unrelated_legacy_reference"
    observed_rejections: list[tuple[str, ...]] = []
    create_calls: list[str] = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )

    def _resolve(
        identifier,
        *,
        rejected_candidate_concept_ids=(),
        **_kwargs,
    ) -> ExternalIdentityResolution:
        rejected = tuple(rejected_candidate_concept_ids)
        observed_rejections.append(rejected)
        if rejected == (candidate_id,):
            return ExternalIdentityResolution(
                status="not_found",
                identifier=identifier,
                resolution_source="caller_rejected_legacy_candidates",
            )
        return ExternalIdentityResolution(
            status="unverified",
            identifier=identifier,
            candidate_concept_ids=(candidate_id,),
            resolution_source="legacy_text_reference",
        )

    def _create(**kwargs: Any) -> dict[str, Any]:
        concept_id = str(kwargs["canonical_concept_id_override"])
        create_calls.append(concept_id)
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        _resolve,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "writes": [{"relation_created": True}],
            "failures": [],
            "indeterminate_failures": [],
        },
    )

    base_item = {
        "name": "New catalogue entity",
        "kind": "instance",
        "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
    }
    blocked = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[base_item],
    )
    created = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                **base_item,
                "identity_rejected_candidate_concept_ids": [candidate_id],
            }
        ],
    )

    assert blocked["results"][0]["error_code"] == (
        "external_identity_candidates_require_confirmation"
    )
    assert blocked["results"][0]["candidate_concept_ids"] == [candidate_id]
    assert created["success"] is True
    assert created["effect_status"] == "succeeded"
    assert created["changed"] is True
    assert created["created_concept_ids"] == create_calls
    assert observed_rejections == [(), (candidate_id,)]


def test_external_identity_confirmation_candidates_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an oversized candidate set must not reach lookup")
        ),
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Imported catalogue entity",
                "kind": "instance",
                "external_identifiers": [
                    {"scheme": "catalogue", "value": "record-42"}
                ],
                "identity_candidate_concept_ids": [
                    f"#V#candidate_{index}" for index in range(51)
                ],
            }
        ],
    )

    assert result["success"] is False
    assert result["changed"] is False
    assert result["results"][0]["error_code"] == (
        "too_many_identity_candidate_concept_ids"
    )


def test_identity_confirmation_cannot_drop_the_explicit_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    create_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Imported catalogue entity",
                "kind": "instance",
                "identity_candidate_concept_ids": ["#V#candidate_record"],
            }
        ],
    )

    assert result["success"] is False
    assert result["changed"] is False
    assert result["results"][0]["error_code"] == (
        "identity_candidates_require_external_identifier"
    )
    assert create_calls == []


def test_invalid_identity_confirmation_candidate_cannot_fall_back_to_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    create_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Imported catalogue entity",
                "kind": "instance",
                "identity_candidate_concept_ids": [123],
            }
        ],
    )

    assert result["success"] is False
    assert result["changed"] is False
    assert result["results"][0]["error_code"] == (
        "invalid_identity_candidate_concept_ids"
    )
    assert create_calls == []


def test_multiple_exact_external_identity_candidates_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="ambiguous",
            identifier=identifier,
            candidate_concept_ids=("#V#record_a", "#V#record_b"),
            resolution_source="persisted_identity_marker",
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Catalogue entity",
                "kind": "instance",
                "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
            }
        ],
    )

    item = result["results"][0]
    assert result["effect_status"] == "failed"
    assert item["error_code"] == "ambiguous_external_identity"
    assert item["candidate_concept_ids"] == ["#V#record_a", "#V#record_b"]
    assert item["external_identity_resolution_source"] == "persisted_identity_marker"
    assert item["changed"] is False
    assert create_calls == []


def test_new_external_identity_uses_generic_stable_id_and_persists_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_create: dict[str, Any] = {}
    persistence_calls: list[dict[str, Any]] = []

    _patch_external_identity_preflights(
        monkeypatch,
        actor_user_id="#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="no_visible_identity_evidence",
        ),
    )

    def _create(**kwargs: Any) -> dict[str, Any]:
        captured_create.update(kwargs)
        concept_id = str(kwargs["canonical_concept_id_override"])
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **kwargs: persistence_calls.append(kwargs)
        or {"success": True, "writes": [{"relation_id": "rel-1"}], "failures": []},
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Mutable display label",
                "kind": "instance",
                "external_identifiers": [
                    {
                        "scheme": "catalogue",
                        "canonical_value": "Record/42:revision=3",
                    }
                ],
            }
        ],
        duplicate_resolution_mode="canonical_id_only",
    )

    stable_id = captured_create["canonical_concept_id_override"]
    assert isinstance(stable_id, str)
    assert stable_id.startswith("#V#external_identity_catalogue_")
    assert "_scope_" in stable_id
    assert captured_create["allow_duplicate_instance_suffix"] is False
    assert result["success"] is True
    assert result["created_concept_ids"] == [stable_id]
    assert persistence_calls[0]["concept_id"] == stable_id
    assert persistence_calls[0]["identifiers"][0].scheme == "catalogue"
    assert persistence_calls[0]["identifiers"][0].value == "Record/42:revision=3"


def test_external_identity_miss_bypasses_title_and_semantic_duplicate_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    repository_reads: list[dict[str, Any]] = []
    semantic_calls: list[str] = []
    create_calls: list[dict[str, Any]] = []

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        repository_reads.append(query)
        if query.get("concept_id") == "#V#mutable_display_label":
            return {
                "concept_id": "#V#mutable_display_label",
                "relationships": {
                    "is_an_instance_of": [_EXTERNAL_IDENTITY_PARENT_ID],
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="no_visible_identity_evidence",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **kwargs: semantic_calls.append(str(kwargs.get("name")))
        or _not_found_resolution(),
    )

    def _create(**kwargs: Any) -> dict[str, Any]:
        create_calls.append(kwargs)
        concept_id = str(kwargs["canonical_concept_id_override"])
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: {"success": True, "writes": [], "failures": []},
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Mutable display label",
                "kind": "instance",
                "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
            }
        ],
    )

    assert result["success"] is True
    assert len(create_calls) == 1
    assert semantic_calls == []
    assert "#V#mutable_display_label" not in {
        query.get("concept_id") for query in repository_reads
    }


def test_external_identity_reuse_conflicts_only_on_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(monkeypatch)
    existing_id = "#V#catalogued_type"
    create_calls: list[dict[str, Any]] = []

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if query.get("concept_id") == existing_id:
            return {
                "concept_id": existing_id,
                "relationships": {
                    "is_a_type_of": [_ALTERNATE_IDENTITY_PARENT_ID],
                    "is_an_instance_of": [],
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="resolved",
            identifier=identifier,
            candidate_concept_ids=(existing_id,),
            resolution_source="persisted_identity_marker",
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Requested instance",
                "kind": "instance",
                "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
            }
        ],
    )

    item = result["results"][0]
    assert result["effect_status"] == "failed"
    assert item["error_code"] == "external_identity_conflict"
    assert item["identity_mismatch_reasons"] == ["kind_mismatch"]
    assert "parent_mismatch" not in item["identity_mismatch_reasons"]
    assert item["existing_concept_id"] == existing_id
    assert create_calls == []


def test_direct_namespace_binds_actor_during_external_identity_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_actors: list[tuple[str | None, str | None]] = []
    captured_create: dict[str, Any] = {}

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda parent_id: ParentResolutionResult(
            requested_parent_id=parent_id,
            canonical_parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
            resolved_parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
        ),
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )

    def _resolve(identifier, **_kwargs):
        from src.backend.security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        observed_actors.append(
            (
                get_effective_user_concept_id(),
                get_effective_organisation_concept_id(),
            )
        )
        return ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="no_visible_identity_evidence",
        )

    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        _resolve,
    )

    def _create(**kwargs: Any) -> dict[str, Any]:
        captured_create.update(kwargs)
        concept_id = str(kwargs["canonical_concept_id_override"])
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: {"success": True, "writes": [], "failures": []},
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        namespace="#V#user_a@org_a",
        concepts=[
            {
                "name": "Directly created entity",
                "kind": "instance",
                "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
            }
        ],
    )

    assert result["success"] is True
    assert observed_actors == [("#V#user_a", "#V#org_a")]
    assert "_scope_" in captured_create["canonical_concept_id_override"]


def test_hidden_atomic_external_identity_collision_is_not_existing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(
        monkeypatch,
        actor_user_id="#V#user_a",
        actor_org_id="#V#org_a",
    )

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="no_visible_identity_evidence",
        ),
    )

    def _collision(**kwargs: Any) -> dict[str, Any]:
        concept_id = str(kwargs["canonical_concept_id_override"])
        return {
            "success": False,
            "changed": False,
            "error_code": "already_exists",
            "existing_concept_id": concept_id,
            "canonical_concept_id": concept_id,
            "concept": None,
        }

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _collision,
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Collision candidate",
                "kind": "instance",
                "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
            }
        ],
    )

    item = result["results"][0]
    assert result["success"] is False
    assert result["effect_status"] == "failed"
    assert result["changed"] is False
    assert result["already_existed"] == 0
    assert item["error_code"] == "external_identity_collision_unverified"
    assert item.get("existing_concept_id") is None


def test_external_identity_persistence_failure_makes_result_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(
        monkeypatch,
        actor_user_id="#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="no_visible_identity_evidence",
        ),
    )

    def _create(**kwargs: Any) -> dict[str, Any]:
        concept_id = str(kwargs["canonical_concept_id_override"])
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: {
            "success": False,
            "writes": [],
            "failures": [
                {
                    "stage": "external_identity_persistence",
                    "scheme": "catalogue",
                    "value": "record-42",
                    "error": "marker write failed",
                }
            ],
        },
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Catalogue entity",
                "kind": "instance",
                "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
            }
        ],
    )

    assert result["effect_status"] == "partial"
    assert result["success"] is False
    assert result["changed"] is True
    assert result["successful"] == 1
    assert result["partial_failure_count"] == 1
    assert result["partial_failures"][0] == {
        "concept_id": result["created_concept_ids"][0],
        "stage": "external_identity_persistence",
        "scheme": "catalogue",
        "value": "record-42",
        "error": "marker write failed",
    }


def test_external_identity_marker_uncertainty_propagates_to_batch_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_external_identity_preflights(
        monkeypatch,
        actor_user_id="#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: ExternalIdentityResolution(
            status="not_found",
            identifier=identifier,
            resolution_source="no_visible_identity_evidence",
        ),
    )

    def _create(**kwargs: Any) -> dict[str, Any]:
        concept_id = str(kwargs["canonical_concept_id_override"])
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    marker_failure = {
        "stage": "external_identity_persistence",
        "scheme": "catalogue",
        "value": "record-42",
        "error": "marker acknowledgement lost",
        "outcome": "indeterminate",
    }
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "persist_external_identity_markers",
        lambda **_kwargs: {
            "success": False,
            "effect_status": "indeterminate",
            "changed": None,
            "writes": [],
            "failures": [],
            "indeterminate_failures": [marker_failure],
        },
    )

    result = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                "name": "Catalogue entity",
                "kind": "instance",
                "external_identifiers": [
                    {"scheme": "catalogue", "value": "record-42"}
                ],
            }
        ],
    )

    stable_id = result["created_concept_ids"][0]
    assert result["success"] is False
    assert result["effect_status"] == "indeterminate"
    assert result["changed"] is True
    assert result["successful"] == 1
    assert result["indeterminate_failure_count"] == 1
    assert result["indeterminate_failures"] == [
        {"concept_id": stable_id, **marker_failure}
    ]
    assert result["results"][0]["effect_status"] == "indeterminate"


def test_explicit_stable_candidate_reuse_does_not_repair_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_external_identity_service as identity_service
    from src.backend.services.concept_external_identity_service import (
        canonical_concept_id_for_external_identifiers,
        normalise_create_external_identifiers,
    )

    actor_id = "#V#test_user"
    identifier = normalise_create_external_identifiers(
        external_identifiers={"scheme": "catalogue", "value": "record-42"},
    )[0]
    stable_id = canonical_concept_id_for_external_identifiers(
        [identifier],
        kind="instance",
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        actor_user_id=actor_id,
    )
    assert stable_id is not None

    _patch_external_identity_preflights(
        monkeypatch,
        actor_user_id=actor_id,
    )
    existing_ids: set[str] = set()
    create_calls: list[str] = []
    persistence_calls: list[str] = []

    monkeypatch.setattr(
        identity_service,
        "_identity_marker_relation_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        identity_service,
        "_legacy_text_reference_candidates",
        lambda _identifier: set(),
    )
    monkeypatch.setattr(
        identity_service,
        "_confirmed_candidate_ids",
        lambda _identifier, _candidate_ids: set(),
    )
    monkeypatch.setattr(
        identity_service,
        "_existing_visible_concept_ids",
        lambda candidate_ids: {
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id in existing_ids
        },
    )

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        concept_id = query.get("concept_id")
        if concept_id == stable_id and stable_id in existing_ids:
            return {
                "concept_id": stable_id,
                "relationships": {
                    "is_an_instance_of": [_EXTERNAL_IDENTITY_PARENT_ID],
                    "is_a_type_of": [],
                },
            }
        return None

    def _create(**kwargs: Any) -> dict[str, Any]:
        concept_id = str(kwargs["canonical_concept_id_override"])
        create_calls.append(concept_id)
        existing_ids.add(concept_id)
        return {
            "success": True,
            "concept": {"concept_id": concept_id},
            "canonical_concept_id": concept_id,
        }

    def _persist(**kwargs: Any) -> dict[str, Any]:
        persistence_calls.append(str(kwargs["concept_id"]))
        if len(persistence_calls) == 1:
            return {
                "success": False,
                "effect_status": "indeterminate",
                "changed": None,
                "writes": [],
                "failures": [],
                "indeterminate_failures": [
                    {
                        "stage": "external_identity_persistence",
                        "outcome": "indeterminate",
                    }
                ],
            }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "writes": [{"relation_created": True}],
            "failures": [],
            "indeterminate_failures": [],
        }

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        _create,
    )
    monkeypatch.setattr(
        identity_service,
        "persist_external_identity_markers",
        _persist,
    )

    concept_item = {
        "name": "Catalogue entity",
        "kind": "instance",
        "external_identifiers": [{"scheme": "catalogue", "value": "record-42"}],
    }
    first = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[concept_item],
    )
    repaired = _create_concepts(
        parent_id=_EXTERNAL_IDENTITY_PARENT_ID,
        concepts=[
            {
                **concept_item,
                "identity_candidate_concept_ids": [stable_id],
            }
        ],
    )

    assert first["effect_status"] == "indeterminate"
    assert first["changed"] is True
    assert first["created_concept_ids"] == [stable_id]
    assert repaired["success"] is True
    assert repaired["effect_status"] == "succeeded"
    assert repaired["changed"] is False
    assert repaired["already_existed"] == 1
    assert repaired["results"][0]["external_identity_resolution_source"] == (
        "caller_confirmed_stable_identity"
    )
    assert create_calls == [stable_id]
    assert persistence_calls == [stable_id]
