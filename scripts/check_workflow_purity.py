#!/usr/bin/env python3
"""
JVNAUTOSCI-1769: Workflow Purity Checker

This script enforces the architectural mandate that Python is the Engine,
and Vontology Workflow Language (VWL) is the Application. 

It scans the core engine, orchestrator, and routing layers to detect 
"Python code pretending to be a VWL workflow".

Usage:
    python scripts/check_workflow_purity.py [path_to_scan]

If run without arguments, it scans standard backend paths.
Exits with code 1 if violations are found.
"""

import sys
import os
import ast
import re
from pathlib import Path

# Directories that should contain pure engine code, free of business logic
ENGINE_PATHS = [
    "src/backend/server/routes",
    "src/backend/integrations/internal_mcp/orchestrator.py",
    "src/backend/workflows/engine.py",
    "src/backend/workflows/durable",
]

# Exceptions where specific strings are allowed (e.g. bridging UI)
WHITELISTED_FILES = {
    # It's acceptable for the tool registry or explicit test files to know about workflows
    "registry_factory.py",
    "definitions.py",
    # The actual workflow definition builder for paper representation is allowed to contain paper terms,
    # but we are scanning it here to ensure it doesn't contain prompt bodies or LLM calls.
}

# Heuristic 2: Hardcoded prompts (String literals only)
PROMPT_REGEXES = [
    re.compile(r'(?i)"you are an (ai|assistant)'),
    re.compile(r'(?i)"summarize the following'),
    re.compile(r'(?i)system_prompt\s*=\s*["\']'),
]

# Heuristic 3 & 4: Domain specific logic in the engine
DOMAIN_TERMS = [
    "arxiv_workflow",
    "scholarly_paper_workflow",
    "#V#scholarly_paper",
]

def scan_file(filepath: Path) -> list[str]:
    violations = []
    
    if filepath.name in WHITELISTED_FILES or "test" in filepath.name:
        return violations

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error reading {filepath}: {e}", file=sys.stderr)
        return violations

    # Regex checks for prompts and domain concepts
    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        for pattern in PROMPT_REGEXES:
            if pattern.search(line):
                violations.append(f"Line {i}: Hardcoded prompt detected (Rule 2). Extract to #V#prompt concept. -> {line.strip()[:80]}")
        
        for term in DOMAIN_TERMS:
            # We allow importing definitions, but we shouldn't be branching on them
            if term in line and "import" not in line and "definitions" not in line:
                violations.append(f"Line {i}: Domain logic '{term}' detected in engine code (Rules 3/4). Engine should be generic. -> {line.strip()[:80]}")

        if 'if "arxiv" in' in line.lower():
             violations.append(f"Line {i}: Fragile state scraping detected (Rule 5). Use Vontology queries. -> {line.strip()[:80]}")

    # AST checks for structural issues
    try:
        tree = ast.parse(content, filename=str(filepath))
    except SyntaxError:
        return violations

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            # Check for domain verbs in function names (ignore in paper_representation_workflow itself)
            if filepath.name != "paper_representation_workflow.py":
                name_lower = node.name.lower()
                if "arxiv" in name_lower or "paper" in name_lower:
                    violations.append(f"Line {node.lineno}: Function '{node.name}' uses domain verbs (Rule 3). Engine functions must be generic.")
            
            # Check for multiple LLM calls in a single function (Rule 1)
            llm_calls = 0
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Attribute):
                        if child.func.attr in ("generate", "chat") and isinstance(child.func.value, ast.Name) and "llm" in child.func.value.id.lower():
                            llm_calls += 1
            if llm_calls > 1:
                violations.append(f"Line {node.lineno}: Function '{node.name}' makes sequential LLM calls (Rule 1). Multi-step LLM reasoning belongs in VWL.")

    return violations

def main():
    print("Running Workflow Purity Checks...")
    root_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    
    paths_to_scan = []
    for p in ENGINE_PATHS:
        target = root_dir / p
        if target.is_file():
            paths_to_scan.append(target)
        elif target.is_dir():
            paths_to_scan.extend(target.rglob("*.py"))
            
    total_violations = 0
    files_with_violations = 0
    
    for filepath in paths_to_scan:
        violations = scan_file(filepath)
        if violations:
            files_with_violations += 1
            total_violations += len(violations)
            print(f"\n❌ {filepath}")
            for v in violations:
                print(f"  - {v}")
                
    if total_violations > 0:
        print(f"\n💥 Purity Check Failed: Found {total_violations} violations across {files_with_violations} files.")
        print("Python is the Engine, VWL is the Application. Move business logic to Vontology.")
        sys.exit(1)
        
    print("\n✅ Purity Check Passed. The Engine is pure.")
    sys.exit(0)

if __name__ == "__main__":
    main()
