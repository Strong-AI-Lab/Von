import pytest


def test_add_relationship_returns_typed_recovery_for_unknown_predicate_name(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    calls = {"find_one": [], "update_one": []}

    def _fake_find_one(filter_doc, projection=None):
        calls["find_one"].append({"filter": filter_doc, "projection": projection})
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        if cid == "#V#target":
            return {"concept_id": "#V#target", "relationships": {}}
        return None

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        calls["update_one"].append(
            {"filter": filter_doc, "update": update_doc, "upsert": upsert}
        )
        raise AssertionError("update_one should not be called for invalid predicates")

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="has_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "predicate_reference_not_found"
    assert result["mutation_outcome"] == "not_started"
    assert result["changed"] is False
    assert result["predicate_resolution"]["status"] == "not_found"
    assert result["recovery_affordances"][0]["action_type"] == (
        "retry_with_predicate_concept_id"
    )
    assert calls["update_one"] == []


def test_add_relationship_resolves_accessible_predicate_name_before_write(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: {
            "concept_id": filter_doc.get("concept_id"),
            "relationships": (
                {"is_an_instance_of": ["#V#predicate"]}
                if filter_doc.get("concept_id") == "#V#depends_on"
                else {}
            ),
        },
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **kwargs: {
            "success": True,
            "status": "resolved",
            "resolved_concept_id": "#V#depends_on",
            "match": {"stage": "exact_name"},
            "candidates": [],
            "audit": [],
        },
    )

    def _add_edge(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "predicate": kwargs["predicate"],
            "target_id": kwargs["target"],
            "modified": True,
        }

    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        _add_edge,
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="Depends on",
        target="#V#target",
    )

    assert result["success"] is True
    assert captured["predicate"] == "#V#depends_on"
    assert result["predicate_resolution"]["status"] == "resolved"
    assert result["predicate_resolution"]["resolved_concept_id"] == (
        "#V#depends_on"
    )


