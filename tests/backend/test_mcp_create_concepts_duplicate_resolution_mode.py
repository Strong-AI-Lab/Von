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
from src.backend.services.concept_external_identity_service import (
    ExternalIdentityResolution,
)
from src.backend.utils.concept_id_utils import canonicalise_vontology_concept_id

_PARENT_ID = "#V#abstract_object"
_PAPER_PARENT_ID = "#V#scholarly_article"


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


def _patch_paper_preflights(
    monkeypatch: pytest.MonkeyPatch,
    *,
    actor_user_id: str | None = None,
    actor_org_id: str | None = None,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda parent_id: ParentResolutionResult(
            requested_parent_id=parent_id,
            canonical_parent_id=_PAPER_PARENT_ID,
            resolved_parent_id=_PAPER_PARENT_ID,
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


def test_canonical_id_only_reuses_external_identity_before_title_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_paper_preflights(monkeypatch)
    existing_id = "#V#legacy_title_derived_paper"
    create_calls: list[dict[str, Any]] = []

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if query.get("concept_id") == existing_id:
            return {
                "concept_id": existing_id,
                "relationships": {
                    "is_an_instance_of": ["#V#paper_on_arxiv"],
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
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_PAPER_PARENT_ID,
        concepts=[
            {
                "name": "arXiv:2506.03346 — Short title",
                "kind": "instance",
            }
        ],
        duplicate_resolution_mode="canonical_id_only",
    )

    item = result["results"][0]
    assert result["successful"] == 0
    assert result["already_existed"] == 1
    assert item["existing_concept_id"] == existing_id
    assert item["duplicate_match_source"] == "external_identifier:arxiv"
    assert create_calls == []


def test_multiple_external_identity_candidates_fail_closed_without_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_paper_preflights(monkeypatch)
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
            candidate_concept_ids=("#V#paper_a", "#V#paper_b"),
        ),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_PAPER_PARENT_ID,
        concepts=[
            {
                "name": "Coral biodiversity paper",
                "kind": "instance",
                "external_identifiers": [{"scheme": "arxiv", "value": "2506.03346v1"}],
            }
        ],
        duplicate_resolution_mode="canonical_id_only",
    )

    item = result["results"][0]
    assert result["effect_status"] == "failed"
    assert item["error_code"] == "ambiguous_external_identity"
    assert item["candidate_concept_ids"] == ["#V#paper_a", "#V#paper_b"]
    assert item["changed"] is False
    assert create_calls == []


def test_new_identified_paper_uses_stable_id_and_persists_identity_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_create: dict[str, Any] = {}
    persistence_calls: list[dict[str, Any]] = []

    _patch_paper_preflights(
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
        "persist_external_identity_names",
        lambda **kwargs: persistence_calls.append(kwargs)
        or {"success": True, "writes": [{"relation_id": "rel-1"}], "failures": []},
    )

    result = _create_concepts(
        parent_id=_PAPER_PARENT_ID,
        concepts=[
            {
                "name": "Negligible effects of environmental fluctuations",
                "kind": "instance",
                "external_identifiers": [
                    {
                        "scheme": "arxiv",
                        "value": "https://arxiv.org/abs/2506.03346v2",
                    }
                ],
            }
        ],
        duplicate_resolution_mode="canonical_id_only",
    )

    stable_id = captured_create["canonical_concept_id_override"]
    assert isinstance(stable_id, str)
    assert stable_id.startswith("#V#paper_on_arxiv_2506_03346_")
    assert captured_create["allow_duplicate_instance_suffix"] is False
    assert result["success"] is True
    assert result["created_concept_ids"] == [stable_id]
    assert persistence_calls[0]["concept_id"] == stable_id
    assert persistence_calls[0]["identifiers"][0].value == "2506.03346"


def test_external_identity_miss_bypasses_title_derived_duplicate_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_paper_preflights(monkeypatch)
    repository_reads: list[dict[str, Any]] = []
    create_calls: list[dict[str, Any]] = []

    def _find_one(
        query: dict[str, Any],
        _projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        repository_reads.append(query)
        if query.get("concept_id") == "#V#a_paper":
            return {
                "concept_id": "#V#a_paper",
                "relationships": {
                    "is_an_instance_of": [_PAPER_PARENT_ID],
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
        ),
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
        "persist_external_identity_names",
        lambda **_kwargs: {"success": True, "writes": [], "failures": []},
    )

    result = _create_concepts(
        parent_id=_PAPER_PARENT_ID,
        concepts=[
            {
                "name": "A paper",
                "kind": "instance",
                "external_identifiers": [{"scheme": "arxiv", "value": "2506.03346"}],
            }
        ],
    )

    assert result["success"] is True
    assert len(create_calls) == 1
    assert "#V#a_paper" not in {query.get("concept_id") for query in repository_reads}


def test_direct_stdio_namespace_binds_actor_during_external_identity_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_actors: list[tuple[str | None, str | None]] = []
    captured_create: dict[str, Any] = {}

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda parent_id: ParentResolutionResult(
            requested_parent_id=parent_id,
            canonical_parent_id=_PAPER_PARENT_ID,
            resolved_parent_id=_PAPER_PARENT_ID,
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
        "persist_external_identity_names",
        lambda **_kwargs: {"success": True, "writes": [], "failures": []},
    )

    result = _create_concepts(
        parent_id=_PAPER_PARENT_ID,
        namespace="#V#user_a@org_a",
        concepts=[
            {
                "name": "A directly-created paper",
                "kind": "instance",
                "external_identifiers": [{"scheme": "arxiv", "value": "2506.03346"}],
            }
        ],
    )

    assert result["success"] is True
    assert observed_actors == [("#V#user_a", "#V#org_a")]
    assert "_scope_" in captured_create["canonical_concept_id_override"]


def test_hidden_atomic_collision_is_not_reported_as_existing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_paper_preflights(
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
        parent_id=_PAPER_PARENT_ID,
        concepts=[
            {
                "name": "A hidden-collision paper",
                "kind": "instance",
                "external_identifiers": [{"scheme": "arxiv", "value": "2506.03346"}],
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


def test_new_external_identity_with_non_paper_parent_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_preflights(monkeypatch)
    create_calls: list[dict[str, Any]] = []
    resolver_calls: list[str] = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: resolver_calls.append(identifier.value),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_PARENT_ID,
        concepts=[
            {
                "name": "arXiv:2506.03346 — Paper under a non-paper parent",
                "kind": "instance",
            }
        ],
    )

    assert result["effect_status"] == "failed"
    assert result["changed"] is False
    assert result["results"][0]["error_code"] == (
        "external_identity_parent_incompatible"
    )
    assert resolver_calls == []
    assert create_calls == []


def test_new_external_identity_with_non_instance_kind_fails_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_paper_preflights(monkeypatch)
    resolver_calls: list[str] = []
    create_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "src.backend.services.concept_external_identity_service."
        "resolve_external_identity_candidates",
        lambda identifier, **_kwargs: resolver_calls.append(identifier.value),
    )
    monkeypatch.setattr(
        "src.backend.vontology.utils_vontology.create_vontology_concept",
        lambda **kwargs: create_calls.append(kwargs),
    )

    result = _create_concepts(
        parent_id=_PAPER_PARENT_ID,
        concepts=[
            {
                "name": "A paper identity asserted for a type",
                "kind": "type",
                "external_identifiers": [{"scheme": "arxiv", "value": "2506.03346"}],
            }
        ],
    )

    assert result["effect_status"] == "failed"
    assert result["results"][0]["error_code"] == (
        "external_identity_parent_incompatible"
    )
    assert resolver_calls == []
    assert create_calls == []


def test_identity_persistence_failure_makes_create_result_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_paper_preflights(
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
        "persist_external_identity_names",
        lambda **_kwargs: {
            "success": False,
            "writes": [{"relation_id": "rel-base-id"}],
            "failures": [
                {
                    "stage": "external_identity_persistence",
                    "value": "https://arxiv.org/abs/2506.03346",
                    "error": "write failed",
                }
            ],
        },
    )

    result = _create_concepts(
        parent_id=_PAPER_PARENT_ID,
        concepts=[
            {
                "name": "A paper",
                "kind": "instance",
                "external_identifiers": [{"scheme": "arxiv", "value": "2506.03346"}],
            }
        ],
    )

    assert result["effect_status"] == "partial"
    assert result["success"] is False
    assert result["changed"] is True
    assert result["successful"] == 1
    assert result["partial_failure_count"] == 1
    assert result["partial_failures"][0]["stage"] == "external_identity_persistence"
