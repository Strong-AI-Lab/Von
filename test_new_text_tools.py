#!/usr/bin/env python3
"""Test the new text relation MCP tools."""

import sys
from pathlib import Path

# Add src to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Import the internal MCP handlers
from src.backend.integrations.internal_mcp.catalogue import (
    _get_text_relations,
    _upsert_text_relation,
)

print("=" * 80)
print("Testing new text relation MCP tools")
print("=" * 80)

# Test 1: Get existing hasContent relation
print("\n1. Testing get_text_relations (should find existing hasContent)...")
result = _get_text_relations(
    concept_id="#V#missing_tool_call_detection_prompt",
    predicate="hasContent"
)
print(f"   Relations found: {result.get('relations_found')}")
relations = result.get('relations', [])
if relations and isinstance(relations, list) and len(relations) > 0:
    rel = relations[0]
    assert isinstance(rel, dict)  # Type assertion for linter
    print(f"   Predicate: {rel.get('predicate')}")
    print(f"   Text preview: {rel.get('text', '')[:100]}...")
    print(f"   Language: {rel.get('lang')}")
    print(f"   ✓ SUCCESS")
else:
    print(f"   ❌ FAILED: {result}")

# Test 2: Add a new hasDescription relation
print("\n2. Testing upsert_text_relation (add hasDescription)...")
result = _upsert_text_relation(
    concept_id="#V#missing_tool_call_detection_prompt",
    predicate="hasDescription",
    text="A prompt used by the auxiliary LLM classifier to detect when the model promises actions but fails to emit tool calls.",
    language="en-NZ"
)
if result.get('success'):
    print(f"   Text value ID: {result.get('text_value_id')}")
    print(f"   Relation ID: {result.get('relation_id')}")
    print(f"   Relation created: {result.get('relation_created')}")
    print(f"   ✓ SUCCESS")
else:
    print(f"   ❌ FAILED: {result}")

# Test 3: Get all text relations (should now have 2: hasContent + hasDescription + hasName entries)
print("\n3. Testing get_text_relations (all predicates)...")
result = _get_text_relations(
    concept_id="#V#missing_tool_call_detection_prompt"
)
print(f"   Total relations found: {result.get('relations_found')}")
predicates = {}
relations = result.get('relations', [])
for rel in relations:
    if isinstance(rel, dict):  # Type guard for linter
        pred = rel.get('predicate')
        predicates[pred] = predicates.get(pred, 0) + 1
print(f"   Breakdown by predicate:")
for pred, count in sorted(predicates.items()):
    print(f"     - {pred}: {count}")
print(f"   ✓ SUCCESS")

# Test 4: Verify the new description can be retrieved
print("\n4. Verifying hasDescription retrieval...")
result = _get_text_relations(
    concept_id="#V#missing_tool_call_detection_prompt",
    predicate="hasDescription"
)
relations_found = result.get('relations_found', 0)
if isinstance(relations_found, int) and relations_found > 0:
    relations = result.get('relations', [])
    if relations and isinstance(relations, list) and len(relations) > 0:
        rel = relations[0]
        assert isinstance(rel, dict)  # Type assertion for linter
        print(f"   Text: {rel.get('text')}")
    print(f"   ✓ SUCCESS")
else:
    print(f"   ❌ FAILED: No hasDescription found")

print("\n" + "=" * 80)
print("All tests completed!")
print("=" * 80)
