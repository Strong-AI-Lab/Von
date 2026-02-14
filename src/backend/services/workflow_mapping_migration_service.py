"""Migrate legacy workflow mapping concepts to structured schema specs.

This service backfills ``concept_data.preserved_fields.workflow_mapping_spec``
for mapping concepts referenced by workflow steps.

Design notes:
- Uses workflow topology from ``build_workflow_process_graph`` so migration
  aligns with executable runtime structure.
- Reuses loader parsing helpers for deterministic behaviour parity.
- Supports dry-run mode for safe preview before mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .concept_service import update_concept
from ..workflows.vontology_loader import (
    _extract_mapping_pair,
    _extract_structured_mapping_spec,
    _extract_tool_output_mapping,
    _fetch_concepts_by_id,
    _normalise_invoked_action_target,
    build_workflow_process_graph,
    discover_workflow_ids,
)


_SCHEMA_VERSION = 1
_INPUT_MAPPING_TYPE = "context_key_to_tool_param"
_OUTPUT_MAPPING_TYPE = "tool_output_field_to_context_key"


@dataclass(frozen=True)
class _MappingUsage:
    mapping_concept_id: str
    mapping_kind: str  # "input" | "output"
    workflow_id: str
    step_id: str
    action_id: str


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _context_key_symbol_to_concept_id(symbol: str) -> str | None:
    raw = str(symbol or "").strip()
    if not raw:
        return None
    if raw.startswith("#V#workflow_context_key_"):
        return raw
    if raw.startswith("workflow_context_key_"):
        return f"#V#{raw}"
    if raw.startswith("#V#"):
        return f"#V#workflow_context_key_{raw[3:]}"
    return f"#V#workflow_context_key_{raw}"


def _description_only_doc(mapping_doc: Mapping[str, Any] | None) -> Dict[str, Any] | None:
    """Return a stripped mapping doc retaining only description fallback text.

    This intentionally ignores any existing structured mapping spec when we need
    to recover from invalid schema objects.
    """

    if not isinstance(mapping_doc, Mapping):
        return None

    concept_data = mapping_doc.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return None
    preserved_fields = concept_data.get("preserved_fields")
    if not isinstance(preserved_fields, Mapping):
        return None
    description = preserved_fields.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    return {
        "concept_data": {
            "preserved_fields": {
                "description": description,
            }
        }
    }


def _parse_input_mapping(
    *,
    mapping_concept_id: str,
    mapping_doc: Mapping[str, Any] | None,
    step_id: str,
    action_id: str,
) -> tuple[str | None, str | None, str | None, str]:
    context_key, tool_param, reason = _extract_mapping_pair(
        mapping_concept_id=mapping_concept_id,
        mapping_doc=dict(mapping_doc) if isinstance(mapping_doc, Mapping) else None,
        step_id=step_id,
        action_id=action_id,
    )
    if context_key and tool_param and not reason:
        return context_key, tool_param, None, "primary"

    structured_spec, _ = _extract_structured_mapping_spec(
        dict(mapping_doc) if isinstance(mapping_doc, Mapping) else None
    )
    if structured_spec:
        fallback_doc = _description_only_doc(mapping_doc)
        context_key, tool_param, fallback_reason = _extract_mapping_pair(
            mapping_concept_id=mapping_concept_id,
            mapping_doc=fallback_doc,
            step_id=step_id,
            action_id=action_id,
        )
        if context_key and tool_param and not fallback_reason:
            return context_key, tool_param, None, "legacy_recovery"

    return None, None, reason or "mapping_pattern_not_detected", "primary"


def _parse_output_mapping(
    *,
    mapping_concept_id: str,
    mapping_doc: Mapping[str, Any] | None,
    step_id: str,
    action_id: str,
) -> tuple[str | None, str | None, str | None, str]:
    output_field, context_key, reason = _extract_tool_output_mapping(
        mapping_concept_id=mapping_concept_id,
        mapping_doc=dict(mapping_doc) if isinstance(mapping_doc, Mapping) else None,
        step_id=step_id,
        action_id=action_id,
    )
    if output_field and context_key and not reason:
        return output_field, context_key, None, "primary"

    structured_spec, _ = _extract_structured_mapping_spec(
        dict(mapping_doc) if isinstance(mapping_doc, Mapping) else None
    )
    if structured_spec:
        fallback_doc = _description_only_doc(mapping_doc)
        output_field, context_key, fallback_reason = _extract_tool_output_mapping(
            mapping_concept_id=mapping_concept_id,
            mapping_doc=fallback_doc,
            step_id=step_id,
            action_id=action_id,
        )
        if output_field and context_key and not fallback_reason:
            return output_field, context_key, None, "legacy_recovery"

    return None, None, reason or "mapping_pattern_not_detected", "primary"


def _build_input_spec(
    *,
    step_id: str,
    action_id: str,
    context_key_symbol: str,
    tool_param_name: str,
) -> Dict[str, Any] | None:
    context_key_concept_id = _context_key_symbol_to_concept_id(context_key_symbol)
    if not context_key_concept_id:
        return None
    return {
        "schema_version": _SCHEMA_VERSION,
        "mapping_type": _INPUT_MAPPING_TYPE,
        "workflow_step_id": step_id,
        "tool_id": action_id,
        "context_key_concept_id": context_key_concept_id,
        "tool_param_name": tool_param_name,
    }


def _build_output_spec(
    *,
    step_id: str,
    action_id: str,
    tool_output_field_name: str,
    target_context_key_symbol: str,
) -> Dict[str, Any] | None:
    context_key_concept_id = _context_key_symbol_to_concept_id(target_context_key_symbol)
    if not context_key_concept_id:
        return None
    return {
        "schema_version": _SCHEMA_VERSION,
        "mapping_type": _OUTPUT_MAPPING_TYPE,
        "workflow_step_id": step_id,
        "tool_id": action_id,
        "tool_output_field_name": tool_output_field_name,
        "target_context_key_concept_id": context_key_concept_id,
    }


def _spec_matches_target(existing_spec: Mapping[str, Any] | None, target_spec: Mapping[str, Any]) -> bool:
    if not isinstance(existing_spec, Mapping):
        return False
    for key, value in target_spec.items():
        if existing_spec.get(key) != value:
            return False
    return True


def _iter_mapping_usages(
    workflow_ids: Sequence[str],
) -> tuple[list[_MappingUsage], list[Dict[str, str]]]:
    usages: list[_MappingUsage] = []
    workflow_errors: list[Dict[str, str]] = []
    for workflow_id in workflow_ids:
        graph, warnings = build_workflow_process_graph(workflow_id)
        if not isinstance(graph, dict):
            workflow_errors.append(
                {
                    "workflow_id": workflow_id,
                    "reason_code": "workflow_graph_unavailable",
                    "detail": ",".join(warnings[:8]) if warnings else "",
                }
            )
            continue
        steps = graph.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            step_id = str(step.get("step_id") or "").strip()
            action_id = _normalise_invoked_action_target(str(step.get("invokes_action") or "").strip())
            if not step_id or not action_id:
                continue

            input_ids = step.get("context_input_mappings")
            if isinstance(input_ids, list):
                for mapping_id in input_ids:
                    if isinstance(mapping_id, str) and mapping_id.strip():
                        usages.append(
                            _MappingUsage(
                                mapping_concept_id=mapping_id.strip(),
                                mapping_kind="input",
                                workflow_id=workflow_id,
                                step_id=step_id,
                                action_id=action_id,
                            )
                        )

            output_ids = step.get("tool_output_context_mappings")
            if isinstance(output_ids, list):
                for mapping_id in output_ids:
                    if isinstance(mapping_id, str) and mapping_id.strip():
                        usages.append(
                            _MappingUsage(
                                mapping_concept_id=mapping_id.strip(),
                                mapping_kind="output",
                                workflow_id=workflow_id,
                                step_id=step_id,
                                action_id=action_id,
                            )
                        )
    return usages, workflow_errors


def migrate_workflow_mapping_specs(
    *,
    workflow_ids: Sequence[str] | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    rewrite_existing: bool = False,
) -> Dict[str, Any]:
    """Backfill structured mapping specs for workflow mapping concepts.

    Args:
        workflow_ids: Optional explicit workflow IDs. When omitted, all
            discoverable workflows are scanned.
        dry_run: When True, no concept updates are written.
        limit: Optional maximum number of distinct mapping concepts to process.
        rewrite_existing: When True, rewrites even already-valid structured
            specs to canonicalised values.
    """

    selected_workflow_ids = (
        [wid for wid in workflow_ids if isinstance(wid, str) and wid.strip()]
        if workflow_ids is not None
        else discover_workflow_ids()
    )
    selected_workflow_ids = [wid.strip() for wid in selected_workflow_ids if wid.strip()]

    usages, workflow_errors = _iter_mapping_usages(selected_workflow_ids)
    usage_by_mapping: Dict[str, list[_MappingUsage]] = {}
    for usage in usages:
        usage_by_mapping.setdefault(usage.mapping_concept_id, []).append(usage)

    mapping_ids = sorted(usage_by_mapping.keys())
    if isinstance(limit, int) and limit > 0:
        mapping_ids = mapping_ids[:limit]
    mapping_docs = _fetch_concepts_by_id(mapping_ids) if mapping_ids else {}

    stats = {
        "workflows_scanned": len(selected_workflow_ids),
        "workflow_errors": workflow_errors,
        "mapping_usages_scanned": len(usages),
        "distinct_mapping_concepts_scanned": len(mapping_ids),
        "migrated": 0,
        "updated": 0,
        "already_structured": 0,
        "skipped_conflicting_context": 0,
        "skipped_unresolved": 0,
        "errors": 0,
    }
    details: list[Dict[str, Any]] = []

    for mapping_id in mapping_ids:
        mapping_contexts = usage_by_mapping.get(mapping_id, [])
        signature_set = {
            (u.mapping_kind, u.step_id, u.action_id) for u in mapping_contexts
        }
        if len(signature_set) != 1:
            stats["skipped_conflicting_context"] += 1
            details.append(
                {
                    "mapping_concept_id": mapping_id,
                    "status": "skipped_conflicting_context",
                    "contexts": sorted(
                        [
                            {
                                "mapping_kind": u.mapping_kind,
                                "workflow_id": u.workflow_id,
                                "step_id": u.step_id,
                                "action_id": u.action_id,
                            }
                            for u in mapping_contexts
                        ],
                        key=lambda item: (
                            item["mapping_kind"],
                            item["workflow_id"],
                            item["step_id"],
                            item["action_id"],
                        ),
                    ),
                }
            )
            continue

        mapping_kind, step_id, action_id = next(iter(signature_set))
        mapping_doc = mapping_docs.get(mapping_id)
        if not isinstance(mapping_doc, Mapping):
            stats["errors"] += 1
            details.append(
                {
                    "mapping_concept_id": mapping_id,
                    "status": "error",
                    "reason_code": "mapping_doc_not_found",
                    "mapping_kind": mapping_kind,
                    "step_id": step_id,
                    "tool_id": action_id,
                }
            )
            continue

        structured_spec, _ = _extract_structured_mapping_spec(dict(mapping_doc))
        parse_source = "primary"
        parse_error: str | None = None
        target_spec: Dict[str, Any] | None = None

        if mapping_kind == "input":
            context_key, tool_param, parse_error, parse_source = _parse_input_mapping(
                mapping_concept_id=mapping_id,
                mapping_doc=mapping_doc,
                step_id=step_id,
                action_id=action_id,
            )
            if context_key and tool_param and not parse_error:
                target_spec = _build_input_spec(
                    step_id=step_id,
                    action_id=action_id,
                    context_key_symbol=context_key,
                    tool_param_name=tool_param,
                )
        else:
            output_field, context_key, parse_error, parse_source = _parse_output_mapping(
                mapping_concept_id=mapping_id,
                mapping_doc=mapping_doc,
                step_id=step_id,
                action_id=action_id,
            )
            if output_field and context_key and not parse_error:
                target_spec = _build_output_spec(
                    step_id=step_id,
                    action_id=action_id,
                    tool_output_field_name=output_field,
                    target_context_key_symbol=context_key,
                )

        if parse_error or not isinstance(target_spec, dict):
            stats["skipped_unresolved"] += 1
            details.append(
                {
                    "mapping_concept_id": mapping_id,
                    "status": "skipped_unresolved",
                    "reason_code": parse_error or "target_spec_unavailable",
                    "mapping_kind": mapping_kind,
                    "step_id": step_id,
                    "tool_id": action_id,
                    "parse_source": parse_source,
                }
            )
            continue

        if _spec_matches_target(structured_spec, target_spec) and not rewrite_existing:
            stats["already_structured"] += 1
            details.append(
                {
                    "mapping_concept_id": mapping_id,
                    "status": "already_structured",
                    "mapping_kind": mapping_kind,
                    "step_id": step_id,
                    "tool_id": action_id,
                }
            )
            continue

        if dry_run:
            stats["migrated"] += 1
            details.append(
                {
                    "mapping_concept_id": mapping_id,
                    "status": "would_migrate",
                    "mapping_kind": mapping_kind,
                    "step_id": step_id,
                    "tool_id": action_id,
                    "target_spec": target_spec,
                    "parse_source": parse_source,
                }
            )
            continue

        try:
            update_concept(
                concept_id=mapping_id,
                update_data={
                    "concept_data.preserved_fields.workflow_mapping_spec": target_spec,
                    "concept_data.preserved_fields.workflow_mapping_spec_migrated_at": _iso_now(),
                },
            )
            stats["migrated"] += 1
            stats["updated"] += 1
            details.append(
                {
                    "mapping_concept_id": mapping_id,
                    "status": "migrated",
                    "mapping_kind": mapping_kind,
                    "step_id": step_id,
                    "tool_id": action_id,
                    "parse_source": parse_source,
                }
            )
        except Exception as exc:
            stats["errors"] += 1
            details.append(
                {
                    "mapping_concept_id": mapping_id,
                    "status": "error",
                    "reason_code": "update_failed",
                    "detail": str(exc),
                    "mapping_kind": mapping_kind,
                    "step_id": step_id,
                    "tool_id": action_id,
                }
            )

    return {
        "success": stats["errors"] == 0,
        "dry_run": bool(dry_run),
        "rewrite_existing": bool(rewrite_existing),
        "workflow_ids": selected_workflow_ids,
        "stats": stats,
        "details": details,
    }


def migrate_workflow_mapping_specs_for_workflows(
    workflow_ids: Iterable[str],
    *,
    dry_run: bool = True,
    limit: int | None = None,
    rewrite_existing: bool = False,
) -> Dict[str, Any]:
    """Convenience wrapper for explicit workflow ID migration."""

    return migrate_workflow_mapping_specs(
        workflow_ids=[wid for wid in workflow_ids if isinstance(wid, str)],
        dry_run=dry_run,
        limit=limit,
        rewrite_existing=rewrite_existing,
    )

