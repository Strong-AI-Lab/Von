"""Tests for standardised MCP error response structure.

These tests verify that:
1. MCPErrorResponse dataclass produces correct output
2. make_error_response helper creates consistent error dicts
3. Write handlers return properly structured error responses
"""

import pytest


class TestMCPErrorResponseDataclass:
    """Tests for MCPErrorResponse dataclass."""

    def test_to_dict_minimal(self):
        """MCPErrorResponse.to_dict() with only required fields."""
        from src.backend.integrations.internal_mcp.schemas import MCPErrorResponse

        err = MCPErrorResponse(error_code="TEST_ERROR", message="Test message")
        result = err.to_dict()

        assert result["success"] is False
        assert result["error"] == "Test message"
        assert result["error_code"] == "TEST_ERROR"
        # Optional fields are omitted when None for cleaner output
        assert "error_details" not in result
        assert "suggestions" not in result
        assert "related_concept_ids" not in result

    def test_to_dict_full(self):
        """MCPErrorResponse.to_dict() with all fields populated."""
        from src.backend.integrations.internal_mcp.schemas import MCPErrorResponse

        err = MCPErrorResponse(
            error_code="FULL_ERROR",
            message="Full error message",
            details={"key": "value"},
            suggestions=["Try this", "Or that"],
            related_concept_ids=["#V#concept1", "#V#concept2"],
        )
        result = err.to_dict()

        assert result["success"] is False
        assert result["error"] == "Full error message"
        assert result["error_code"] == "FULL_ERROR"
        assert result["error_details"] == {"key": "value"}
        assert result["suggestions"] == ["Try this", "Or that"]
        assert result["related_concept_ids"] == ["#V#concept1", "#V#concept2"]


class TestMakeErrorResponse:
    """Tests for make_error_response helper function."""

    def test_minimal_call(self):
        """make_error_response with only required args."""
        from src.backend.integrations.internal_mcp.schemas import make_error_response

        result = make_error_response("SIMPLE_ERROR", "Simple message")

        assert result["success"] is False
        assert result["error"] == "Simple message"
        assert result["error_code"] == "SIMPLE_ERROR"
        # Optional fields omitted when not provided
        assert "error_details" not in result
        assert "suggestions" not in result
        assert "related_concept_ids" not in result

    def test_with_suggestions(self):
        """make_error_response with suggestions for LLM guidance."""
        from src.backend.integrations.internal_mcp.schemas import make_error_response

        result = make_error_response(
            "MISSING_PARAM",
            "Missing concept_id",
            suggestions=["Use concept_search to find valid concept IDs"],
        )

        assert result["success"] is False
        assert result["error_code"] == "MISSING_PARAM"
        assert "Use concept_search" in result["suggestions"][0]

    def test_with_related_concepts(self):
        """make_error_response with related concept IDs."""
        from src.backend.integrations.internal_mcp.schemas import make_error_response

        result = make_error_response(
            "RELATIONSHIP_EXISTS",
            "Relationship already exists",
            related_concept_ids=["#V#source", "#V#target"],
        )

        assert result["related_concept_ids"] == ["#V#source", "#V#target"]

    def test_with_details(self):
        """make_error_response with error_details dict."""
        from src.backend.integrations.internal_mcp.schemas import make_error_response

        result = make_error_response(
            "VALIDATION_ERROR",
            "Validation failed",
            details={"field": "name", "reason": "too_short"},
        )

        assert result["error_details"] == {"field": "name", "reason": "too_short"}


