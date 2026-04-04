#!/usr/bin/env python3
"""Check that internal MCP input schemas are configured with allow_unknown=True.

This is a developer utility script (not a pytest test).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.backend.integrations.internal_mcp.catalogue import (  # noqa: E402
    _add_names_input_schema,
    _add_relationship_input_schema,
    _annotation_input_schema,
    _concept_fetch_input_schema,
    _concept_search_input_schema,
    _delete_concept_input_schema,
    _merge_concepts_input_schema,
    _search_knowledge_base_input_schema,
    build_default_catalogue,
)


def main() -> int:
    schemas_to_check = [
        ("fetch_concept", _concept_fetch_input_schema()),
        ("extract_annotations", _annotation_input_schema()),
        ("search_concepts", _concept_search_input_schema()),
        ("add_names", _add_names_input_schema()),
        ("add_relationship", _add_relationship_input_schema()),
        ("delete_concept", _delete_concept_input_schema()),
        ("merge_concepts", _merge_concepts_input_schema()),
        ("search_knowledge_base", _search_knowledge_base_input_schema()),
    ]

    all_passed = True
    for name, schema in schemas_to_check:
        if not schema.allow_unknown:
            print(f"FAIL: {name} schema has allow_unknown=False")
            all_passed = False
        else:
            print(f"PASS: {name} schema has allow_unknown=True")

    catalogue = build_default_catalogue()
    inline_tools = ["rag_get_status", "rag_list_indexed", "rag_get_item"]
    for tool_name in inline_tools:
        try:
            tool = catalogue.get(tool_name)
            if not tool.input_schema.allow_unknown:
                print(f"FAIL: {tool_name} schema has allow_unknown=False")
                all_passed = False
            else:
                print(f"PASS: {tool_name} schema has allow_unknown=True")
        except KeyError:
            print(f"FAIL: {tool_name} not found in catalogue")
            all_passed = False

    if all_passed:
        print("All checked schemas have allow_unknown=True")
        return 0

    print("Some schemas failed check")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
