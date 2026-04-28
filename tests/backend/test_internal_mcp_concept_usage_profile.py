from __future__ import annotations


def test_concept_usage_profile_tool_is_registered() -> None:
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue

    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()

    assert "get_concept_usage_profile" in methods
    method = catalogue.get("get_concept_usage_profile")
    assert method.category == "read"
    assert sorted(method.input_schema.required) == ["concept_id"]
