import pytest
from src.backend.integrations.internal_mcp import build_default_catalogue


def test_workflow_list_definitions_exists_and_returns_data():
    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_list_definitions" in methods

    handler = catalogue.get("workflow_list_definitions").handler
    result = handler(limit=10)

    assert result.get("success") is True
    assert "definitions" in result
    assert "count" in result
    assert result["count"] >= 0

    # Check default workflows are present (from registry)
    def_ids = [d["workflow_id"] for d in result["definitions"]]
    # We expect durable workflows to be registered by the factory
    # (rag_sync_workflow is registered in build_durable_workflow_registry)
    assert "#V#rag_text_relation_sync_workflow" in def_ids
    assert "#V#generate_considerations_workflow" in def_ids
