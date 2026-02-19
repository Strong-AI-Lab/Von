def test_internal_mcp_catalogue_builds_and_includes_relationship_tools():
    from src.backend.integrations.internal_mcp import build_default_catalogue

    catalogue = build_default_catalogue()
    methods = set(catalogue.list_methods())

    assert "add_relationship" in methods
    assert "remove_relationship" in methods
    assert "turn_execution_list" in methods
    assert "turn_execution_get" in methods
    assert "turn_execution_search_failures" in methods
    assert "turn_execution_build_benchmark" in methods


def test_internal_mcp_gmail_list_messages_accepts_max_results_aliases():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.schemas import validate_payload

    catalogue = build_default_catalogue()
    method = catalogue.get("gmail_list_messages")

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "query": "in:inbox", "max_results": 10},
    )
    assert ok, errors

    ok, errors = validate_payload(
        method.input_schema,
        {"profile": "zhan-gmail", "query": "in:inbox", "maxResults": 10},
    )
    assert ok, errors
