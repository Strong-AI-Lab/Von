from __future__ import annotations


def test_internal_mcp_catalogue_registers_rag_concept_tools():
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue

    catalogue = build_default_catalogue()
    methods = set(catalogue.list_methods())

    assert "search_concept_descriptions" in methods
    assert "get_related_concepts" in methods
    assert "index_concept_text" in methods
