#!/usr/bin/env python3
"""Test RAG MCP tools directly to verify namespace filtering."""

from src.backend.integrations.internal_mcp.catalogue import _rag_list_indexed

print("=== Testing RAG MCP Tools Directly ===\n")

# Test 1: Call rag_list_indexed WITHOUT namespace
print("Test 1: rag_list_indexed WITHOUT namespace parameter")
print("-" * 60)
try:
    result = _rag_list_indexed()
    if isinstance(result, dict) and result.get("error") == "namespace_required":
        print("✅ PASS: Tool correctly requires namespace")
        print(f"   Error message: {result.get('message')}")
    else:
        print("❌ FAIL: Tool should require namespace but didn't")
        print(f"   Got: {result}")
except Exception as e:
    print(f"❌ FAIL: Exception: {e}")

print("\n" + "=" * 60 + "\n")

# Test 2: Call rag_list_indexed WITH michael_witbrock namespace
print("Test 2: rag_list_indexed WITH namespace=#V#michael_witbrock")
print("-" * 60)
try:
    result = _rag_list_indexed(namespace="#V#michael_witbrock")
    if isinstance(result, dict):
        if result.get("success"):
            total = result.get("total", 0)
            items_count = len(result.get("items", []))
            print(f"✅ PASS: Got {total} total sessions, {items_count} items returned")
            print(f"   Namespace in result: {result.get('namespace')}")
            if total == 10:
                print("   ✅ Correct count (expected 10 for michael_witbrock)")
            else:
                print(f"   ❌ WRONG count (expected 10, got {total})")
        else:
            print(f"❌ FAIL: Tool returned error: {result}")
    else:
        print(f"❌ FAIL: Unexpected result type: {type(result)}")
except Exception as e:
    print(f"❌ FAIL: Exception: {e}")

print("\n" + "=" * 60 + "\n")

# Test 3: Call rag_list_indexed WITH von_archivist namespace
print("Test 3: rag_list_indexed WITH namespace=#V#von_archivist")
print("-" * 60)
try:
    result = _rag_list_indexed(namespace="#V#von_archivist")
    if isinstance(result, dict):
        if result.get("success"):
            total = result.get("total", 0)
            items_count = len(result.get("items", []))
            print(f"✅ PASS: Got {total} total sessions, {items_count} items returned")
            print(f"   Namespace in result: {result.get('namespace')}")
            if total == 16:
                print("   ✅ Correct count (expected 16 for von_archivist)")
            else:
                print(f"   ❌ WRONG count (expected 16, got {total})")
        else:
            print(f"❌ FAIL: Tool returned error: {result}")
    else:
        print(f"❌ FAIL: Unexpected result type: {type(result)}")
except Exception as e:
    print(f"❌ FAIL: Exception: {e}")

print("\n" + "=" * 60)
print("\n=== Summary ===")
print("Expected behaviour:")
print("  - Tool WITHOUT namespace should return 'namespace_required' error")
print("  - Tool WITH namespace should filter to that user's sessions only")
print("  - michael_witbrock should have 10 indexed sessions")
print("  - von_archivist should have 16 indexed sessions")

