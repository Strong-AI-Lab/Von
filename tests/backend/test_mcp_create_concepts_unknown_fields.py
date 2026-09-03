"""
Test that create_concepts tolerates orchestrator context fields (including namespace).

This validates the fix for the "Unexpected field 'namespace'" error that occurred
when callers included additional top-level context.
"""

import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp.catalogue import (
    _create_concepts as _governed_create_concepts,
)
from src.backend.security.visibility_predicates import (
    CANONICAL_SPECIFIC_TO_ORG_PREDICATE,
    CANONICAL_SPECIFIC_TO_USER_PREDICATE,
)

_create_concepts = _governed_create_concepts.__wrapped__


@pytest.fixture(autouse=True)
def seed_core_concepts():
    """Seed core type hierarchy for create_concepts tests."""
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    # Seed core concepts needed for tests
    concepts.delete_many(
        {"concept_id": {"$in": ["#V#thing", "#V#abstract_object", "#V#predicate"]}}
    )
    concepts.insert_one(
        {
            "concept_id": "#V#thing",
            "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#abstract_object",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        }
    )
    concepts.insert_one(
        {
            "concept_id": "#V#predicate",
            "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
        }
    )
    yield
    # Cleanup test-created concepts
    concepts.delete_many(
        {
            "concept_id": {
                "$regex": "^#V#(test_concept_|multi_extra_|test_predicate_|scope_mode_test_|no_scope_context_test_)"
            }
        }
    )


def test_create_concepts_accepts_namespace_field():
    """
    Verify that create_concepts accepts namespace as top-level context.

    Context: JVNAUTOSCI-760 - MCP orchestrator was adding namespace to create_concepts
    calls, causing "Unexpected field 'namespace'" errors. The fix allows namespace
    while still validating required fields.
    """
    import uuid

    # Arrange: payload with top-level 'namespace' context and unique concept name
    unique_name = f"test_concept_ns_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#abstract_object",
        "namespace": "#V#test_user",
        "concepts": [
            {
                "name": unique_name,
                "kind": "type",
                "description": "Test concept with namespace in payload",
            }
        ],
    }

    # Act: call create_concepts with namespace field
    result = _create_concepts(**payload)

    # Assert: should succeed and create concept
    assert result is not None
    assert "results" in result
    assert "total" in result
    assert result["total"] == 1

    # Check that concept was created (will have success=True or concept_id)
    first_result = result["results"][0]
    assert isinstance(first_result, dict)
    # Either success=True or it has a concept_id (depends on backend implementation)
    assert first_result.get("success") is True or "concept_id" in first_result


def test_create_concepts_rejects_missing_required_fields():
    """
    Verify that create_concepts still validates required fields.

    Ensures that allowing unknown fields doesn't break validation of required fields.
    """
    # Arrange: payload missing required 'parent_id'
    payload = {
        "namespace": "#V#test_user",  # Extra field
        "concepts": [{"name": "test_concept", "kind": "type"}],
    }

    # Act & Assert: should fail due to missing required field
    result = _create_concepts(**payload)
    assert "error" in result
    assert "parent_id" in result["error"].lower()


@pytest.mark.parametrize(
    ("concept", "expected_error"),
    [
        ({"name": "test_concept_missing_kind"}, "concept_kind_required"),
        (
            {"name": "test_concept_ambiguous_kind", "kind": "candidature"},
            "invalid_concept_kind",
        ),
    ],
)
def test_create_concepts_requires_an_explicit_supported_kind(
    concept, expected_error
):
    result = _create_concepts(
        parent_id="#V#abstract_object",
        concepts=[concept],
    )

    assert result["successful"] == 0
    assert result["effect_status"] == "failed"
    assert result["results"][0]["error_code"] == expected_error
    assert result["results"][0]["changed"] is False


def test_create_concepts_accepts_multiple_unknown_fields():
    """
    Verify that create_concepts tolerates multiple unknown fields.
    """
    # Arrange: payload with multiple extra fields
    payload = {
        "parent_id": "#V#abstract_object",
        "namespace": "#V#test_user",
        "session_id": "abc123",
        "user_context": {"name": "Test User"},
        "concepts": [{"name": "multi_extra_test", "kind": "instance"}],
    }

    # Act
    result = _create_concepts(**payload)

    # Assert: should succeed
    assert result is not None
    assert result.get("total") == 1


def test_create_concepts_predicate_kind_creates_predicate_instance():
    """
    Verify that predicate kind creates an instance of #V#predicate.

    Context: JVNAUTOSCI-986 - ensure predicate concepts are instances of predicate
    rather than being created as types.
    """
    import uuid

    unique_name = f"test_predicate_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#abstract_object",
        "concepts": [
            {
                "name": unique_name,
                "kind": "predicate",
                "description": "Test predicate concept",
            }
        ],
    }

    result = _create_concepts(**payload)

    assert result is not None
    assert result.get("total") == 1

    first_result = result["results"][0]
    assert isinstance(first_result, dict)
    assert first_result.get("success") is True

    concept = first_result.get("concept") or {}
    relationships = concept.get("relationships") or {}
    instance_of = relationships.get("is_an_instance_of") or []
    type_of = relationships.get("is_a_type_of") or []

    assert "#V#predicate" in instance_of
    assert type_of == []
    first_result = result["results"][0]
    assert isinstance(first_result, dict)
    assert first_result.get("success") is True or "concept_id" in first_result


