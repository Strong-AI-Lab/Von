"""Tests for blocked parent types (JVNAUTOSCI-1072).

Using #V#thing as parent type via is_a_type_of or is_an_instance_of is blocked.
Agents must search for appropriate parent types instead.

Originally JVNAUTOSCI-1010 was a soft warning but upgraded to hard block in JVNAUTOSCI-1072.

Also tests soft warnings on retrieval for concepts that already have vacuous typing
(grandfathered concepts that need repair).
"""

import types


def test_add_structural_is_a_type_of_thing_is_blocked(monkeypatch):
    """Adding is_a_type_of #V#thing is blocked with an informative error."""
    from src.backend.services import relationship_write_service as rws

    calls = {"find_one": [], "update_one": [], "ensure": []}

    def _fake_find_one(filter_doc, projection=None):
        calls["find_one"].append({"filter": filter_doc, "projection": projection})
        cid = filter_doc.get("concept_id")
        if cid == "#V#my_concept":
            return {"concept_id": "#V#my_concept", "relationships": {}}
        if cid == "#V#thing":
            return {"concept_id": "#V#thing", "relationships": {}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        calls["ensure"].append({"concept_id": concept_id, "rel_kind": rel_kind})
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        calls["update_one"].append(
            {"filter": filter_doc, "update": update_doc, "upsert": upsert}
        )
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = rws.add_structural_relationship(
        source_id="#V#my_concept",
        predicate="is_a_type_of",
        target_id="#V#thing",
    )

    # Operation is blocked
    assert result["success"] is False
    assert result["error"] == "blocked_parent_type"
    assert "#V#thing" in result["message"]
    assert "semantically close parent" in result["message"]
    assert "create" in result["message"].lower()
    assert "suggested_alternatives" in result
    assert len(result["suggested_alternatives"]) > 0

    # Ensure no update was made
    assert len(calls["update_one"]) == 0


def test_add_structural_is_a_type_of_specific_type_no_warning(monkeypatch):
    """Adding is_a_type_of to a specific type does not produce a warning."""
    from src.backend.services import relationship_write_service as rws

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#my_concept":
            return {"concept_id": "#V#my_concept", "relationships": {}}
        if cid == "#V#physical_object":
            return {"concept_id": "#V#physical_object", "relationships": {}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = rws.add_structural_relationship(
        source_id="#V#my_concept",
        predicate="is_a_type_of",
        target_id="#V#physical_object",
    )

    # Operation succeeds
    assert result["success"] is True
    assert result["predicate"] == "is_a_type_of"

    # No warning for specific supertypes
    assert "warning" not in result


def test_add_structural_is_an_instance_of_thing_is_blocked(monkeypatch):
    """Adding is_an_instance_of #V#thing is also blocked (JVNAUTOSCI-1072).

    Both is_a_type_of and is_an_instance_of are now blocked for #V#thing.
    """
    from src.backend.services import relationship_write_service as rws

    calls = {"update_one": []}

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#my_instance":
            return {"concept_id": "#V#my_instance", "relationships": {}}
        if cid == "#V#thing":
            return {"concept_id": "#V#thing", "relationships": {}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        calls["update_one"].append({"filter": filter_doc, "update": update_doc})
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = rws.add_structural_relationship(
        source_id="#V#my_instance",
        predicate="is_an_instance_of",
        target_id="#V#thing",
    )

    # Operation is blocked
    assert result["success"] is False
    assert result["error"] == "blocked_parent_type"
    assert "#V#thing" in result["message"]
    assert "semantically close parent" in result["message"]
    assert "create" in result["message"].lower()
    assert "suggested_alternatives" in result

    # Ensure no update was made
    assert len(calls["update_one"]) == 0


def test_mcp_add_relationship_blocks_thing_as_target(monkeypatch):
    """MCP add_relationship tool blocks #V#thing as parent type."""
    from src.backend.integrations.internal_mcp import catalogue

    calls = {"update_one": []}

    def _fake_find_one(filter_doc, projection=None):
        cid = filter_doc.get("concept_id")
        if cid == "#V#source":
            if projection is None:
                return {"concept_id": "#V#source", "relationships": {}}
            return {"concept_id": "#V#source", "relationships": {"is_a_type_of": []}}
        if cid == "#V#thing":
            if projection is None:
                return {"concept_id": "#V#thing", "relationships": {}}
            return {"concept_id": "#V#thing", "relationships": {"has_subtype": []}}
        return None

    def _fake_ensure_relationship_array(concept_id, rel_kind):
        return False

    def _fake_update_one(filter_doc, update_doc, upsert=False):
        calls["update_one"].append({"filter": filter_doc, "update": update_doc})
        return types.SimpleNamespace(modified_count=1, matched_count=1)

    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.find_one",
        _fake_find_one,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository._ensure_relationship_array",
        _fake_ensure_relationship_array,
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.ConceptsRepository.update_one",
        _fake_update_one,
    )

    result = catalogue._add_relationship(
        source_id="#V#source", predicate="typeOf", target="#V#thing"
    )

    # Operation is blocked
    assert result["success"] is False
    assert result.get("error") == "blocked_parent_type"

    # Ensure no update was made
    assert len(calls["update_one"]) == 0


def test_mcp_create_concepts_blocks_thing_as_parent():
    """MCP create_concepts tool blocks #V#thing as parent_id (JVNAUTOSCI-1072)."""
    from src.backend.integrations.internal_mcp import catalogue

    result = catalogue._create_concepts(
        parent_id="#V#thing",
        concepts=[{"name": "Test Concept", "kind": "type"}],
    )

    # Operation is blocked
    assert (
        result.get("success") is False or result.get("error") == "blocked_parent_type"
    )
    assert "blocked_parent_type" in str(result)
    assert "#V#thing" in str(result)
    assert "semantically close parent" in str(result).lower()


# --- Tests for detect_vacuous_typing (soft warning on retrieval) ---


def test_detect_vacuous_typing_returns_none_for_proper_type():
    """No warning for types with specific supertypes."""
    from src.backend.services.relationship_write_service import detect_vacuous_typing

    concept = {
        "concept_id": "#V#my_type",
        "relationships": {"is_a_type_of": ["#V#physical_object"]},
    }
    result = detect_vacuous_typing(concept)
    assert result is None


def test_detect_vacuous_typing_returns_none_for_proper_instance():
    """No warning for instances with specific types."""
    from src.backend.services.relationship_write_service import detect_vacuous_typing

    concept = {
        "concept_id": "#V#my_instance",
        "relationships": {"is_an_instance_of": ["#V#person"]},
    }
    result = detect_vacuous_typing(concept)
    assert result is None


def test_detect_vacuous_typing_warns_for_type_of_thing():
    """Soft warning for types with only #V#thing as supertype."""
    from src.backend.services.relationship_write_service import detect_vacuous_typing

    concept = {
        "concept_id": "#V#vacuous_type",
        "relationships": {"is_a_type_of": ["#V#thing"]},
    }
    result = detect_vacuous_typing(concept)
    assert result is not None
    assert result["code"] == "vacuous_typing"
    assert "#V#vacuous_type" in result["message"]
    assert "vacuous_is_a_type_of" in result
    assert "#V#thing" in result["vacuous_is_a_type_of"]
    assert "suggested_alternatives" in result


def test_detect_vacuous_typing_warns_for_instance_of_thing():
    """Soft warning for instances with only #V#thing as type."""
    from src.backend.services.relationship_write_service import detect_vacuous_typing

    concept = {
        "concept_id": "#V#vacuous_instance",
        "relationships": {"is_an_instance_of": ["#V#thing"]},
    }
    result = detect_vacuous_typing(concept)
    assert result is not None
    assert result["code"] == "vacuous_typing"
    assert "#V#vacuous_instance" in result["message"]
    assert "vacuous_is_an_instance_of" in result
    assert "#V#thing" in result["vacuous_is_an_instance_of"]
    assert "suggested_alternatives" in result


def test_detect_vacuous_typing_no_warning_for_empty_relationships():
    """No warning for concepts with no type/instance relationships."""
    from src.backend.services.relationship_write_service import detect_vacuous_typing

    concept = {
        "concept_id": "#V#no_parents",
        "relationships": {},
    }
    result = detect_vacuous_typing(concept)
    assert result is None


def test_detect_vacuous_typing_no_warning_for_mixed_parents():
    """No warning if there's at least one non-thing parent."""
    from src.backend.services.relationship_write_service import detect_vacuous_typing

    concept = {
        "concept_id": "#V#mixed_type",
        "relationships": {"is_a_type_of": ["#V#thing", "#V#physical_object"]},
    }
    result = detect_vacuous_typing(concept)
    assert result is None


def test_mcp_fetch_concept_adds_vacuous_warning(monkeypatch):
    """MCP fetch_concept adds _vacuous_typing_warning for grandfathered concepts."""
    from src.backend.integrations.internal_mcp import catalogue

    # Mock the concept service to return a concept with vacuous typing
    def _fake_get_concept(concept_id):
        return {
            "concept_id": "#V#old_vacuous_concept",
            "relationships": {"is_a_type_of": ["#V#thing"]},
        }

    def _fake_enrich(concept):
        return concept

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id_exact",
        _fake_get_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.enrich_concept_with_text_relations",
        _fake_enrich,
    )

    result = catalogue._get_concept_by_concept_id(concept_id="#V#old_vacuous_concept")

    assert result is not None
    assert "_vacuous_typing_warning" in result
    assert result["_vacuous_typing_warning"]["code"] == "vacuous_typing"
    assert "suggested_alternatives" in result["_vacuous_typing_warning"]


def test_gateway_error_response_bypasses_output_validation(monkeypatch):
    """Error responses (success=False) bypass output schema validation (JVNAUTOSCI-1072 fix).

    When a tool returns make_error_response(), the gateway should not validate
    against the tool's success output schema – otherwise the LLM sees a confusing
    'Missing required field' error instead of the actual error message.
    """
    from src.backend.integrations.internal_mcp.gateway import (
        InternalMCPGateway,
        MethodDefinition,
        MethodCatalogue,
    )
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
    from src.backend.integrations.internal_mcp.schemas import (
        Schema,
        make_error_response,
    )

    # Define a tool that returns an error response with output schema expecting different fields
    def handler_that_errors(**kwargs):
        return make_error_response(
            "my_error_code",
            "Something went wrong",
            suggestions=["Try again"],
        )

    # Output schema expects 'results', 'total', 'successful' – but error response won't have them
    output_schema = Schema(
        required={"results": list, "total": int, "successful": int},
        description="test output",
    )

    definition = MethodDefinition(
        name="test_error_tool",
        handler=handler_that_errors,
        input_schema=Schema(required={}, allow_unknown=True),
        output_schema=output_schema,
        description="Tool that returns error",
    )

    # Create catalogue and register the tool
    catalogue = MethodCatalogue()
    catalogue.register(definition)

    # Create gateway with the catalogue
    transport = InternalMCPTransport()
    gateway = InternalMCPGateway(catalogue=catalogue, transport=transport, enabled=True)

    # Execute the tool – this should NOT raise SchemaValidationError
    result = gateway.invoke(method_name="test_error_tool", payload={})

    # Verify we got the error response, not a SchemaValidationError
    assert result.payload["success"] is False
    assert result.payload["error_code"] == "my_error_code"
    assert result.payload["error"] == "Something went wrong"