def test_add_relationship_returns_candidates_for_ambiguous_predicate_name(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {"concept_id": "#V#source", "relationships": {}}
            if filter_doc.get("concept_id") == "#V#source"
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **kwargs: {
            "success": True,
            "status": "ambiguous",
            "resolved_concept_id": None,
            "candidates": [
                {"concept_id": "#V#about"},
                {"concept_id": "#V#is_about"},
            ],
            "audit": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an ambiguous predicate must not be written")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="about",
        target="#V#target",
    )

    assert result["success"] is False
    assert result["error_code"] == "predicate_reference_ambiguous"
    assert result["mutation_outcome"] == "not_started"
    assert [
        item["predicate_ref"]["concept_id"]
        for item in result["recovery_affordances"]
    ] == ["#V#about", "#V#is_about"]


def test_add_relationship_typed_reference_can_create_text_predicate(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    events: list[str] = []
    validation_results = iter(
        [
            (
                False,
                "predicate_concept_not_found",
                {"predicate": "#V#has_summary"},
            ),
            (True, None, None),
        ]
    )

    def _find_one(filter_doc, projection=None):
        concept_id = filter_doc.get("concept_id")
        if concept_id == "#V#source":
            return {"concept_id": concept_id, "relationships": {}}
        if concept_id == "#V#has_summary":
            return {
                "concept_id": concept_id,
                "relationships": {
                    "is_an_instance_of": ["#V#binary_text_predicate"]
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_resolution_service.resolve_concept_by_name",
        lambda **kwargs: {
            "success": True,
            "status": "not_found",
            "resolved_concept_id": None,
            "candidates": [],
            "audit": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: next(validation_results),
    )

    def _create_predicate(**kwargs):
        events.append("create")
        assert kwargs["parent_id"] == "#V#binary_text_predicate"
        assert kwargs["concepts"][0]["name"] == "Has summary"
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": ["#V#has_summary"],
        }

    monkeypatch.setattr(catalogue, "_create_concepts", _create_predicate)

    def _upsert_text(**kwargs):
        events.append("text")
        assert kwargs["predicate"] == "#V#has_summary"
        assert kwargs["text"] == "A concise summary"
        return {
            "text_value_id": "text-1",
            "relation_id": "relation-1",
            "relation_created": True,
            "context_updated": False,
        }

    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_text_for_concept",
        _upsert_text,
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        target="A concise summary",
        predicate_ref={
            "name": "Has summary",
            "on_missing": "create_typed_predicate",
            "value_kind": "text",
        },
    )

    assert events == ["create", "text"]
    assert result["success"] is True
    assert result["relationship_type"] == "text_relation"
    assert result["changed"] is True
    assert result["predicate"] == "#V#has_summary"
    assert result["predicate_dependency"]["status"] == "created"


def test_add_relationship_rejects_missing_dynamic_predicate_concepts(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        if cid == "#V#target":
            return {"concept_id": "#V#target", "relationships": {}}
        # Predicate concept does not exist
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="#V#has_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "predicate_concept_not_found"


def test_add_relationship_rejects_dynamic_concepts_that_are_not_predicates(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        if cid == "#V#target":
            return {"concept_id": "#V#target", "relationships": {}}
        if cid == "#V#has_todo_item":
            # Exists, but is not typed as a predicate
            return {
                "concept_id": "#V#has_todo_item",
                "relationships": {"is_an_instance_of": []},
            }
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="#V#has_todo_item", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "predicate_concept_not_typed"


def test_predicate_typing_validation_uses_hard_authority_view(monkeypatch):
    from src.backend.security import access_control
    from src.backend.services.relationship_write_service import (
        validate_predicate_concept,
    )

    def _fake_find_one(filter_doc, projection=None):
        assert access_control._BYPASS.get() is True
        return {
            "concept_id": filter_doc["concept_id"],
            "relationships": {"is_an_instance_of": ["#V#predicate"]},
        }

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    assert validate_predicate_concept("#V#about") == (True, None, None)


def test_add_relationship_returns_structured_error_for_missing_source(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        # Source is missing
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="typeOf", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "source_concept_not_found"
    assert result.get("error_details", {}).get("concept_id") == "#V#source"


def test_add_relationship_returns_structured_error_for_missing_target(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            return {"concept_id": "#V#source", "relationships": {}}
        # Target is missing
        return None

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="typeOf", target="#V#target"
    )

    assert result["success"] is False
    assert result.get("error_code") == "target_not_found"


def test_add_relationship_commit_then_raise_is_indeterminate(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    committed: list[str] = []

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: {
            "concept_id": "#V#source",
            "relationships": {},
        },
    )

    def _commit_then_raise(**_kwargs):
        committed.append("relationship-written")
        raise RuntimeError("lost acknowledgement after commit")

    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        _commit_then_raise,
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="typeOf",
        target="#V#target",
    )

    assert committed == ["relationship-written"]
    assert result["success"] is False
    assert result["error_code"] == "effect_outcome_unknown"
    assert result["effect_status"] == "indeterminate"
    assert result["mutation_outcome"] == "unknown"
    assert result["changed"] is None
    assert result["retryable"] is False
    assert result["recovery_affordances"] == [
        {"action_type": "inspect_operation_state_before_retry"}
    ]


def test_add_relationship_unexpected_pre_dispatch_failure_remains_definite(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("concept store unavailable")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="typeOf",
        target="#V#target",
    )

    assert result["success"] is False
    assert result["error_code"] == "exception"
    assert "mutation_outcome" not in result
    assert result.get("effect_status") != "indeterminate"


def test_add_relationship_creates_and_verifies_exact_missing_predicate_before_edge(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security.access_control import override_current_actor

    events: list[str] = []
    predicate_created = False

    def _fake_find_one(filter_doc, projection=None):
        concept_id = filter_doc.get("concept_id")
        if concept_id == "#V#source":
            return {"concept_id": concept_id, "relationships": {}}
        if concept_id == "#V#target":
            return {"concept_id": concept_id, "relationships": {}}
        if concept_id == "#V#depends_on" and predicate_created:
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": ["#V#predicate"]},
            }
        return None

    validation_count = 0

    def _validate_predicate(predicate, repo=None):
        nonlocal validation_count
        validation_count += 1
        events.append(f"validate:{validation_count}")
        assert predicate == "#V#depends_on"
        if validation_count == 1:
            return (
                False,
                "predicate_concept_not_found",
                {"predicate": predicate},
            )
        assert predicate_created is True
        return True, None, None

    def _create_predicate(**kwargs):
        nonlocal predicate_created
        events.append("create")
        assert kwargs["parent_id"] == "#V#predicate"
        assert kwargs["duplicate_resolution_mode"] == "canonical_id_only"
        assert kwargs["namespace"] == "#V#user@org"
        assert kwargs["created_by_concept_id"] == "#V#user"
        assert kwargs["organisation_concept_id"] == "#V#org"
        assert kwargs["concepts"] == [
            {
                "name": "Depends on",
                "kind": "predicate",
                "description": "Connects an entity to one of its dependencies.",
            }
        ]
        predicate_created = True
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": ["#V#depends_on"],
            "results": [{"success": True, "concept_id": "#V#depends_on"}],
        }

    def _add_edge(**kwargs):
        events.append("edge")
        assert predicate_created is True
        assert kwargs["predicate"] == "#V#depends_on"
        return {
            "success": True,
            "predicate": "#V#depends_on",
            "target_id": "#V#target",
            "modified": True,
        }

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        _validate_predicate,
    )
    monkeypatch.setattr(catalogue, "_create_concepts", _create_predicate)
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        _add_edge,
    )

    with override_current_actor("#V#user", "#V#org"):
        result = catalogue._add_relationship(
            source_id="#V#source",
            predicate="#V#depends_on",
            target="#V#target",
            namespace="#V#user@org",
            predicate_if_missing={
                "name": "Depends on",
                "description": "Connects an entity to one of its dependencies.",
            },
        )

    assert events == ["validate:1", "create", "validate:2", "edge"]
    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["changed"] is True
    assert result["predicate_dependency"]["concept_id"] == "#V#depends_on"
    assert result["predicate_dependency"]["status"] == "created"
    assert result["predicate_dependency"]["verified"] is True


def test_add_relationship_reuses_verified_predicate_without_creation(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: {
            "concept_id": filter_doc["concept_id"],
            "relationships": {"is_an_instance_of": ["#V#predicate"]},
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: (True, None, None),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an existing predicate must not be recreated")
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **kwargs: {
            "success": True,
            "predicate": kwargs["predicate"],
            "target_id": kwargs["target"],
            "modified": True,
        },
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is True
    assert result["predicate_dependency"] == {
        "concept_id": "#V#depends_on",
        "name": "Depends on",
        "status": "reused_existing",
        "verified": True,
        "changed": False,
    }


def test_add_relationship_fails_closed_when_predicate_creation_is_indeterminate(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: (
            False,
            "predicate_concept_not_found",
            {"predicate": predicate},
        ),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: {
            "success": False,
            "effect_status": "indeterminate",
            "changed": None,
            "error_code": "effect_outcome_unknown",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the edge must not be attempted")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is False
    assert result["error_code"] == "effect_outcome_unknown"
    assert result["effect_status"] == "indeterminate"
    assert result["changed"] is None
    assert result["predicate_dependency"]["status"] == "indeterminate"
    assert result["predicate_dependency"]["verified"] is False


def test_add_relationship_stops_after_persisted_predicate_when_cancelled(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp.transport import (
        InternalMCPHandlerCancelled,
    )

    cancellation_checks = 0
    validation_results = iter(
        [
            (
                False,
                "predicate_concept_not_found",
                {"predicate": "#V#depends_on"},
            ),
            (True, None, None),
        ]
    )

    def _raise_after_dependency() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise InternalMCPHandlerCancelled("deadline elapsed")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        _raise_after_dependency,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: next(validation_results),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": ["#V#depends_on"],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the relationship must not be attempted")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert cancellation_checks == 2
    assert result["success"] is False
    assert result["effect_status"] == "partial"
    assert result["changed"] is True
    assert result["mutation_outcome"] == "partial"
    assert result["predicate_dependency"]["verified"] is True
    assert result["partial_failures"][-1] == {
        "stage": "relationship",
        "error_code": "relationship_not_started_after_predicate_dependency",
        "dispatched": False,
    }


def test_add_relationship_reports_partial_when_predicate_persists_but_edge_fails(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    validation_results = iter(
        [
            (
                False,
                "predicate_concept_not_found",
                {"predicate": "#V#depends_on"},
            ),
            (True, None, None),
        ]
    )

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: next(validation_results),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": ["#V#depends_on"],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: {
            "success": False,
            "error": "target_not_found",
            "concept_id": "#V#target",
        },
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is False
    assert result["error_code"] == "target_not_found"
    assert result["effect_status"] == "partial"
    assert result["changed"] is True
    assert result["mutation_outcome"] == "partial"
    assert result["predicate_dependency"]["status"] == "created"
    assert result["predicate_dependency"]["verified"] is True
    assert result["partial_failures"][-1]["stage"] == "relationship"
    assert result["partial_failures"][-1]["error_code"] == "target_not_found"


def test_add_relationship_cancellation_preserves_unchanged_dependency_failure(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp.transport import (
        InternalMCPHandlerCancelled,
    )

    cancellation_checks = 0
    validation_results = iter(
        [
            (
                False,
                "predicate_concept_not_found",
                {"predicate": "#V#depends_on"},
            ),
            (True, None, None),
        ]
    )

    def _cancel_before_edge() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise InternalMCPHandlerCancelled("deadline elapsed")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        _cancel_before_edge,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: next(validation_results),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: {
            "success": False,
            "effect_status": "partial",
            "changed": False,
            "error_code": "concurrent_create_observed",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the relationship must not be attempted")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is False
    assert result["effect_status"] == "partial"
    assert result["changed"] is False
    assert result["mutation_outcome"] == "partial"
    assert result["predicate_dependency"]["status"] == "reused_existing"
    assert result["predicate_dependency"]["verified"] is True
    assert [failure["stage"] for failure in result["partial_failures"]] == [
        "predicate_dependency",
        "relationship",
    ]


def test_add_relationship_edge_failure_preserves_unchanged_dependency_failure(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    validation_results = iter(
        [
            (
                False,
                "predicate_concept_not_found",
                {"predicate": "#V#depends_on"},
            ),
            (True, None, None),
        ]
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: next(validation_results),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: {
            "success": False,
            "effect_status": "partial",
            "changed": False,
            "error_code": "concurrent_create_observed",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: {
            "success": False,
            "error": "target_not_found",
        },
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is False
    assert result["effect_status"] == "partial"
    assert result["changed"] is False
    assert result["mutation_outcome"] == "partial"
    assert result["predicate_dependency"]["status"] == "reused_existing"
    assert result["predicate_dependency"]["verified"] is True
    assert [failure["stage"] for failure in result["partial_failures"]] == [
        "predicate_dependency",
        "relationship",
    ]


def test_add_relationship_preserves_created_dependency_when_validation_raises(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    validation_calls = 0

    def _validate(predicate, repo=None):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            return (
                False,
                "predicate_concept_not_found",
                {"predicate": predicate},
            )
        raise RuntimeError("predicate readback unavailable")

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        _validate,
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": ["#V#depends_on"],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the relationship must not be attempted")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is False
    assert result["effect_status"] == "indeterminate"
    assert result["changed"] is True
    assert result["predicate_dependency"]["concept_id"] == "#V#depends_on"
    assert result["predicate_dependency"]["status"] == "unverified"
    assert result["predicate_dependency"]["verified"] is False
    assert result["known_changes"] == [
        {
            "stage": "predicate_dependency",
            "concept_id": "#V#depends_on",
            "changed": True,
        }
    ]


def test_add_relationship_does_not_create_predicate_when_target_is_missing(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {"concept_id": "#V#source", "relationships": {}}
            if filter_doc.get("concept_id") == "#V#source"
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: (
            False,
            "predicate_concept_not_found",
            {"predicate": predicate},
        ),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a dependency must not be created for a missing target")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#missing_target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is False
    assert result["error_code"] == "target_concept_not_found"
    assert result.get("effect_status") != "partial"
    assert result.get("changed") in {None, False}


def test_add_relationship_propagates_cancellation_without_dependency(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp.transport import (
        InternalMCPHandlerCancelled,
    )

    cancellation_checks = 0

    def _cancel_before_edge() -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1
        if cancellation_checks == 2:
            raise InternalMCPHandlerCancelled("deadline elapsed")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.transport."
        "raise_if_internal_mcp_cancelled",
        _cancel_before_edge,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: {
            "concept_id": filter_doc.get("concept_id"),
            "relationships": {},
        },
    )

    with pytest.raises(InternalMCPHandlerCancelled, match="deadline elapsed"):
        catalogue._add_relationship(
            source_id="#V#source",
            predicate="typeOf",
            target="#V#target",
        )


def test_add_relationship_propagates_cancellation_from_predicate_create(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp.transport import (
        InternalMCPHandlerCancelled,
    )

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: (
            False,
            "predicate_concept_not_found",
            {"predicate": predicate},
        ),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: (_ for _ in ()).throw(
            InternalMCPHandlerCancelled("nested create cancelled")
        ),
    )

    with pytest.raises(InternalMCPHandlerCancelled, match="nested create cancelled"):
        catalogue._add_relationship(
            source_id="#V#source",
            predicate="#V#depends_on",
            target="#V#target",
            predicate_if_missing={"name": "Depends on"},
        )


def test_add_relationship_preserves_known_dependency_when_edge_is_indeterminate(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    validation_results = iter(
        [
            (
                False,
                "predicate_concept_not_found",
                {"predicate": "#V#depends_on"},
            ),
            (True, None, None),
        ]
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {
                "concept_id": filter_doc.get("concept_id"),
                "relationships": {},
            }
            if filter_doc.get("concept_id") in {"#V#source", "#V#target"}
            else None
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.validate_predicate_concept",
        lambda predicate, repo=None: next(validation_results),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_concept_ids": ["#V#depends_on"],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.add_relationship",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("edge acknowledgement lost")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Depends on"},
    )

    assert result["success"] is False
    assert result["effect_status"] == "indeterminate"
    assert result["changed"] is True
    assert result["predicate_dependency"]["status"] == "created"
    assert result["predicate_dependency"]["verified"] is True
    assert result["known_changes"] == [
        {
            "stage": "predicate_dependency",
            "concept_id": "#V#depends_on",
            "changed": True,
        }
    ]


def test_add_relationship_rejects_predicate_dependency_name_mismatch(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        lambda filter_doc, projection=None: (
            {"concept_id": "#V#source", "relationships": {}}
            if filter_doc.get("concept_id") == "#V#source"
            else None
        ),
    )
    monkeypatch.setattr(
        catalogue,
        "_create_concepts",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a mismatched predicate must not be created")
        ),
    )

    result = catalogue._add_relationship(
        source_id="#V#source",
        predicate="#V#depends_on",
        target="#V#target",
        predicate_if_missing={"name": "Related to"},
    )

    assert result["success"] is False
    assert result["error_code"] == "predicate_dependency_identity_mismatch"
    assert result["error_details"]["canonical_dependency_id"] == "#V#related_to"
