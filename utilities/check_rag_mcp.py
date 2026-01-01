#!/usr/bin/env python3
"""Check RAG internal MCP functions directly.

This is a developer utility script (not a pytest test).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.backend.integrations.internal_mcp.catalogue import (
    _rag_list_indexed,
)  # noqa: E402


def main() -> int:
    print("=== Checking RAG MCP Tools Directly ===\n")

    # 1) Call rag_list_indexed WITHOUT namespace
    print("Test 1: rag_list_indexed WITHOUT namespace parameter")
    print("-" * 60)
    try:
        result = _rag_list_indexed()
        if isinstance(result, dict) and result.get("error") == "namespace_required":
            print("PASS: Tool correctly requires namespace")
            print(f"   Error message: {result.get('message')}")
        else:
            print("FAIL: Tool should require namespace but didn't")
            print(f"   Got: {result}")
    except Exception as ex:
        print(f"FAIL: Exception: {ex}")

    print("\n" + "=" * 60 + "\n")

    # 2) Call rag_list_indexed WITH michael_witbrock namespace
    print("Test 2: rag_list_indexed WITH namespace=#V#michael_witbrock")
    print("-" * 60)
    try:
        result = _rag_list_indexed(namespace="#V#michael_witbrock")
        if isinstance(result, dict) and result.get("success"):
            total = result.get("total", 0)
            items_count = len(result.get("items", []))
            print(f"PASS: Got {total} total sessions, {items_count} items returned")
            print(f"   Namespace in result: {result.get('namespace')}")
        else:
            print(f"FAIL: Tool returned error: {result}")
    except Exception as ex:
        print(f"FAIL: Exception: {ex}")

    print("\n" + "=" * 60 + "\n")

    # 3) Call rag_list_indexed WITH von_archivist namespace
    print("Test 3: rag_list_indexed WITH namespace=#V#von_archivist")
    print("-" * 60)
    try:
        result = _rag_list_indexed(namespace="#V#von_archivist")
        if isinstance(result, dict) and result.get("success"):
            total = result.get("total", 0)
            items_count = len(result.get("items", []))
            print(f"PASS: Got {total} total sessions, {items_count} items returned")
            print(f"   Namespace in result: {result.get('namespace')}")
        else:
            print(f"FAIL: Tool returned error: {result}")
    except Exception as ex:
        print(f"FAIL: Exception: {ex}")

    print("\n" + "=" * 60)
    print("\n=== Summary ===")
    print("Expected behaviour:")
    print("  - Tool WITHOUT namespace should return 'namespace_required' error")
    print("  - Tool WITH namespace should filter to that user's sessions only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
