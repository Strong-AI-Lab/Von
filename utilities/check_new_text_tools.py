#!/usr/bin/env python3
"""Check the new text relation internal MCP functions.

This is a developer utility script (not a pytest test).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from the repo root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.backend.integrations.internal_mcp.catalogue import (  # noqa: E402
    _get_text_relations,
    _upsert_text_relation,
)


def main() -> int:
    print("=" * 80)
    print("Checking new text relation MCP functions")
    print("=" * 80)

    # 1) Get existing hasContent relation
    print("\n1. Checking get_text_relations (should find existing hasContent)...")
    result = _get_text_relations(
        concept_id="#V#missing_tool_call_detection_prompt", predicate="hasContent"
    )
    print(f"   Relations found: {result.get('relations_found')}")
    relations = result.get("relations", [])
    if relations and isinstance(relations, list) and len(relations) > 0:
        rel = relations[0]
        assert isinstance(rel, dict)
        print(f"   Predicate: {rel.get('predicate')}")
        print(f"   Text preview: {rel.get('text', '')[:100]}...")
        print(f"   Language: {rel.get('lang')}")
        print("   ✓ OK")
    else:
        print(f"   ✗ FAILED: {result}")

    # 2) Add/update a hasDescription relation
    print("\n2. Checking upsert_text_relation (add hasDescription)...")
    result = _upsert_text_relation(
        concept_id="#V#missing_tool_call_detection_prompt",
        predicate="hasDescription",
        text=(
            "A prompt used by the auxiliary LLM classifier to detect when the model "
            "promises actions but fails to emit tool calls."
        ),
        language="en-NZ",
    )
    if result.get("success"):
        print(f"   Text value ID: {result.get('text_value_id')}")
        print(f"   Relation ID: {result.get('relation_id')}")
        print(f"   Relation created: {result.get('relation_created')}")
        print("   ✓ OK")
    else:
        print(f"   ✗ FAILED: {result}")

    # 3) Get all text relations
    print("\n3. Checking get_text_relations (all predicates)...")
    result = _get_text_relations(concept_id="#V#missing_tool_call_detection_prompt")
    print(f"   Total relations found: {result.get('relations_found')}")
    predicates: dict[str | None, int] = {}
    relations = result.get("relations", [])
    for rel in relations:
        if isinstance(rel, dict):
            pred = rel.get("predicate")
            predicates[pred] = predicates.get(pred, 0) + 1
    print("   Breakdown by predicate:")
    for pred, count in sorted(predicates.items(), key=lambda kv: (str(kv[0]), kv[1])):
        print(f"     - {pred}: {count}")
    print("   ✓ OK")

    # 4) Verify hasDescription retrieval
    print("\n4. Checking hasDescription retrieval...")
    result = _get_text_relations(
        concept_id="#V#missing_tool_call_detection_prompt", predicate="hasDescription"
    )
    relations_found = result.get("relations_found", 0)
    if isinstance(relations_found, int) and relations_found > 0:
        relations = result.get("relations", [])
        if relations and isinstance(relations, list) and len(relations) > 0:
            rel = relations[0]
            assert isinstance(rel, dict)
            print(f"   Text: {rel.get('text')}")
        print("   ✓ OK")
    else:
        print("   ✗ FAILED: No hasDescription found")

    print("\n" + "=" * 80)
    print("Done")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