class TestHandlerErrorStructure:
    """Tests that handlers return properly structured error responses."""

    def test_add_relationship_missing_params(self):
        """_add_relationship returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("add_relationship")
        assert method_def is not None

        # Missing source_concept_id - access handler via method_def.handler
        result = method_def.handler(
            target_concept_id="#V#target", predicate_id="#V#pred"
        )

        assert result["success"] is False
        assert "error_code" in result
        assert "suggestions" in result
        assert isinstance(result["suggestions"], list)

    def test_remove_relationship_missing_params(self):
        """_remove_relationship returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("remove_relationship")
        assert method_def is not None

        # Missing all required params
        result = method_def.handler()

        assert result["success"] is False
        assert "error_code" in result
        assert "suggestions" in result

    def test_create_concepts_missing_params(self):
        """_create_concepts returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("create_concepts")
        assert method_def is not None

        # Empty concepts list
        result = method_def.handler(concepts=[])

        assert result["success"] is False
        assert "error_code" in result

    def test_upsert_text_relation_missing_params(self):
        """_upsert_text_relation returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("upsert_text_relation")
        assert method_def is not None

        # Missing concept_id
        result = method_def.handler(predicate_id="#V#pred", text="some text")

        assert result["success"] is False
        assert "error_code" in result

    def test_delete_concept_missing_params(self):
        """_delete_concept returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("delete_concept")
        assert method_def is not None

        # Missing concept_id
        result = method_def.handler()

        assert result["success"] is False
        assert "error_code" in result

    def test_merge_concepts_missing_params(self):
        """_merge_concepts returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("merge_concepts")
        assert method_def is not None

        # Missing target
        result = method_def.handler(source_concept_id="#V#source")

        assert result["success"] is False
        assert "error_code" in result

    def test_rename_concept_missing_params(self):
        """_rename_concept returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("rename_concept")
        assert method_def is not None

        # Missing new_name
        result = method_def.handler(concept_id="#V#concept")

        assert result["success"] is False
        assert "error_code" in result

    def test_update_concept_missing_params(self):
        """_update_concept returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("update_concept")
        assert method_def is not None

        # Missing concept_id
        result = method_def.handler(updates={"name": "new"})

        assert result["success"] is False
        assert "error_code" in result

    def test_task_create_missing_params(self):
        """_task_create returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("task_create")
        assert method_def is not None

        # Missing title
        result = method_def.handler(description="test desc")

        assert result["success"] is False
        assert "error_code" in result
        assert "suggestions" in result

    def test_task_update_status_missing_params(self):
        """_task_update_status returns structured error for missing params."""
        from src.backend.integrations.internal_mcp.catalogue import (
            build_default_catalogue,
        )

        catalogue = build_default_catalogue()
        method_def = catalogue.get("task_update_status")
        assert method_def is not None

        # Missing task_concept_id
        result = method_def.handler(status="completed")

        assert result["success"] is False
        assert "error_code" in result


class TestErrorResponseConsistency:
    """Tests that all error responses have consistent structure."""

    def test_error_response_always_has_required_keys(self):
        """All error responses must have success, error, and error_code keys."""
        from src.backend.integrations.internal_mcp.schemas import make_error_response

        # Test various error codes - only required keys are always present
        test_cases = [
            ("MISSING_PARAM", "Test"),
            ("NOT_FOUND", "Not found"),
            ("VALIDATION_ERROR", "Invalid"),
            ("UNEXPECTED_ERROR", "Something broke"),
        ]

        for code, msg in test_cases:
            result = make_error_response(code, msg)
            # These are ALWAYS present
            assert "success" in result, f"Missing 'success' for {code}"
            assert "error" in result, f"Missing 'error' for {code}"
            assert "error_code" in result, f"Missing 'error_code' for {code}"
            # Verify values
            assert result["success"] is False
            assert result["error"] == msg
            assert result["error_code"] == code

    def test_error_response_with_optional_keys(self):
        """Error responses include optional keys when provided."""
        from src.backend.integrations.internal_mcp.schemas import make_error_response

        result = make_error_response(
            "TEST",
            "Test message",
            details={"key": "value"},
            suggestions=["suggestion"],
            related_concept_ids=["#V#id"],
        )

        assert result["error_details"] == {"key": "value"}
        assert result["suggestions"] == ["suggestion"]
        assert result["related_concept_ids"] == ["#V#id"]
