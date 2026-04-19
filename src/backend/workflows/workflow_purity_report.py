"""Workflow-first purity reporting and regression helpers.

This module extends the existing workflow authority/parity diagnostics with
code-shape impurity counters that track the remaining hybrid authority surface.
The report is intentionally deterministic and suitable for both startup
diagnostics and CI regression gates.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..services.workflow_capability_service import BUILTIN_WORKFLOW_CAPABILITIES

WORKFLOW_PURITY_REPORT_SCHEMA_VERSION = "workflow_purity_report.v1"
WORKFLOW_PURITY_BASELINE_SCHEMA_VERSION = "workflow_purity_baseline.v1"

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PURITY_BASELINE_PATH = (
    PROJECT_ROOT / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
)
WORKFLOW_PURITY_SUPPORT_FILE = "src/backend/workflows/workflow_purity_report.py"
WORKFLOW_PURITY_BASELINE_REFRESH_COMMAND = (
    "pdm run python scripts/workflow_purity_report.py --refresh-baseline"
)

DIRECT_INSTANCE_CREATE_PATTERN = re.compile(r"\.create_instance(?:_for_event)?\(")
ALLOWED_DIRECT_INSTANCE_CREATE_CALLSITES = frozenset(
    {
        "src/backend/workflows/durable/durable_executor.py",
        "src/backend/workflows/durable/instance_manager.py",
        "src/backend/workflows/durable/workflow_instance_submission_service.py",
    }
)

ENV_EVENT_BINDING_AUTHORITY_PATTERNS = {
    "EVENT_WORKFLOW_ID_ENV_MAP": re.compile(r"\bEVENT_WORKFLOW_ID_ENV_MAP\b"),
    "include_env_fallback": re.compile(r"\binclude_env_fallback\b"),
}
ENV_EVENT_BINDING_SCAN_GLOBS = (
    "src/backend/**/*.py",
    "src/backend/mcp_server/vontology_mcp.json",
)

LEGACY_SELECTOR_CONSTRUCT_PATTERNS = {
    "legacy_classifier_prompt": re.compile(r"\b_LEGACY_CLASSIFIER_PROMPT\b"),
    "legacy_discovery_suffix": re.compile(r"\b_LEGACY_DISCOVERY_SUFFIX\b"),
    "verdict_mapping": re.compile(r"\bverdict_mapping\b"),
    "prepare_legacy_prompt": re.compile(r"\bdef _prepare_legacy_prompt\b"),
    "parse_legacy_selection": re.compile(r"\bdef _parse_legacy_selection\b"),
}
LEGACY_SELECTOR_FILE = "src/backend/workflows/workflow_selector.py"

PYTHON_WORKFLOW_FAMILY_FILE_GLOBS = (
    "src/backend/workflows/definitions.py",
    "src/backend/workflows/durable/*_workflow.py",
    "src/backend/services/*workflow*_service.py",
)
PYTHON_WORKFLOW_FAMILY_FUNCTION_PATTERNS = (
    re.compile(r"^build_.*workflow$"),
    re.compile(r"^get_.*workflow_registration$"),
    re.compile(r"^register_default_workflows$"),
)
WORKFLOW_REGISTRATION_CALL_PATTERN = re.compile(r"\bWorkflowRegistration\(")

CANONICAL_WORKFLOW_SOURCE_SCAN_GLOBS = (
    "src/backend/workflows/workflow_concept_authority_service.py",
    "src/backend/services/*workflow*_service.py",
)
CANONICAL_WORKFLOW_SOURCE_FUNCTION_PATTERNS = (
    re.compile(r"^_?build_.*workflow_spec$"),
)
CANONICAL_WORKFLOW_SOURCE_LITERAL_PATTERNS = {
    "canonical_publication_spec_literal": re.compile(
        r"^\s*_CANONICAL_WORKFLOW_PUBLICATION_SPECS\s*=\s*\{",
        re.MULTILINE,
    ),
}

WORKFLOW_PROMPT_SOURCE_SCAN_GLOBS = (
    "src/backend/services/*workflow*_service.py",
    "src/backend/services/parent_specificity_vontology_service.py",
    "src/backend/workflows/durable/*_workflow.py",
)
WORKFLOW_PROMPT_SOURCE_IGNORED_TARGET_SUFFIXES = (
    "_CONCEPT_ID",
    "_TYPE_ID",
    "_LINK_PREDICATE",
    "_PREDICATE",
    "_PREDICATES",
    "_PATTERN",
    "_PATTERNS",
    "_SPECS",
    "_WORKFLOW_IDS",
)
WORKFLOW_PROMPT_SOURCE_FUNCTION_PATTERNS = (
    re.compile(r"^_?build_.*prompt.*template$"),
    re.compile(r"^_?ensure_.*prompt.*concept$"),
)

CORE_SUPPORT_PROMPT_SOURCE_FILES = (
    "src/backend/integrations/internal_mcp/orchestrator.py",
)

ORCHESTRATOR_RETIRED_SUPPORT_SYMBOLS = {
    "_PROMPT_MCP_TOOL_FAMILY_PATTERN": "retired_semantic_regex_symbol",
    "_URL_READING_INTENT_PATTERN": "retired_semantic_regex_symbol",
    "_guided_retrieval_focus_terms": "retired_guided_retrieval_helper",
    "_build_guided_retrieval_query": "retired_guided_retrieval_helper",
    "_infer_guided_retrieval_retry_tool_calls": "retired_guided_retrieval_helper",
    "_build_guided_jira_search_jql": "retired_guided_retrieval_helper",
    "_extract_topic_keywords_from_context": "retired_topic_keyword_helper",
}
ORCHESTRATOR_RETIRED_SUPPORT_SYMBOL_PATTERNS = {
    "retired_prompt_semantic_regex_symbol": re.compile(
        r"^_PROMPT_.*(?:INTENT|HINT|SEMANTIC|INFERENCE|ANALYTICAL).*_PATTERN$"
    ),
}
WORKFLOW_CAPABILITY_RETIRED_SYMBOL_PATTERNS = {
    "retired_workflow_capability_bm25_symbol": re.compile(
        r"(?i)(?:^|_)bm25(?:$|_)"
    ),
    "retired_workflow_capability_stopword_symbol": re.compile(
        r"(?i)(?:^|_)stop_?words?(?:$|_)"
    ),
    "retired_workflow_capability_tokeniser_symbol": re.compile(
        r"(?i)^_?tokeni[sz]e(?:_re|_text|_query|r)?$"
    ),
    "retired_workflow_capability_token_normaliser_symbol": re.compile(
        r"(?i)^_?(?:normalise|normalize)_tokens?$"
    ),
}
WORKFLOW_CAPABILITY_RETIRED_IMPORT_MODULES = {
    "rank_bm25": "retired_workflow_capability_bm25_import",
}
WORKFLOW_CAPABILITY_RETIRED_IMPORT_NAMES = {
    "BM25Okapi": "retired_workflow_capability_bm25_import_name",
    "InMemoryBM25Retriever": "retired_workflow_capability_bm25_import_name",
}
WRITE_TOOL_POLICY_ALLOWED_REGEX_PATTERN_NAMES = frozenset(
    {"_CONFIRMATION_PATTERN", "_DESTRUCTIVE_MUTATION_PATTERN"}
)
FILE_COPY_INTERPRETATION_RETIRED_SYMBOLS = {
    "_ORGANISATION_SUFFIX_PATTERN": "retired_file_copy_semantic_regex_symbol",
    "_ORGANISATION_PREFIX_PATTERN": "retired_file_copy_semantic_regex_symbol",
    "_ALL_CAPS_ORG_PATTERN": "retired_file_copy_semantic_regex_symbol",
    "_ORGANISATION_STOPWORDS": "retired_file_copy_stopword_symbol",
}

CORE_SUPPORT_POLICY_CONTRACTS = (
    {
        "name": "orchestrator_support_surface_authority",
        "path": "src/backend/integrations/internal_mcp/orchestrator.py",
        "forbidden_patterns": {
            "code_fallback_source_marker": re.compile(
                r"['\"]source['\"]\s*:\s*['\"]code_fallback['\"]",
                re.IGNORECASE,
            ),
        },
        "banned_symbol_names": ORCHESTRATOR_RETIRED_SUPPORT_SYMBOLS,
        "banned_symbol_patterns": ORCHESTRATOR_RETIRED_SUPPORT_SYMBOL_PATTERNS,
    },
    {
        "name": "workflow_capability_retrieval_authority",
        "path": "src/backend/services/workflow_capability_service.py",
        "banned_symbol_patterns": WORKFLOW_CAPABILITY_RETIRED_SYMBOL_PATTERNS,
        "banned_import_modules": WORKFLOW_CAPABILITY_RETIRED_IMPORT_MODULES,
        "banned_import_names": WORKFLOW_CAPABILITY_RETIRED_IMPORT_NAMES,
    },
    {
        "name": "write_tool_policy_regex_backstop_scope",
        "path": "src/backend/workflows/write_tool_policy.py",
        "allowed_regex_pattern_names": sorted(
            WRITE_TOOL_POLICY_ALLOWED_REGEX_PATTERN_NAMES
        ),
        "allowed_regex_helper_functions": ("prompt_explicitly_denies_write",),
    },
    {
        "name": "file_copy_interpretation_authority_surface",
        "path": "src/backend/services/file_copy_interpretation_service.py",
        "banned_symbol_names": FILE_COPY_INTERPRETATION_RETIRED_SYMBOLS,
    },
)

REPO_SEED_AUTHORITY_SCAN_GLOBS = ("src/backend/**/*.py",)
REPO_SEED_AUTHORITY_ALLOWED_PATHS = frozenset(
    {
        "src/backend/services/conversation_turn_workflow_vontology_service.py",
        "src/backend/services/entity_representation_workflow_vontology_service.py",
        "src/backend/services/episode_evaluation_workflow_vontology_service.py",
        "src/backend/services/paper_recommendation_workflow_vontology_service.py",
        "src/backend/services/paper_representation_workflow_vontology_service.py",
        "src/backend/services/talk_representation_workflow_vontology_service.py",
        "src/backend/services/testing_workflow_vontology_service.py",
        "src/backend/services/workflow_repo_seed_bootstrap.py",
        "src/backend/workflows/workflow_concept_authority_service.py",
        "src/backend/workflows/workflow_template_profile_service.py",
    }
)
REPO_SEED_AUTHORITY_PATTERNS = {
    "repo_seed_bootstrap_module": re.compile(r"\bworkflow_repo_seed_bootstrap\b"),
    "repo_seed_bundle_loader": re.compile(r"\bload_repo_seed_workflow_bundle\s*\("),
    "repo_seed_publication_specs": re.compile(
        r"\bseed_canonical_workflow_publication_specs\s*\("
    ),
    "repo_seed_text_relations": re.compile(
        r"\bseed_canonical_workflow_text_relations\s*\("
    ),
    "repo_seed_template_asset": re.compile(
        r"\bDEFAULT_REPO_SEED_TEMPLATE_ASSET_PATH\b"
    ),
    "repo_seed_template_hydration": re.compile(
        r"\bensure_repo_seeded_workflow_template_bundle\s*\("
    ),
}

WORKFLOW_ID_SPECIAL_CASE_SCAN_GLOBS = (
    "src/backend/integrations/internal_mcp/orchestrator.py",
    "src/backend/server/routes/von_routes.py",
    "src/backend/workflows/durable/turn_execution_actions.py",
)
WORKFLOW_ID_SPECIAL_CASE_OPERATORS = (
    ast.Eq,
    ast.NotEq,
    ast.In,
    ast.NotIn,
)

SUPERVISED_FAIL_OPEN_CONTRACT = {
    "path": "src/backend/integrations/internal_mcp/orchestrator.py",
    "function": "execute_conversation_turn_supervised",
    "forbidden_patterns": {
        "fallback_to_legacy_run": re.compile(r"\bself\.run\s*\("),
        "synthetic_missing_response_success": re.compile(
            r"Workflow execution completed without response text",
            re.IGNORECASE,
        ),
    },
}

SEED_FALLBACK_ORDER_CONTRACTS = (
    {
        "name": "workflow_template_bundle_vontology_first",
        "path": "src/backend/workflows/workflow_template_profile_service.py",
        "function": "load_workflow_template_bundle",
        "authoritative_calls": ("_load_vontology_workflow_template_bundle_cached",),
        "fallback_calls": ("ensure_repo_seeded_workflow_template_bundle",),
    },
    {
        "name": "canonical_workflow_publication_vontology_first",
        "path": "src/backend/workflows/workflow_concept_authority_service.py",
        "function": "publish_canonical_chat_workflow_graphs",
        "authoritative_calls": (
            "_resolve_authoritative_publication_specs",
            "_resolve_authoritative_workflow_text_relations",
        ),
        "fallback_calls": (
            "seed_canonical_workflow_publication_specs",
            "seed_canonical_workflow_text_relations",
        ),
    },
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_path(path: Path, project_root: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _iter_files(project_root: Path, globs: Sequence[str]) -> list[Path]:
    matched: dict[str, Path] = {}
    for glob in globs:
        for path in project_root.glob(glob):
            if path.is_file():
                matched[str(path.resolve())] = path
    return sorted(matched.values(), key=lambda item: str(item.resolve()))


def _extract_target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in node.elts:
            names.extend(_extract_target_names(element))
        return names
    return []


def _collect_string_literals(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    strings: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            strings.append(child.value)
    return strings


def _annotate_ast_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            setattr(child, "parent", parent)


def _is_docstring_constant(node: ast.Constant) -> bool:
    parent = getattr(node, "parent", None)
    if not isinstance(parent, ast.Expr) or parent.value is not node:
        return False
    grandparent = getattr(parent, "parent", None)
    return isinstance(
        grandparent,
        (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
    )


def _looks_like_embedded_support_prompt_body(text: str) -> bool:
    candidate = str(text or "").strip()
    if not candidate:
        return False
    if candidate.count("\n") < 2:
        return False
    return (
        len(candidate) >= 120
        and any(character.isspace() for character in candidate)
        and _looks_like_python_authored_prompt_body((candidate,))
    )


def _looks_like_python_authored_prompt_body(strings: Sequence[str]) -> bool:
    meaningful_fragments = []
    for raw in strings:
        text = str(raw or "").strip()
        if not text:
            continue
        if text.startswith("#V#"):
            continue
        if text in {
            "hasContent",
            "#V#hasContent",
            "en-NZ",
            "replace_others",
        }:
            continue
        meaningful_fragments.append(text)
    if not meaningful_fragments:
        return False
    return any(
        len(fragment) >= 40 and any(character.isspace() for character in fragment)
        for fragment in meaningful_fragments
    )


def _is_prompt_source_target_name(name: str) -> bool:
    upper_name = name.upper()
    if "PROMPT" not in upper_name:
        return False
    return not any(
        upper_name.endswith(suffix)
        for suffix in WORKFLOW_PROMPT_SOURCE_IGNORED_TARGET_SUFFIXES
    )


def _extract_top_level_function_source(
    *,
    path: Path,
    project_root: Path,
    function_name: str,
) -> dict[str, Any] | None:
    relative_path = _relative_path(path, project_root)
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=relative_path)
    except SyntaxError:
        return None

    lines = text.splitlines()
    matching_nodes = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        key=lambda item: int(getattr(item, "lineno", 0) or 0),
    )
    for node in matching_nodes:
        end_lineno = getattr(node, "end_lineno", None)
        if end_lineno is None:
            continue
        start_line = int(node.lineno)
        end_line = int(end_lineno)
        function_text = "\n".join(lines[start_line - 1 : end_line])
        if function_text:
            function_text += "\n"
        return {
            "path": relative_path,
            "function_name": function_name,
            "start_line": start_line,
            "end_line": end_line,
            "text": function_text,
        }
    return None


def _iter_contract_symbol_records(tree: ast.AST) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            records.append(
                {
                    "name": str(node.name),
                    "line": int(getattr(node, "lineno", 0) or 0),
                    "kind": "definition",
                }
            )
            continue

        if isinstance(node, ast.Assign):
            target_names: list[str] = []
            for target in node.targets:
                target_names.extend(_extract_target_names(target))
            for name in target_names:
                records.append(
                    {
                        "name": str(name),
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "kind": "assignment",
                    }
                )
            continue

        if isinstance(node, ast.AnnAssign):
            for name in _extract_target_names(node.target):
                records.append(
                    {
                        "name": str(name),
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "kind": "assignment",
                    }
                )
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                records.append(
                    {
                        "name": str(alias.asname or alias.name.split(".")[-1]),
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "kind": "import",
                        "import_module": str(alias.name),
                        "import_name": str(alias.name.split(".")[-1]),
                    }
                )
            continue

        if isinstance(node, ast.ImportFrom):
            module_name = str(node.module or "")
            for alias in node.names:
                records.append(
                    {
                        "name": str(alias.asname or alias.name),
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "kind": "import",
                        "import_module": module_name,
                        "import_name": str(alias.name),
                    }
                )
    return records


def _scan_contract_symbol_drift(
    *,
    tree: ast.AST,
    relative_path: str,
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    banned_symbol_names = {
        str(name): str(reason)
        for name, reason in dict(contract.get("banned_symbol_names") or {}).items()
        if str(name).strip() and str(reason).strip()
    }
    banned_symbol_patterns = [
        (str(reason), pattern)
        for reason, pattern in dict(contract.get("banned_symbol_patterns") or {}).items()
        if str(reason).strip() and isinstance(pattern, re.Pattern)
    ]
    banned_import_modules = {
        str(name): str(reason)
        for name, reason in dict(contract.get("banned_import_modules") or {}).items()
        if str(name).strip() and str(reason).strip()
    }
    banned_import_names = {
        str(name): str(reason)
        for name, reason in dict(contract.get("banned_import_names") or {}).items()
        if str(name).strip() and str(reason).strip()
    }

    def _append_violation(*, line: int, pattern: str, symbol: str | None = None) -> None:
        key = (pattern, str(symbol or ""), int(line))
        if key in seen:
            return
        seen.add(key)
        payload = {
            "path": relative_path,
            "line": int(line),
            "pattern": str(pattern),
        }
        if symbol:
            payload["symbol"] = str(symbol)
        violations.append(payload)

    for record in _iter_contract_symbol_records(tree):
        name = str(record.get("name") or "")
        line = int(record.get("line") or 0)
        import_module = str(record.get("import_module") or "")
        import_name = str(record.get("import_name") or "")

        exact_reason = banned_symbol_names.get(name)
        if exact_reason:
            _append_violation(line=line, pattern=exact_reason, symbol=name)

        for reason, pattern in banned_symbol_patterns:
            if pattern.search(name):
                _append_violation(line=line, pattern=reason, symbol=name)

        if import_module in banned_import_modules:
            _append_violation(
                line=line,
                pattern=banned_import_modules[import_module],
                symbol=import_module,
            )

        if import_name in banned_import_names:
            _append_violation(
                line=line,
                pattern=banned_import_names[import_name],
                symbol=import_name,
            )

    return violations


def _scan_allowed_regex_backstop_scope(
    *,
    tree: ast.AST,
    relative_path: str,
    allowed_pattern_names: Sequence[str],
    allowed_helper_functions: Sequence[str] = (),
) -> list[dict[str, Any]]:
    allowed_names = {str(name) for name in allowed_pattern_names if str(name).strip()}
    allowed_helpers = {
        str(name) for name in allowed_helper_functions if str(name).strip()
    }
    violations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()

    def _append_violation(
        *,
        line: int,
        pattern: str,
        symbol: str | None = None,
        call_name: str | None = None,
    ) -> None:
        key = (pattern, str(symbol or call_name or ""), int(line))
        if key in seen:
            return
        seen.add(key)
        payload = {
            "path": relative_path,
            "line": int(line),
            "pattern": str(pattern),
        }
        if symbol:
            payload["symbol"] = str(symbol)
        if call_name:
            payload["call"] = str(call_name)
        violations.append(payload)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "re":
            continue
        call_name = str(node.func.attr)
        line = int(getattr(node, "lineno", 0) or 0)
        current_parent = getattr(node, "parent", None)
        while current_parent is not None:
            if isinstance(current_parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if str(current_parent.name) in allowed_helpers:
                    break
            current_parent = getattr(current_parent, "parent", None)
        else:
            current_parent = None
        if current_parent is not None and isinstance(
            current_parent, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        is_module_level_compile = False
        module_level_symbols: list[str] = []

        if call_name == "compile":
            parent = getattr(node, "parent", None)
            grandparent = getattr(parent, "parent", None)
            if isinstance(parent, ast.Assign) and isinstance(grandparent, ast.Module):
                is_module_level_compile = True
                for target in parent.targets:
                    module_level_symbols.extend(_extract_target_names(target))
            elif isinstance(parent, ast.AnnAssign) and isinstance(grandparent, ast.Module):
                is_module_level_compile = True
                module_level_symbols.extend(_extract_target_names(parent.target))

        if is_module_level_compile and module_level_symbols:
            disallowed_symbols = sorted(
                symbol for symbol in module_level_symbols if symbol not in allowed_names
            )
            for symbol in disallowed_symbols:
                _append_violation(
                    line=line,
                    pattern="unexpected_write_tool_regex_backstop",
                    symbol=symbol,
                    call_name=call_name,
                )
            if not disallowed_symbols:
                continue
            continue

        _append_violation(
            line=line,
            pattern="unexpected_write_tool_regex_backstop",
            call_name=call_name,
        )

    return violations


def _scan_direct_instance_create_callsites(project_root: Path) -> dict[str, Any]:
    backend_root = project_root / "src" / "backend"
    callsites: list[dict[str, Any]] = []
    for path in sorted(backend_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        relative_path = _relative_path(path, project_root)
        if relative_path == WORKFLOW_PURITY_SUPPORT_FILE:
            continue
        is_allowed = relative_path in ALLOWED_DIRECT_INSTANCE_CREATE_CALLSITES
        for match in DIRECT_INSTANCE_CREATE_PATTERN.finditer(text):
            callsites.append(
                {
                    "path": relative_path,
                    "line": _line_number(text, match.start()),
                    "allowed": is_allowed,
                }
            )

    offenders = [item for item in callsites if not item["allowed"]]
    return {
        "total_callsites": len(callsites),
        "allowed_callsite_count": len(callsites) - len(offenders),
        "offending_callsite_count": len(offenders),
        "callsites": callsites,
        "offending_callsites": offenders,
        "allowed_paths": sorted(ALLOWED_DIRECT_INSTANCE_CREATE_CALLSITES),
    }


def _scan_env_event_binding_authority(project_root: Path) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in _iter_files(project_root, ENV_EVENT_BINDING_SCAN_GLOBS):
        text = path.read_text(encoding="utf-8")
        relative_path = _relative_path(path, project_root)
        if relative_path == WORKFLOW_PURITY_SUPPORT_FILE:
            continue
        for pattern_name, pattern in ENV_EVENT_BINDING_AUTHORITY_PATTERNS.items():
            for match in pattern.finditer(text):
                matches.append(
                    {
                        "path": relative_path,
                        "line": _line_number(text, match.start()),
                        "pattern": pattern_name,
                    }
                )
    return {
        "total_matches": len(matches),
        "matches": matches,
        "patterns": sorted(ENV_EVENT_BINDING_AUTHORITY_PATTERNS),
    }


def _scan_legacy_selector_support(project_root: Path) -> dict[str, Any]:
    selector_path = project_root / LEGACY_SELECTOR_FILE
    if not selector_path.exists():
        return {
            "module_count": 0,
            "construct_count": 0,
            "matched_constructs": [],
            "file": LEGACY_SELECTOR_FILE,
        }

    text = selector_path.read_text(encoding="utf-8")
    matched_constructs = [
        name
        for name, pattern in LEGACY_SELECTOR_CONSTRUCT_PATTERNS.items()
        if pattern.search(text)
    ]
    return {
        "module_count": 1 if matched_constructs else 0,
        "construct_count": len(matched_constructs),
        "matched_constructs": matched_constructs,
        "file": LEGACY_SELECTOR_FILE,
    }


def _scan_python_workflow_family_files(project_root: Path) -> dict[str, Any]:
    family_files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for path in _iter_files(project_root, PYTHON_WORKFLOW_FAMILY_FILE_GLOBS):
        relative_path = _relative_path(path, project_root)
        if relative_path in seen_paths:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError:
            continue

        matched_symbols: list[str] = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                pattern.match(node.name)
                for pattern in PYTHON_WORKFLOW_FAMILY_FUNCTION_PATTERNS
            ):
                matched_symbols.append(node.name)

        if (
            not matched_symbols
            and relative_path.startswith("src/backend/services/")
            and WORKFLOW_REGISTRATION_CALL_PATTERN.search(text)
        ):
            matched_symbols.append("WorkflowRegistration")

        if not matched_symbols:
            continue

        family_files.append(
            {
                "path": relative_path,
                "matched_symbols": sorted(set(matched_symbols)),
            }
        )
        seen_paths.add(relative_path)

    return {
        "family_file_count": len(family_files),
        "family_files": family_files,
        "file_globs": list(PYTHON_WORKFLOW_FAMILY_FILE_GLOBS),
    }


def _scan_python_authored_canonical_workflow_sources(
    project_root: Path,
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for path in _iter_files(project_root, CANONICAL_WORKFLOW_SOURCE_SCAN_GLOBS):
        relative_path = _relative_path(path, project_root)
        text = path.read_text(encoding="utf-8")
        matched_symbols: list[str] = []
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError:
            tree = None

        if tree is not None:
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(
                    pattern.match(node.name)
                    for pattern in CANONICAL_WORKFLOW_SOURCE_FUNCTION_PATTERNS
                ):
                    matched_symbols.append(node.name)

        for pattern_name, pattern in CANONICAL_WORKFLOW_SOURCE_LITERAL_PATTERNS.items():
            if pattern.search(text):
                matched_symbols.append(pattern_name)

        if matched_symbols:
            sources.append(
                {
                    "path": relative_path,
                    "matched_symbols": sorted(dict.fromkeys(matched_symbols)),
                }
            )

    return {
        "source_count": sum(len(item["matched_symbols"]) for item in sources),
        "sources": sources,
        "file_globs": list(CANONICAL_WORKFLOW_SOURCE_SCAN_GLOBS),
    }


def _scan_python_authored_workflow_prompt_sources(
    project_root: Path,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in _iter_files(project_root, WORKFLOW_PROMPT_SOURCE_SCAN_GLOBS):
        relative_path = _relative_path(path, project_root)
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError:
            continue

        for node in tree.body:
            if isinstance(node, ast.Assign):
                target_names: list[str] = []
                for target in node.targets:
                    target_names.extend(_extract_target_names(target))
                prompt_targets = [
                    name for name in target_names if _is_prompt_source_target_name(name)
                ]
                if prompt_targets and _looks_like_python_authored_prompt_body(
                    _collect_string_literals(node.value)
                ):
                    matches.append(
                        {
                            "path": relative_path,
                            "kind": "assignment",
                            "symbol": sorted(dict.fromkeys(prompt_targets))[0],
                            "line": int(node.lineno),
                        }
                    )
                continue

            if isinstance(node, ast.AnnAssign):
                target_names = _extract_target_names(node.target)
                prompt_targets = [
                    name for name in target_names if _is_prompt_source_target_name(name)
                ]
                if prompt_targets and _looks_like_python_authored_prompt_body(
                    _collect_string_literals(node.value)
                ):
                    matches.append(
                        {
                            "path": relative_path,
                            "kind": "assignment",
                            "symbol": sorted(dict.fromkeys(prompt_targets))[0],
                            "line": int(node.lineno),
                        }
                    )
                continue

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not any(
                    pattern.match(node.name)
                    for pattern in WORKFLOW_PROMPT_SOURCE_FUNCTION_PATTERNS
                ):
                    continue
                if not _looks_like_python_authored_prompt_body(
                    _collect_string_literals(node)
                ):
                    continue
                matches.append(
                    {
                        "path": relative_path,
                        "kind": "function",
                        "symbol": node.name,
                        "line": int(node.lineno),
                    }
                )

    return {
        "source_count": len(matches),
        "sources": matches,
        "file_globs": list(WORKFLOW_PROMPT_SOURCE_SCAN_GLOBS),
    }


def _scan_python_authored_core_support_prompt_sources(
    project_root: Path,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for relative_path in CORE_SUPPORT_PROMPT_SOURCE_FILES:
        path = project_root / relative_path
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError:
            continue
        _annotate_ast_parents(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if _is_docstring_constant(node):
                continue
            candidate = str(node.value or "")
            if not _looks_like_embedded_support_prompt_body(candidate):
                continue
            matches.append(
                {
                    "path": relative_path,
                    "line": int(getattr(node, "lineno", 0) or 0),
                    "preview": candidate[:120],
                }
            )
    return {
        "source_count": len(matches),
        "sources": matches,
        "files": list(CORE_SUPPORT_PROMPT_SOURCE_FILES),
    }


def _scan_core_support_policy_contracts(project_root: Path) -> dict[str, Any]:
    contracts: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for contract in CORE_SUPPORT_POLICY_CONTRACTS:
        relative_path = str(contract["path"])
        path = project_root / relative_path
        if not path.exists():
            contracts.append(
                {
                    "name": str(contract["name"]),
                    "path": relative_path,
                    "status": "file_missing",
                    "violations": [],
                }
            )
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError:
            tree = None
        if tree is not None:
            _annotate_ast_parents(tree)
        contract_violations: list[dict[str, Any]] = []
        for pattern_name, pattern in dict(
            contract.get("forbidden_patterns") or {}
        ).items():
            if not isinstance(pattern, re.Pattern):
                continue
            for match in pattern.finditer(text):
                contract_violations.append(
                    {
                        "path": relative_path,
                        "line": _line_number(text, match.start()),
                        "pattern": str(pattern_name),
                    }
                )
        if tree is not None:
            contract_violations.extend(
                _scan_contract_symbol_drift(
                    tree=tree,
                    relative_path=relative_path,
                    contract=contract,
                )
            )
            allowed_regex_pattern_names = tuple(
                str(name)
                for name in contract.get("allowed_regex_pattern_names", ())
                if str(name).strip()
            )
            if allowed_regex_pattern_names:
                allowed_regex_helper_functions = tuple(
                    str(name)
                    for name in contract.get("allowed_regex_helper_functions", ())
                    if str(name).strip()
                )
                contract_violations.extend(
                    _scan_allowed_regex_backstop_scope(
                        tree=tree,
                        relative_path=relative_path,
                        allowed_pattern_names=allowed_regex_pattern_names,
                        allowed_helper_functions=allowed_regex_helper_functions,
                    )
                )
        contract_violations = sorted(
            contract_violations,
            key=lambda item: (
                int(item.get("line", 0) or 0),
                str(item.get("pattern") or ""),
                str(item.get("symbol") or ""),
                str(item.get("call") or ""),
            ),
        )
        contracts.append(
            {
                "name": str(contract["name"]),
                "path": relative_path,
                "status": "ok" if not contract_violations else "violation",
                "violations": contract_violations,
            }
        )
        violations.extend(contract_violations)
    return {
        "contract_count": len(contracts),
        "violation_count": len(violations),
        "contracts": contracts,
        "violations": violations,
    }


def _scan_repo_seed_authority_drift(project_root: Path) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in _iter_files(project_root, REPO_SEED_AUTHORITY_SCAN_GLOBS):
        relative_path = _relative_path(path, project_root)
        if relative_path == WORKFLOW_PURITY_SUPPORT_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        is_allowed = relative_path in REPO_SEED_AUTHORITY_ALLOWED_PATHS
        for pattern_name, pattern in REPO_SEED_AUTHORITY_PATTERNS.items():
            for match in pattern.finditer(text):
                matches.append(
                    {
                        "path": relative_path,
                        "line": _line_number(text, match.start()),
                        "pattern": pattern_name,
                        "allowed": is_allowed,
                    }
                )

    offending_matches = [item for item in matches if not item["allowed"]]
    offending_paths = sorted({item["path"] for item in offending_matches})
    return {
        "total_matches": len(matches),
        "offending_match_count": len(offending_matches),
        "offending_path_count": len(offending_paths),
        "matches": matches,
        "offending_matches": offending_matches,
        "offending_paths": offending_paths,
        "allowed_paths": sorted(REPO_SEED_AUTHORITY_ALLOWED_PATHS),
        "patterns": sorted(REPO_SEED_AUTHORITY_PATTERNS),
    }


def _scan_vontology_first_seed_fallback_contracts(project_root: Path) -> dict[str, Any]:
    contracts: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    for contract in SEED_FALLBACK_ORDER_CONTRACTS:
        relative_path = str(contract["path"])
        function_name = str(contract["function"])
        authoritative_calls = tuple(
            str(item)
            for item in contract.get("authoritative_calls", ())
            if isinstance(item, str) and str(item).strip()
        )
        fallback_calls = tuple(
            str(item)
            for item in contract.get("fallback_calls", ())
            if isinstance(item, str) and str(item).strip()
        )
        source_path = project_root / relative_path
        if not source_path.exists():
            contracts.append(
                {
                    "name": str(contract["name"]),
                    "path": relative_path,
                    "function": function_name,
                    "status": "file_missing",
                    "authoritative_calls": list(authoritative_calls),
                    "fallback_calls": list(fallback_calls),
                    "missing_authoritative_calls": [],
                    "late_authoritative_calls": [],
                    "first_fallback_line": None,
                }
            )
            continue
        function_record = _extract_top_level_function_source(
            path=source_path,
            project_root=project_root,
            function_name=function_name,
        )
        if function_record is None:
            contracts.append(
                {
                    "name": str(contract["name"]),
                    "path": relative_path,
                    "function": function_name,
                    "status": "function_missing",
                    "authoritative_calls": list(authoritative_calls),
                    "fallback_calls": list(fallback_calls),
                    "missing_authoritative_calls": [],
                    "late_authoritative_calls": [],
                    "first_fallback_line": None,
                }
            )
            continue

        function_text = str(function_record["text"])
        start_line = int(function_record["start_line"])
        authoritative_positions: dict[str, int] = {}
        fallback_positions: dict[str, int] = {}
        for call_name in authoritative_calls:
            pattern = re.compile(rf"\b{re.escape(call_name)}\s*\(")
            match = pattern.search(function_text)
            if match is not None:
                authoritative_positions[call_name] = match.start()
        for call_name in fallback_calls:
            pattern = re.compile(rf"\b{re.escape(call_name)}\s*\(")
            match = pattern.search(function_text)
            if match is not None:
                fallback_positions[call_name] = match.start()

        if fallback_positions:
            first_fallback_offset = min(fallback_positions.values())
            first_fallback_line = (
                start_line
                + _line_number(
                    function_text,
                    first_fallback_offset,
                )
                - 1
            )
        else:
            first_fallback_offset = None
            first_fallback_line = None

        missing_authoritative_calls = [
            call_name
            for call_name in authoritative_calls
            if call_name not in authoritative_positions
        ]
        late_authoritative_calls = [
            call_name
            for call_name, offset in authoritative_positions.items()
            if first_fallback_offset is not None and offset > first_fallback_offset
        ]
        violation = bool(
            fallback_positions
            and (missing_authoritative_calls or late_authoritative_calls)
        )
        status = (
            "violation"
            if violation
            else "no_fallback_calls_present" if not fallback_positions else "ok"
        )
        contract_result = {
            "name": str(contract["name"]),
            "path": relative_path,
            "function": function_name,
            "status": status,
            "authoritative_calls": list(authoritative_calls),
            "fallback_calls": list(fallback_calls),
            "found_authoritative_calls": sorted(authoritative_positions),
            "found_fallback_calls": sorted(fallback_positions),
            "missing_authoritative_calls": missing_authoritative_calls,
            "late_authoritative_calls": late_authoritative_calls,
            "first_fallback_line": first_fallback_line,
        }
        contracts.append(contract_result)
        if violation:
            violations.append(contract_result)

    return {
        "contract_count": len(contracts),
        "violation_count": len(violations),
        "contracts": contracts,
        "violations": violations,
    }


def _extract_workflow_id_string(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    candidate = str(node.value).strip()
    if not candidate.startswith("#V#"):
        return None
    return candidate if candidate.endswith("_workflow") else None


def _scan_workflow_id_special_case_branches(project_root: Path) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in _iter_files(project_root, WORKFLOW_ID_SPECIAL_CASE_SCAN_GLOBS):
        relative_path = _relative_path(path, project_root)
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not any(
                isinstance(operator, WORKFLOW_ID_SPECIAL_CASE_OPERATORS)
                for operator in node.ops
            ):
                continue
            workflow_ids = [
                workflow_id
                for workflow_id in (
                    _extract_workflow_id_string(node.left),
                    *(
                        _extract_workflow_id_string(comparator)
                        for comparator in node.comparators
                    ),
                )
                if workflow_id
            ]
            if not workflow_ids:
                continue
            operator_names = [
                type(operator).__name__
                for operator in node.ops
                if isinstance(operator, WORKFLOW_ID_SPECIAL_CASE_OPERATORS)
            ]
            matches.append(
                {
                    "path": relative_path,
                    "line": int(node.lineno),
                    "workflow_ids": sorted(dict.fromkeys(workflow_ids)),
                    "operators": operator_names,
                }
            )

    return {
        "match_count": len(matches),
        "matches": matches,
        "file_globs": list(WORKFLOW_ID_SPECIAL_CASE_SCAN_GLOBS),
    }


def _scan_supervised_fail_open_fallbacks(project_root: Path) -> dict[str, Any]:
    contract = dict(SUPERVISED_FAIL_OPEN_CONTRACT)
    relative_path = str(contract["path"])
    function_name = str(contract["function"])
    forbidden_patterns = {
        str(name): pattern
        for name, pattern in dict(contract.get("forbidden_patterns") or {}).items()
        if isinstance(name, str) and isinstance(pattern, re.Pattern)
    }
    source_path = project_root / relative_path
    if not source_path.exists():
        return {
            "path": relative_path,
            "function": function_name,
            "status": "file_missing",
            "offending_match_count": 0,
            "offending_matches": [],
            "forbidden_patterns": sorted(forbidden_patterns),
        }

    function_record = _extract_top_level_function_source(
        path=source_path,
        project_root=project_root,
        function_name=function_name,
    )
    if function_record is None:
        return {
            "path": relative_path,
            "function": function_name,
            "status": "function_missing",
            "offending_match_count": 0,
            "offending_matches": [],
            "forbidden_patterns": sorted(forbidden_patterns),
        }

    function_text = str(function_record["text"])
    start_line = int(function_record["start_line"])
    offending_matches: list[dict[str, Any]] = []
    for pattern_name, pattern in forbidden_patterns.items():
        for match in pattern.finditer(function_text):
            offending_matches.append(
                {
                    "path": relative_path,
                    "function": function_name,
                    "pattern": pattern_name,
                    "line": start_line + _line_number(function_text, match.start()) - 1,
                }
            )

    return {
        "path": relative_path,
        "function": function_name,
        "status": "violation" if offending_matches else "ok",
        "offending_match_count": len(offending_matches),
        "offending_matches": offending_matches,
        "forbidden_patterns": sorted(forbidden_patterns),
    }


def _collect_registry_sources(registry: Any | None) -> dict[str, Any]:
    if registry is None:
        return {
            "registry_available": False,
            "counts": {},
            "source_by_workflow_id": {},
        }

    source_counts: dict[str, int] = {}
    source_by_workflow_id: dict[str, str] = {}
    try:
        workflow_ids = sorted(set(registry.all_workflow_ids()))
    except Exception:
        workflow_ids = []

    for workflow_id in workflow_ids:
        source = "unknown"
        try:
            get_source = getattr(registry, "get_registration_source", None)
            if callable(get_source):
                source = (
                    str(get_source(workflow_id, resolve_lazy=False) or "").strip()
                    or "unknown"
                )
            else:
                registration = registry.get_registration(workflow_id)
                if registration is not None:
                    source = (
                        str(getattr(registration, "source", "") or "").strip()
                        or "unknown"
                    )
        except Exception:
            source = "unknown"
        source_by_workflow_id[workflow_id] = source
        source_counts[source] = source_counts.get(source, 0) + 1

        # JVNAUTOSCI-1818: Track synthesized (virtual) launch contracts as impurities
        # to encourage migration to fully KB-based declarative contracts.
        try:
            peek_registration = getattr(registry, "peek_registration", None)
            if callable(peek_registration):
                registration = peek_registration(workflow_id)
            else:
                registration = registry.get_registration(workflow_id)
            definition = getattr(registration, "definition", None)
            if registration and definition is not None:
                metadata = getattr(definition, "metadata", None)
                if isinstance(metadata, Mapping) and (
                    metadata.get("launch_contract_source")
                    == "synthesized_from_initial_state"
                ):
                    source_counts["synthesized_launch_contract_count"] = (
                        source_counts.get("synthesized_launch_contract_count", 0) + 1
                    )
        except Exception:
            pass

    return {
        "registry_available": True,
        "counts": source_counts,
        "source_by_workflow_id": source_by_workflow_id,
    }


def load_workflow_purity_baseline(
    baseline_path: Path | None = None,
) -> dict[str, Any] | None:
    path = baseline_path or WORKFLOW_PURITY_BASELINE_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def compare_workflow_purity_to_baseline(
    *,
    counters: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(baseline, Mapping):
        return {
            "baseline_available": False,
            "baseline_schema_version": None,
            "missing_counter_keys": [],
            "increased_counters": {},
            "regression_detected": False,
        }

    baseline_counters = (
        baseline.get("counters", {}) if isinstance(baseline, Mapping) else {}
    )
    if not isinstance(baseline_counters, Mapping):
        baseline_counters = {}

    increased: dict[str, dict[str, int]] = {}
    missing_counter_keys: list[str] = []
    for key, value in counters.items():
        if not isinstance(value, int):
            continue
        baseline_value = baseline_counters.get(key)
        if not isinstance(baseline_value, int):
            missing_counter_keys.append(key)
            continue
        if value > baseline_value:
            increased[key] = {
                "baseline": baseline_value,
                "current": value,
                "delta": value - baseline_value,
            }

    return {
        "baseline_available": bool(isinstance(baseline, Mapping)),
        "baseline_schema_version": (
            str(baseline.get("schema_version"))
            if isinstance(baseline, Mapping) and baseline.get("schema_version")
            else None
        ),
        "missing_counter_keys": sorted(missing_counter_keys),
        "increased_counters": increased,
        "regression_detected": bool(increased or missing_counter_keys),
    }


def build_workflow_purity_baseline_snapshot(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    counters = report.get("counters", {})
    if not isinstance(counters, Mapping):
        counters = {}
    return {
        "schema_version": WORKFLOW_PURITY_BASELINE_SCHEMA_VERSION,
        "generated_at_utc": _utc_now_iso(),
        "report_schema_version": report.get("schema_version"),
        "counters": {
            key: int(value)
            for key, value in counters.items()
            if isinstance(key, str) and isinstance(value, int)
        },
    }


def write_workflow_purity_baseline(
    report: Mapping[str, Any],
    baseline_path: Path | None = None,
) -> Path:
    path = baseline_path or WORKFLOW_PURITY_BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_workflow_purity_baseline_snapshot(report)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def build_workflow_purity_report(
    *,
    registry: Any | None,
    project_root: Path | None = None,
    baseline_path: Path | None = None,
) -> dict[str, Any]:
    """Build a structured workflow-first impurity report."""

    repo_root = (project_root or PROJECT_ROOT).resolve()
    runtime_sources = _collect_registry_sources(registry)
    source_counts = runtime_sources.get("counts", {})
    source_by_workflow_id = runtime_sources.get("source_by_workflow_id", {})
    if not isinstance(source_counts, Mapping):
        source_counts = {}
    if not isinstance(source_by_workflow_id, Mapping):
        source_by_workflow_id = {}

    built_in_workflow_ids = sorted(
        workflow_id
        for workflow_id, source in source_by_workflow_id.items()
        if source == "built_in"
    )
    non_vontology_discoverable_workflow_ids = sorted(
        workflow_id
        for workflow_id, source in source_by_workflow_id.items()
        if isinstance(source, str) and source != "vontology"
    )

    direct_create = _scan_direct_instance_create_callsites(repo_root)
    env_event_binding = _scan_env_event_binding_authority(repo_root)
    legacy_selector = _scan_legacy_selector_support(repo_root)
    python_workflow_families = _scan_python_workflow_family_files(repo_root)
    canonical_workflow_sources = _scan_python_authored_canonical_workflow_sources(
        repo_root
    )
    workflow_prompt_sources = _scan_python_authored_workflow_prompt_sources(repo_root)
    core_support_prompt_sources = _scan_python_authored_core_support_prompt_sources(
        repo_root
    )
    repo_seed_authority = _scan_repo_seed_authority_drift(repo_root)
    seed_fallback_contracts = _scan_vontology_first_seed_fallback_contracts(repo_root)
    workflow_id_special_cases = _scan_workflow_id_special_case_branches(repo_root)
    supervised_fail_open_fallbacks = _scan_supervised_fail_open_fallbacks(repo_root)
    core_support_policy_contracts = _scan_core_support_policy_contracts(repo_root)
    builtin_capability_overrides = sorted(BUILTIN_WORKFLOW_CAPABILITIES)

    counters = {
        "built_in_registration_count": len(built_in_workflow_ids),
        "remaining_python_workflow_family_count": int(
            python_workflow_families.get("family_file_count", 0)
        ),
        "python_authored_canonical_workflow_source_count": int(
            canonical_workflow_sources.get("source_count", 0)
        ),
        "python_authored_workflow_prompt_source_count": int(
            workflow_prompt_sources.get("source_count", 0)
        ),
        "python_authored_support_prompt_source_count": int(
            core_support_prompt_sources.get("source_count", 0)
        ),
        "direct_instance_create_callsite_count": int(
            direct_create.get("offending_callsite_count", 0)
        ),
        "env_event_binding_count": int(env_event_binding.get("total_matches", 0)),
        "legacy_selector_mode_count": int(legacy_selector.get("module_count", 0)),
        "builtin_capability_override_count": len(builtin_capability_overrides),
        "non_vontology_discoverable_workflow_count": len(
            non_vontology_discoverable_workflow_ids
        ),
        "repo_seed_authority_drift_path_count": int(
            repo_seed_authority.get("offending_path_count", 0)
        ),
        "vontology_first_seed_fallback_violation_count": int(
            seed_fallback_contracts.get("violation_count", 0)
        ),
        "workflow_id_special_case_count": int(
            workflow_id_special_cases.get("match_count", 0)
        ),
        "supervised_fail_open_fallback_count": int(
            supervised_fail_open_fallbacks.get("offending_match_count", 0)
        ),
        "support_surface_policy_contract_violation_count": int(
            core_support_policy_contracts.get("violation_count", 0)
        ),
        "synthesized_launch_contract_count": int(
            source_counts.get("synthesized_launch_contract_count", 0)
        ),
    }

    baseline = load_workflow_purity_baseline(baseline_path)
    comparison = compare_workflow_purity_to_baseline(
        counters=counters,
        baseline=baseline,
    )
    summary_parts = [f"{key}={value}" for key, value in counters.items()]
    summary_text = "Workflow purity: " + " ".join(summary_parts)

    return {
        "schema_version": WORKFLOW_PURITY_REPORT_SCHEMA_VERSION,
        "generated_at_utc": _utc_now_iso(),
        "summary_text": summary_text,
        "counters": counters,
        "details": {
            "registry_available": bool(runtime_sources.get("registry_available")),
            "registry_source_counts": dict(source_counts),
            "built_in_workflow_ids": built_in_workflow_ids,
            "non_vontology_discoverable_workflow_ids": non_vontology_discoverable_workflow_ids,
            "remaining_python_workflow_family_files": copy.deepcopy(
                python_workflow_families.get("family_files", [])
            ),
            "python_authored_canonical_workflow_sources": copy.deepcopy(
                canonical_workflow_sources.get("sources", [])
            ),
            "python_authored_workflow_prompt_sources": copy.deepcopy(
                workflow_prompt_sources.get("sources", [])
            ),
            "python_authored_support_prompt_sources": copy.deepcopy(
                core_support_prompt_sources.get("sources", [])
            ),
            "repo_seed_authority_drift": repo_seed_authority,
            "vontology_first_seed_fallback_contracts": seed_fallback_contracts,
            "workflow_id_special_cases": workflow_id_special_cases,
            "supervised_fail_open_fallbacks": supervised_fail_open_fallbacks,
            "support_surface_policy_contracts": core_support_policy_contracts,
            "direct_instance_create": direct_create,
            "env_event_binding_authority": env_event_binding,
            "legacy_selector_support": legacy_selector,
            "builtin_capability_override_workflow_ids": builtin_capability_overrides,
        },
        "baseline": {
            "path": _relative_path(
                (baseline_path or WORKFLOW_PURITY_BASELINE_PATH).resolve(),
                repo_root,
            ),
            "refresh_command": WORKFLOW_PURITY_BASELINE_REFRESH_COMMAND,
            "comparison": comparison,
        },
    }


__all__ = [
    "ALLOWED_DIRECT_INSTANCE_CREATE_CALLSITES",
    "CORE_SUPPORT_POLICY_CONTRACTS",
    "CORE_SUPPORT_PROMPT_SOURCE_FILES",
    "DIRECT_INSTANCE_CREATE_PATTERN",
    "ENV_EVENT_BINDING_AUTHORITY_PATTERNS",
    "LEGACY_SELECTOR_CONSTRUCT_PATTERNS",
    "LEGACY_SELECTOR_FILE",
    "REPO_SEED_AUTHORITY_ALLOWED_PATHS",
    "REPO_SEED_AUTHORITY_PATTERNS",
    "SEED_FALLBACK_ORDER_CONTRACTS",
    "WORKFLOW_PURITY_BASELINE_PATH",
    "WORKFLOW_PURITY_BASELINE_REFRESH_COMMAND",
    "WORKFLOW_PURITY_BASELINE_SCHEMA_VERSION",
    "WORKFLOW_PURITY_REPORT_SCHEMA_VERSION",
    "build_workflow_purity_baseline_snapshot",
    "build_workflow_purity_report",
    "compare_workflow_purity_to_baseline",
    "load_workflow_purity_baseline",
    "write_workflow_purity_baseline",
]
