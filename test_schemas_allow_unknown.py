
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from backend.integrations.internal_mcp.catalogue import (
    _concept_fetch_input_schema,
    _annotation_input_schema,
    _concept_search_input_schema,
    _add_names_input_schema,
    _add_relationship_input_schema,
    _delete_concept_input_schema,
    _merge_concepts_input_schema,
    _search_knowledge_base_input_schema,
    build_default_catalogue
)

def test_schemas():
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

    # Check inline schemas in catalogue
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
    else:
        print("Some schemas failed check")

if __name__ == "__main__":
    test_schemas()