def test_create_concepts_defaults_to_user_scope_from_namespace():
    """Omitted scope should remain actor-private even when an organisation exists."""
    import uuid

    unique_name = f"scope_mode_test_default_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#abstract_object",
        "namespace": "#V#scope_user@scope_org",
        "concepts": [{"name": unique_name, "kind": "type"}],
    }

    result = _create_concepts(**payload)
    first_result = (result.get("results") or [{}])[0]
    concept = first_result.get("concept") or {}
    relationships = concept.get("relationships") or {}

    assert first_result.get("success") is True
    assert relationships.get(CANONICAL_SPECIFIC_TO_USER_PREDICATE) == [
        "#V#scope_user"
    ]
    assert not relationships.get(CANONICAL_SPECIFIC_TO_ORG_PREDICATE)
    scope_selection = result.get("scope_selection") or {}
    assert scope_selection.get("requested_scope_mode") == "user_only_default"
    assert scope_selection.get("scope_mode_source") == "default.user_only_default"


def test_create_concepts_scope_mode_organisation_general():
    """organisation_general should be org-scoped and not user-scoped."""
    import uuid

    unique_name = f"scope_mode_test_org_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#abstract_object",
        "namespace": "#V#scope_user@scope_org",
        "scope_mode": "organisation_general",
        "concepts": [{"name": unique_name, "kind": "type"}],
    }

    result = _create_concepts(**payload)
    first_result = (result.get("results") or [{}])[0]
    concept = first_result.get("concept") or {}
    relationships = concept.get("relationships") or {}

    assert first_result.get("success") is True
    assert "specific_to_user" not in relationships
    assert relationships.get(CANONICAL_SPECIFIC_TO_ORG_PREDICATE) == [
        "#V#scope_org"
    ]
    scope_selection = result.get("scope_selection") or {}
    assert scope_selection.get("requested_scope_mode") == "organisation_general"
    assert scope_selection.get("scope_mode_source") == "request.scope_mode"
    assert "organisation_general" in (scope_selection.get("effective_scope_modes") or [])


def test_create_concepts_publication_scope_organisation_alias():
    """The natural publication_scope alias must retain organisation audience."""
    import uuid

    unique_name = f"scope_mode_test_publication_alias_{uuid.uuid4().hex[:8]}"
    result = _create_concepts(
        parent_id="#V#abstract_object",
        namespace="#V#scope_user@scope_org",
        publication_scope="organisation",
        concepts=[{"name": unique_name, "kind": "type"}],
    )
    first_result = (result.get("results") or [{}])[0]
    relationships = (first_result.get("concept") or {}).get("relationships") or {}

    assert first_result.get("success") is True
    assert "specific_to_user" not in relationships
    assert relationships.get(CANONICAL_SPECIFIC_TO_ORG_PREDICATE) == ["#V#scope_org"]
    scope_selection = result.get("scope_selection") or {}
    assert scope_selection.get("requested_scope_mode") == "organisation_general"
    assert scope_selection.get("scope_mode_source") == "request.publication_scope"


def test_create_concepts_rejects_conflicting_scope_aliases_before_mutation():
    result = _create_concepts(
        parent_id="#V#abstract_object",
        namespace="#V#scope_user@scope_org",
        scope_mode="user_only_default",
        publication_scope="organisation",
        concepts=[{"name": "scope_mode_test_conflict", "kind": "type"}],
    )

    assert result.get("success") is False
    assert result.get("error_code") == "conflicting_scope_mode_fields"
    assert result.get("effect_status") == "not_started"
    assert result.get("changed") is False
    assert (
        ConceptsRepository.collection().find_one(
            {"concept_id": "#V#scope_mode_test_conflict"}
        )
        is None
    )


def test_create_concepts_scope_mode_global_general():
    """global_general should avoid user/org visibility restrictions."""
    import uuid

    unique_name = f"scope_mode_test_global_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#abstract_object",
        "namespace": "#V#scope_user@scope_org",
        "scope_mode": "global_general",
        "concepts": [{"name": unique_name, "kind": "type"}],
    }

    result = _create_concepts(**payload)
    first_result = (result.get("results") or [{}])[0]
    concept = first_result.get("concept") or {}
    relationships = concept.get("relationships") or {}

    assert first_result.get("success") is True
    assert "specific_to_user" not in relationships
    assert "specific_to_org" not in relationships
    scope_selection = result.get("scope_selection") or {}
    assert scope_selection.get("requested_scope_mode") == "global_general"
    assert scope_selection.get("scope_mode_source") == "request.scope_mode"
    assert "global_general" in (scope_selection.get("effective_scope_modes") or [])


def test_create_concepts_scope_mode_org_general_requires_org_context():
    """organisation_general should return a clear error when org context is missing."""
    payload = {
        "parent_id": "#V#abstract_object",
        "namespace": "#V#scope_user",
        "scope_mode": "organisation_general",
        "concepts": [{"name": "scope_mode_test_missing_org", "kind": "type"}],
    }

    result = _create_concepts(**payload)
    assert result.get("success") is False
    assert result.get("error_code") == "missing_organisation_context"


def test_create_concepts_publication_scope_org_requires_org_context():
    result = _create_concepts(
        parent_id="#V#abstract_object",
        namespace="#V#scope_user",
        publication_scope="organisation",
        concepts=[{"name": "scope_mode_test_missing_org_alias", "kind": "type"}],
    )

    assert result.get("success") is False
    assert result.get("error_code") == "missing_organisation_context"


def test_create_concepts_defaults_to_global_when_context_missing():
    """Without authenticated context, default create_concepts should be global."""
    import uuid

    unique_name = f"no_scope_context_test_{uuid.uuid4().hex[:8]}"
    payload = {
        "parent_id": "#V#abstract_object",
        "concepts": [{"name": unique_name, "kind": "type"}],
    }

    result = _create_concepts(**payload)
    first_result = (result.get("results") or [{}])[0]
    concept = first_result.get("concept") or {}
    relationships = concept.get("relationships") or {}

    assert first_result.get("success") is True
    assert "specific_to_user" not in relationships
    assert "specific_to_org" not in relationships
