"""Workflow-first purity reporting and regression helpers.

This module extends the existing workflow authority/parity diagnostics with
code-shape impurity counters that track the remaining hybrid authority surface.
The report is intentionally deterministic and suitable for both startup
diagnostics and CI regression gates.
"""

from __future__ import annotations

import ast
import copy
import fnmatch
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
            if any(pattern.match(node.name) for pattern in PYTHON_WORKFLOW_FAMILY_FUNCTION_PATTERNS):
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
                source = str(get_source(workflow_id, resolve_lazy=False) or "").strip() or "unknown"
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

    baseline_counters = baseline.get("counters", {}) if isinstance(baseline, Mapping) else {}
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


def build_workflow_purity_baseline_snapshot(report: Mapping[str, Any]) -> dict[str, Any]:
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
    builtin_capability_overrides = sorted(BUILTIN_WORKFLOW_CAPABILITIES)

    counters = {
        "built_in_registration_count": len(built_in_workflow_ids),
        "remaining_python_workflow_family_count": int(
            python_workflow_families.get("family_file_count", 0)
        ),
        "python_authored_canonical_workflow_source_count": int(
            canonical_workflow_sources.get("source_count", 0)
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
    "DIRECT_INSTANCE_CREATE_PATTERN",
    "ENV_EVENT_BINDING_AUTHORITY_PATTERNS",
    "LEGACY_SELECTOR_CONSTRUCT_PATTERNS",
    "LEGACY_SELECTOR_FILE",
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
